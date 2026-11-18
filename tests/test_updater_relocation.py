import contextlib
import io
import json
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_updater as fixtures

u = fixtures.u


class UpdaterRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / 'runtime'
        self.base.mkdir()
        original = self.root / 'Original location/Bark.app'
        fixtures.app_fixture(original)
        moved = self.root / 'Moved folder with spaces'
        moved.mkdir()
        self.app = original.rename(moved / 'My Bark.app')
        self.up = u.Updater(app=self.app, base=self.base)

    def stage(self):
        return fixtures.UpdaterTests.stage(self)

    def apply(self, fault=None):
        with patch.object(self.up, 'verify_signature'), patch.object(self.up, 'validate_payload'):
            return self.up.apply(restart=False, fault=fault)

    def test_default_uses_registered_moved_bundle_and_unavailable_location_fails_closed(self):
        with patch.object(u, 'resolve_app', return_value=self.app) as resolve:
            resolved = u.Updater(base=self.base)
        self.assertEqual(resolved.app, self.app)
        resolve.assert_called_once_with(self.base)
        with patch.object(u, 'resolve_app', side_effect=u.UpdateError('app_location_error')):
            with self.assertRaisesRegex(u.UpdateError, 'app_location_error'):
                u.Updater(base=self.base)

    def test_explicit_bundle_is_pinned_without_reconsulting_bookmark(self):
        with patch.object(u, 'resolve_app', side_effect=AssertionError('must stay pinned')):
            updater = u.Updater(app=self.app, base=self.base)
            self.assertEqual(updater.installed()['version'], '2.5.0')
            self.assertEqual(updater.app, self.app)

    def test_worker_keeps_current_destination_and_runtime_base(self):
        panel = str(self.app / 'Contents/Helpers/Codex Bark Panel.app/Contents/MacOS/CodexBarkPanel')
        with patch.object(u, 'process_path', return_value=panel), \
                patch.object(self.up, 'status', return_value={'status': 'ready'}), \
                patch.object(u.subprocess, 'Popen') as launch:
            self.up.start(123)
        command = launch.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, '-I', '-B', str(self.up.root / 'worker.py')])
        self.assertEqual(command[command.index('--app') + 1], str(self.app))
        self.assertEqual(command[command.index('--base') + 1], str(self.base))
        self.assertEqual(command[command.index('--parent-pid') + 1], '123')

    def test_cli_passes_explicit_current_app(self):
        output = io.StringIO()
        argv = ['updater.py', 'status', '--app', str(self.app), '--base', str(self.base)]
        with patch.object(sys, 'argv', argv), patch.object(u, 'Updater') as constructor, contextlib.redirect_stdout(output):
            constructor.return_value.status.return_value = {'ok': True}
            u.main()
        constructor.assert_called_once_with(app=str(self.app), base=str(self.base))
        self.assertEqual(json.loads(output.getvalue()), {'ok': True})

    def test_copied_isolated_worker_reads_explicit_bundle(self):
        worker = self.up.root / 'worker.py'
        shutil.copy2(Path(u.__file__), worker)
        result = subprocess.run([sys.executable, '-I', '-B', str(worker), 'status', '--app', str(self.app),
                                 '--base', str(self.base)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['installed']['version'], '2.5.0')

    def test_location_manager_loads_stable_runtime_even_from_isolated_worker(self):
        (self.base / 'manage.py').write_text(
            'from pathlib import Path\n'
            'def resolve_app(base=None):\n'
            '    return Path(base).parent / "Moved folder with spaces/My Bark.app"\n'
            'def register_app(app, base=None, migrate_login=True):\n'
            '    assert migrate_login is False\n'
            '    (Path(base) / "registration.txt").write_text(str(app))\n')
        worker = self.up.root / 'worker.py'
        shutil.copy2(Path(u.__file__), worker)
        result = subprocess.run([sys.executable, '-I', '-B', str(worker), 'status', '--base', str(self.base)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['installed']['version'], '2.5.0')
        u.register_app(self.app, self.base)
        self.assertEqual((self.base / 'registration.txt').read_text(), str(self.app))

    def test_update_swaps_actual_moved_bundle_and_records_original_destination(self):
        self.stage()
        sentinel = self.base / 'config.json'
        sentinel.write_bytes(b'private sentinel')
        with patch.object(u, 'register_app') as register:
            self.apply()
        self.assertEqual(self.up.installed()['version'], '2.6.0')
        txn = u.read_json(self.up.transaction_path)
        self.assertEqual(txn['app_path'], str(self.app))
        self.assertEqual(txn['phase'], 'complete')
        register.assert_called_once_with(self.app, self.base)
        self.assertEqual(sentinel.read_bytes(), b'private sentinel')
        self.assertFalse(any(self.app.parent.glob('.Bark-update-*.app')))

    def test_failure_rolls_back_in_same_moved_folder_and_refreshes_old_bundle(self):
        self.stage()
        (self.base / 'bark.py').write_bytes(b'old runtime')
        versions = []
        def register(app, base):
            versions.append(self.up.installed()['version'])
        def fail(point):
            if point == 'before_commit':
                raise RuntimeError('injected')
        with patch.object(u, 'register_app', side_effect=register):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                self.apply(fail)
        self.assertEqual(versions, ['2.6.0', '2.5.0'])
        self.assertEqual(self.up.installed()['version'], '2.5.0')
        self.assertEqual((self.base / 'bark.py').read_bytes(), b'old runtime')
        self.assertEqual(u.read_json(self.up.transaction_path)['app_path'], str(self.app))

    def test_location_refresh_failure_cannot_leave_new_programs_or_block_rollback(self):
        self.stage()
        for name in u.RUNTIME:
            (self.base / name).write_bytes(('original ' + name).encode())
        with patch.object(u, 'register_app', side_effect=RuntimeError('bookmark unavailable')) as register:
            with self.assertRaisesRegex(u.UpdateError, 'app_location_error'):
                self.apply()
        self.assertEqual(register.call_count, 2)
        self.assertEqual(self.up.installed()['version'], '2.5.0')
        for name in u.RUNTIME:
            self.assertEqual((self.base / name).read_bytes(), ('original ' + name).encode())
        self.assertEqual(u.read_json(self.up.transaction_path)['phase'], 'rolled_back')

    def test_recovery_uses_recorded_destination_after_bookmarked_inode_was_swapped(self):
        self.stage()
        original_inode = self.app.stat().st_ino
        (self.base / 'bark.py').write_bytes(b'original runtime')
        class PowerLoss(BaseException):
            pass
        def crash(point):
            if point == 'after_swap':
                raise PowerLoss()
        with self.assertRaises(PowerLoss):
            self.apply(crash)
        txn = u.read_json(self.up.transaction_path)
        old_inode_path = self.app.with_name('.Bark-update-' + txn['id'] + '.app')
        self.assertEqual(old_inode_path.stat().st_ino, original_inode)
        # resolve_app owns bookmark/transaction reconciliation. The updater must
        # preserve the returned destination through its own recovery and swap.
        with patch.object(u, 'resolve_app', return_value=Path(txn['app_path'])), \
                patch.object(u, 'register_app') as register:
            resumed = u.Updater(base=self.base)
            self.assertTrue(resumed.recover()['recovered'])
        self.assertEqual(resumed.app, self.app)
        self.assertEqual(resumed.installed()['version'], '2.5.0')
        self.assertEqual((self.base / 'bark.py').read_bytes(), b'original runtime')
        register.assert_called_once_with(self.app, self.base)
        self.assertFalse(old_inode_path.exists())

    def test_active_transaction_cannot_be_redirected_by_explicit_other_bundle(self):
        txn = {'id': 'backup-' + 'a' * 32, 'phase': 'applying', 'app_path': str(self.app), 'runtime_present': []}
        u.write_json(self.up.transaction_path, txn)
        other = self.root / 'Other.app'
        fixtures.app_fixture(other)
        before = (other / 'Contents/Info.plist').read_bytes()
        with self.assertRaisesRegex(u.UpdateError, 'app_location_error'):
            u.Updater(app=other, base=self.base)
        self.assertEqual((other / 'Contents/Info.plist').read_bytes(), before)

    def test_transaction_paths_and_runtime_scope_are_checked_before_restore(self):
        valid = {'id': 'backup-' + 'a' * 32, 'phase': 'applying', 'app_path': str(self.app), 'runtime_present': []}
        other = self.root / 'Other.app'
        fixtures.app_fixture(other)
        for change in ({'id': '../outside'}, {'app_path': str(other)},
                       {'app_path': str(self.base / 'Data.app')}, {'app_path': 'relative.app'},
                       {'runtime_present': ['../config.json']}, {'runtime_present': ['bark.py', 'bark.py']}):
            with self.subTest(change=change), self.assertRaises(u.UpdateError):
                self.up.restore({**valid, **change})
        self.assertEqual(self.up.installed()['version'], '2.5.0')
        self.assertFalse(self.up.transaction_path.exists())

    def test_legacy_transaction_is_recovered_only_for_explicit_manifest_installation(self):
        self.stage()
        class PowerLoss(BaseException):
            pass
        def crash(point):
            if point == 'after_swap': raise PowerLoss()
        with self.assertRaises(PowerLoss): self.apply(crash)
        txn = u.read_json(self.up.transaction_path)
        txn.pop('app_path')
        u.write_json(self.up.transaction_path, txn)
        u.write_json(self.base / 'installation.json', {'menu_app': {'path': str(self.app)}})
        with patch.object(u, 'resolve_app', return_value=self.app):
            with self.assertRaisesRegex(u.UpdateError, 'local_state_error'):
                u.Updater(base=self.base)
        with patch.object(u, 'register_app'):
            resumed = u.Updater(app=self.app, base=self.base)
            self.assertTrue(resumed.recover()['recovered'])
        self.assertEqual(resumed.installed()['version'], '2.5.0')
        self.assertEqual(u.read_json(self.up.transaction_path)['app_path'], str(self.app))

    def test_legacy_transaction_refuses_missing_or_unrelated_metadata(self):
        txn = {'id': 'backup-' + 'a' * 32, 'phase': 'applying', 'runtime_present': []}
        u.write_json(self.up.transaction_path, txn)
        with self.assertRaisesRegex(u.UpdateError, 'local_state_error'):
            u.Updater(app=self.app, base=self.base)
        u.write_json(self.base / 'installation.json', {'menu_app': {'path': str(self.app)}})
        u.write_json(self.base / 'app-location.json', {'schema': 1, 'bundle_id': 'local.codex.bark.menu',
                                                      'path': str(self.root / 'Other.app')})
        with self.assertRaisesRegex(u.UpdateError, 'local_state_error'):
            u.Updater(app=self.app, base=self.base)

    def complete_location_fixture(self, app, manager):
        # Public structural fixtures pass the real manager's bundle validation;
        # they carry no developer signing identity or user configuration.
        for prefix, identifier, executable in [
            ('Contents', 'local.codex.bark.menu', 'CodexBarkMenu'),
            ('Contents/Helpers/Codex Bark Panel.app/Contents', 'local.codex.bark.panel', 'CodexBarkPanel'),
        ]:
            contents = app / prefix
            (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
            info_path = contents / 'Info.plist'
            info = plistlib.loads(info_path.read_bytes()) if info_path.exists() else {}
            info.update(CFBundleIdentifier=identifier, CFBundleExecutable=executable, CFBundlePackageType='APPL')
            info_path.write_bytes(plistlib.dumps(info))
            binary = contents / 'MacOS' / executable
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o700)
        (app / 'Contents/Helpers/BarkUpdateVerifier').chmod(0o700)
        (app / 'Contents/Resources/update-public-key.txt').write_text(manager.UPDATE_PUBLIC_KEY + '\n')

    @unittest.skipUnless(sys.platform == 'darwin', 'actual CoreFoundation bookmark integration')
    def test_real_bookmark_tracks_move_then_pins_destination_during_swap_recovery(self):
        manager = u.location_manager(fixtures.ROOT / 'src/runtime')
        self.complete_location_fixture(self.app, manager)
        manager.register_app(self.app, self.base, migrate_login=False)
        new_parent = self.root / 'Moved again while closed'
        self.app.parent.rename(new_parent)
        self.app = new_parent / self.app.name
        with patch.object(u, 'location_manager', return_value=manager):
            self.up = u.Updater(base=self.base)
            self.assertEqual(self.up.app, self.app)
            self.stage()
            self.complete_location_fixture(self.up.root / 'stage/payload/Bark.app', manager)
            class PowerLoss(BaseException):
                pass
            def crash(point):
                if point == 'after_swap': raise PowerLoss()
            with self.assertRaises(PowerLoss): self.apply(crash)
            txn = u.read_json(self.up.transaction_path)
            record = manager._location_record(self.base)
            bookmarked, _ = manager._resolve_app_bookmark(record)
            self.assertEqual(bookmarked, self.app.with_name('.Bark-update-' + txn['id'] + '.app'))
            self.assertEqual(manager.resolve_app(self.base), self.app)
            resumed = u.Updater(base=self.base)
            self.assertTrue(resumed.recover()['recovered'])
            self.assertEqual(manager.resolve_app(self.base), self.app)
            self.assertEqual(resumed.installed()['version'], '2.5.0')

    @unittest.skipUnless(sys.platform == 'darwin', 'actual CoreFoundation bookmark integration')
    def test_successful_swap_refreshes_real_bookmark_before_old_inode_is_deleted(self):
        manager = u.location_manager(fixtures.ROOT / 'src/runtime')
        self.complete_location_fixture(self.app, manager)
        manager.register_app(self.app, self.base, migrate_login=False)
        self.stage()
        self.complete_location_fixture(self.up.root / 'stage/payload/Bark.app', manager)
        original_inode = self.app.stat().st_ino
        with patch.object(u, 'location_manager', return_value=manager):
            self.apply()
            self.assertEqual(manager.resolve_app(self.base), self.app)
            self.assertEqual(u.Updater(base=self.base).installed()['version'], '2.6.0')
        self.assertNotEqual(self.app.stat().st_ino, original_inode)
        self.assertFalse(any(self.app.parent.glob('.Bark-update-*.app')))


if __name__ == '__main__':
    unittest.main()
