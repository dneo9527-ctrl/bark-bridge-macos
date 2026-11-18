"""Public temporary app fixtures only; never inspect the installed Bark runtime."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_codex_bark as fixtures

manage = fixtures.module("location_manage", fixtures.SRC / "manage.py")


def make_app(path, version="2.6.2"):
    for prefix, identifier, executable in [
        ("Contents", "local.codex.bark.menu", "CodexBarkMenu"),
        ("Contents/Helpers/Codex Bark Panel.app/Contents", "local.codex.bark.panel", "CodexBarkPanel"),
    ]:
        contents = path / prefix
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": executable,
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
        }))
        binary = contents / "MacOS" / executable
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
    verifier = path / "Contents/Helpers/BarkUpdateVerifier"
    verifier.write_text("#!/bin/sh\nexit 0\n")
    verifier.chmod(0o700)
    resources = path / "Contents/Resources"
    resources.mkdir()
    (resources / "release.json").write_text(json.dumps({"update_protocol": 1}))
    (resources / "update-public-key.txt").write_text(manage.UPDATE_PUBLIC_KEY + "\n")
    return path


@unittest.skipUnless(sys.platform == "darwin", "actual CoreFoundation bookmark integration")
class AppLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.base = self.root / "runtime"
        self.base.mkdir(mode=0o700)
        self.app = make_app(self.root / "Original/Bark.app")
        self.legacy = make_app(self.root / "Legacy/Bark.app", "2.6.1")
        self.login = self.root / "LaunchAgents/local.codex.bark.menu.plist"
        self.manifest = self.base / "installation.json"
        self.original_manifest = {"homes": {}, "backups": ["preserve"],
                                  "menu_app": {"path": str(self.app), "version": "keep-version",
                                               "login_plist": str(self.login)},
                                  "future_setting": {"preserve": True}}
        self.manifest.write_text(json.dumps(self.original_manifest))
        self.manifest.chmod(0o600)
        (self.base / "manage.py").write_text("# fixture launcher path\n")
        self.patchers = [mock.patch.object(manage, "BASE", self.base),
                         mock.patch.object(manage, "MANIFEST", self.manifest),
                         mock.patch.object(manage, "LOGIN_FILE", self.login),
                         mock.patch.object(manage, "APP_PATH", self.legacy)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def write_login(self, enabled=True):
        self.login.parent.mkdir(exist_ok=True)
        data = {"Label": manage.APP_IDENTIFIER, "RunAtLoad": enabled,
                "ProgramArguments": ["/usr/bin/open", "-g", str(self.app), "--args", "--background"],
                "KeepAlive": False, "future_setting": "preserve"}
        self.login.write_bytes(plistlib.dumps(data))
        self.login.chmod(0o600)
        return data

    def transaction(self, path, phase="applying"):
        directory = self.base / "updates"
        directory.mkdir(exist_ok=True)
        transaction = {"id": "backup-" + "a" * 32, "phase": phase, "app_path": str(path)}
        (directory / "transaction.json").write_text(json.dumps(transaction))
        (directory / "transaction.json").chmod(0o600)
        return transaction

    def test_closed_move_and_parent_rename_launch_actual_bundle_preserving_settings(self):
        self.write_login()
        manage.register_app(self.app)
        moved_parent = self.root / "User Selected Folder"
        self.app.parent.rename(moved_parent)
        moved = moved_parent / "Renamed Bark.app"
        (moved_parent / "Bark.app").rename(moved)
        self.assertEqual(manage.resolve_app(), moved)
        # A new process has no in-memory URL cache; disk bookmark still resolves.
        other = fixtures.module("fresh_location_manage", fixtures.SRC / "manage.py")
        self.assertEqual(other.resolve_app(self.base), moved)
        with mock.patch.object(manage.subprocess, "run") as run:
            self.assertEqual(manage.launch_app(), moved)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-g", str(moved), "--args", "--background"])
        expected = json.loads(json.dumps(self.original_manifest))
        expected["menu_app"]["path"] = str(moved)
        self.assertEqual(json.loads(self.manifest.read_text()), expected)
        login = plistlib.loads(self.login.read_bytes())
        self.assertEqual(login["ProgramArguments"], manage.login_arguments())
        self.assertEqual(login["future_setting"], "preserve")
        self.assertEqual((self.base / "app-location.json").stat().st_mode & 0o777, 0o600)

    def test_registration_idempotent_and_never_reads_private_configuration(self):
        self.write_login()
        for name in ("config.json", "rules.json", "auth.json"):
            (self.base / name).write_text("PRIVATE_FIXTURE_KEEP")
        manage.register_app(self.app)
        tracked = [self.base / "app-location.json", self.manifest, self.login,
                   self.base / "config.json", self.base / "rules.json"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked}
        original_open = Path.open

        def public_only(path, *args, **kwargs):
            if path.name in ("config.json", "rules.json", "auth.json"):
                raise AssertionError("private configuration must not be read")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", public_only):
            self.assertEqual(manage.resolve_app(), self.app)
            manage.register_app(self.app)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked})

    def test_registration_never_enables_disabled_login(self):
        manage.register_app(self.app)
        self.assertFalse(self.login.exists())
        original = self.write_login(enabled=False)
        before = self.login.read_bytes(), self.login.stat().st_mtime_ns
        manage.register_app(self.app)
        self.assertEqual((self.login.read_bytes(), self.login.stat().st_mtime_ns), before)
        self.assertEqual(plistlib.loads(self.login.read_bytes()), original)

    def test_pending_update_preserves_legacy_login_until_commit(self):
        original = self.write_login()
        before = self.login.read_bytes(), self.login.stat().st_mtime_ns
        self.transaction(self.app, phase="awaiting_start")
        manage.register_app(self.app)
        self.assertEqual((self.login.read_bytes(), self.login.stat().st_mtime_ns), before)
        self.assertEqual(plistlib.loads(self.login.read_bytes()), original)
        self.transaction(self.app, phase="complete")
        manage.register_app(self.app)
        committed = plistlib.loads(self.login.read_bytes())
        self.assertEqual(committed["ProgramArguments"], manage.login_arguments())
        self.assertTrue(committed["RunAtLoad"])
        self.assertEqual(committed["future_setting"], original["future_setting"])

    def test_pending_update_and_commit_never_enable_disabled_login(self):
        original = self.write_login(enabled=False)
        before = self.login.read_bytes(), self.login.stat().st_mtime_ns
        for phase in ("awaiting_start", "complete"):
            self.transaction(self.app, phase=phase)
            manage.register_app(self.app)
            self.assertEqual((self.login.read_bytes(), self.login.stat().st_mtime_ns), before)
            self.assertEqual(plistlib.loads(self.login.read_bytes()), original)

    def test_lost_registered_app_never_falls_back_to_other_installation(self):
        manage.register_app(self.app)
        shutil.rmtree(self.app)
        with self.assertRaises(RuntimeError):
            manage.resolve_app()
        self.assertTrue(self.legacy.exists())

    def test_corrupt_record_never_falls_back(self):
        (self.base / "app-location.json").write_text('{"schema": "PRIVATE_FIXTURE"}')
        with self.assertRaises(RuntimeError):
            manage.resolve_app()

    def test_no_record_uses_validated_legacy_app_only(self):
        self.assertEqual(manage.resolve_app(), self.legacy)
        (self.legacy / "Contents/Helpers/BarkUpdateVerifier").chmod(0o600)
        with self.assertRaises(RuntimeError):
            manage.resolve_app()

    def test_invalid_bundle_identity_protocol_helpers_and_key_are_rejected(self):
        for mutation in ("identifier", "protocol", "helper", "key"):
            with self.subTest(mutation=mutation):
                app = make_app(self.root / mutation / "Bark.app")
                if mutation == "identifier":
                    file = app / "Contents/Info.plist"
                    info = plistlib.loads(file.read_bytes())
                    info["CFBundleIdentifier"] = "unrelated.app"
                    file.write_bytes(plistlib.dumps(info))
                elif mutation == "protocol":
                    (app / "Contents/Resources/release.json").write_text('{"update_protocol":2}')
                elif mutation == "helper":
                    (app / "Contents/Helpers/Codex Bark Panel.app/Contents/MacOS/CodexBarkPanel").unlink()
                else:
                    (app / "Contents/Resources/update-public-key.txt").write_text("other-public-key")
                with self.assertRaises(RuntimeError):
                    manage.register_app(app)
        self.assertFalse((self.base / "app-location.json").exists())

    def test_generated_update_and_symlink_candidates_are_rejected(self):
        candidates = [make_app(self.root / ".Bark-update-backup-test.app"),
                      make_app(self.base / "updates/stage/Bark.app"),
                      make_app(self.root / ".Trash/Bark.app"),
                      make_app(self.root / ".Trashes/501/Bark.app")]
        link = self.root / "Shortcut.app"
        link.symlink_to(self.app)
        candidates.append(link)
        for app in candidates:
            with self.subTest(path=app), self.assertRaises(RuntimeError):
                manage.register_app(app)

    def test_active_swap_pins_destination_and_commit_refreshes_old_inode_bookmark(self):
        manage.register_app(self.app)
        transaction = self.transaction(self.app)
        sibling = self.app.with_name(".Bark-update-" + transaction["id"] + ".app")
        self.app.rename(sibling)
        make_app(self.app, "2.6.3")
        # macOS may resolve a replacement at the old path or follow the old inode.
        self.assertIn(manage._resolve_app_bookmark(manage._location_record())[0], (self.app, sibling))
        self.assertEqual(manage.resolve_app(), self.app)
        with self.assertRaises(RuntimeError):
            manage.register_app(self.legacy)
        manage.register_app(self.app, migrate_login=False)
        self.transaction(self.app, phase="complete")
        self.assertEqual(manage.resolve_app(), self.app)
        self.assertEqual(manage._resolve_app_bookmark(manage._location_record())[0], self.app)

    def test_active_transaction_cannot_select_an_unrelated_installation(self):
        manage.register_app(self.app)
        self.transaction(self.legacy)
        with self.assertRaises(RuntimeError):
            manage.resolve_app()

    def test_legacy_pending_transaction_pins_existing_manifest_location(self):
        transaction = self.transaction(self.app)
        del transaction["app_path"]
        transaction["runtime_present"] = ["bark.py", "manage.py"]
        file = self.base / "updates/transaction.json"
        file.write_text(json.dumps(transaction))
        self.assertEqual(manage.resolve_app(), self.app)
        with self.assertRaises(RuntimeError):
            manage.register_app(self.legacy)
        manage.register_app(self.app)
        self.assertEqual(json.loads(self.manifest.read_text())["menu_app"]["path"], str(self.app))
        transaction["runtime_present"].append("config.json")
        file.write_text(json.dumps(transaction))
        with self.assertRaises(RuntimeError):
            manage.resolve_app()

    def test_login_toggle_uses_stable_interpreter_and_launcher(self):
        manage.register_app(self.app)
        python = self.base / "python/bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text("public fixture interpreter")
        manage.set_login(True)
        self.assertEqual(plistlib.loads(self.login.read_bytes())["ProgramArguments"],
                         [str(python), "-I", "-B", str(self.base / "manage.py"), "launch-app"])
        self.assertEqual(manage._check_login_item(self.login, self.app)["status"], "pass")
        manage.set_login(False)
        self.assertFalse(self.login.exists())

    def test_status_version_and_uninstall_follow_moved_bundle(self):
        manage.register_app(self.app)
        moved = self.root / "Actually Here.app"
        self.app.rename(moved)
        report = manage.support_report(lambda: {}, lambda: {})
        self.assertEqual(report["report"]["app_version"], "2.6.2")
        with mock.patch.object(manage.subprocess, "run"), contextlib.redirect_stdout(io.StringIO()):
            manage.uninstall()
        self.assertFalse(moved.exists())
        self.assertTrue(self.legacy.exists())

    def test_cli_returns_only_selected_path_or_generic_failure(self):
        script = self.base / "manage.py"
        shutil.copyfile(fixtures.SRC / "manage.py", script)
        fake_home = self.root / "empty-home"
        fake_home.mkdir()
        environment = dict(os.environ, HOME=str(fake_home))
        command = ["/usr/bin/python3", "-I", "-B", str(script)]
        registered = subprocess.run(command + ["app-register", "--app", str(self.app)],
                                    env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(registered.stdout), {"ok": True, "path": str(self.app)})
        moved = self.root / "CLI Renamed.app"
        self.app.rename(moved)
        located = subprocess.run(command + ["app-locate"], env=environment,
                                 capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(located.stdout), {"ok": True, "path": str(moved)})
        failed = subprocess.run(command + ["app-register", "--app", str(self.root / "PRIVATE_FIXTURE.app")],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout), {"ok": False, "error": "app_location_unavailable"})
        self.assertNotIn("PRIVATE_FIXTURE", failed.stdout + failed.stderr)


if __name__ == "__main__":
    unittest.main()
