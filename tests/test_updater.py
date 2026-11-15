import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('update_tests', ROOT/'src/runtime/updater.py')
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)


def app_fixture(app, version='2.5.0', tag='v2.5.0-preview.1', build='45'):
    (app/'Contents/Resources').mkdir(parents=True)
    (app/'Contents/Helpers').mkdir()
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(
        CFBundleIdentifier='local.codex.bark.menu', CFBundleShortVersionString=version, CFBundleVersion=build)))
    (app/'Contents/Resources/release.json').write_text(json.dumps(dict(tag=tag, channel='preview', update_protocol=1)))
    (app/'Contents/Helpers/BarkUpdateVerifier').write_text('fixture')


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root/'Bark.app'; app_fixture(self.app)
        self.base = self.root/'runtime'; self.base.mkdir()
        self.up = u.Updater(self.app,self.base)

    def stage(self):
        stage = self.up.root/'stage'; stage.mkdir()
        payload = stage/'payload'; payload.mkdir()
        app_fixture(payload/'Bark.app','2.6.0','v2.6.0-preview.1','46')
        (payload/'runtime').mkdir()
        for name in u.RUNTIME: (payload/'runtime'/name).write_text('# new public program\n')
        files = {}
        for path in payload.rglob('*'):
            if path.is_file():
                path.chmod(0o700)
                files[str(path.relative_to(payload))] = {'sha256':u.digest(path),'mode':0o700}
        manifest = dict(schema=1, repository=u.REPOSITORY, version='2.6.0', tag='v2.6.0-preview.1',
                        build=46, arch=self.up.arch, min_os=13, runtime_protocol=1,
                        asset='Bark-Bridge-2.6.0-preview.1-macOS-'+self.up.arch+'-update.zip', size=100, sha256='a'*64, files=files)
        u.write_json(stage/'manifest.json',manifest)
        (stage/'manifest.sig').write_text('fixture')
        u.write_json(self.up.state_path,{'status':'ready','tag':manifest['tag']})
        return manifest

    def test_version_selection_rejects_unrecognized_tags_and_orders_prereleases(self):
        self.assertLess(u.version_key('v2.6.0-preview.2'),u.version_key('v2.6.0'))
        self.assertLess(u.version_key('v2.6.0-preview.2'),u.version_key('v2.6.0-preview.10'))
        for bad in ('v2.6','latest','v2.6.0/../x','2.6.0-rc.1',None):
            with self.assertRaises(u.UpdateError): u.version_key(bad)

    def test_default_off_and_due_check_does_not_network(self):
        with patch.object(u,'fetch',side_effect=AssertionError('network')):
            self.assertFalse(self.up.check(due=True)['automatic'])
        self.up.set_automatic(True)
        self.assertTrue(u.Updater(self.app,self.base).status()['automatic'])
        self.assertEqual(stat.S_IMODE(self.up.prefs_path.stat().st_mode),0o600)

    def test_network_failure_is_classified_and_throttled_for_background(self):
        self.up.set_automatic(True)
        with patch.object(u,'fetch',side_effect=u.UpdateError('network_error')) as fetch:
            self.assertEqual(self.up.check(due=True)['error'],'network_error')
            self.up.check(due=True)
            self.assertEqual(fetch.call_count,1)

    def test_newest_applicable_signed_release_wins_and_stable_filters_preview(self):
        releases = [dict(tag_name=tag, prerelease=preview, assets=[{'name':'bark-update-'+self.up.arch+ext} for ext in ('.json','.sig')])
                    for tag,preview in [('v2.5.1',False),('v2.7.0-preview.1',True)]]
        def fetch(url,limit,*args):
            return json.dumps(releases).encode() if url==u.API else b'fixture'
        with patch.object(u,'fetch',side_effect=fetch),patch.object(self.up,'signed_manifest',return_value={'size':123}):
            self.assertEqual(self.up.check()['tag'],'v2.7.0-preview.1')
            release=self.app/'Contents/Resources/release.json'
            release.write_text(json.dumps(dict(tag='v2.5.0',channel='stable')))
            self.assertEqual(self.up.check()['tag'],'v2.5.1')

    def test_signature_failure_leaves_no_installable_stage(self):
        releases=[dict(tag_name='v2.6.0-preview.1',prerelease=True,assets=[{'name':'bark-update-'+self.up.arch+ext} for ext in ('.json','.sig')])]
        with patch.object(u,'fetch',side_effect=lambda url,*args:json.dumps(releases).encode() if url==u.API else b'bad'),patch.object(self.up,'verify_signature',side_effect=u.UpdateError('signature_invalid')):
            result=self.up.check(download=True)
        self.assertEqual(result['error'],'signature_invalid')
        self.assertFalse((self.up.root/'stage').exists())

    def test_manifest_rejects_other_repository_arch_duplicate_keys_and_data_paths(self):
        manifest=self.stage()
        for field,value in [('repository','someone/else'),('arch','unsupported'),('size',u.MAX_ARCHIVE+1)]:
            bad=dict(manifest);bad[field]=value
            with self.assertRaises(u.UpdateError):u.manifest_data(json.dumps(bad).encode(),manifest['tag'],self.up.arch)
        for bad_name in ('../outside','Bark.app/Contents/../../escape','runtime/config.json','Bark.app//Contents/foo'):
            bad=json.loads(json.dumps(manifest));bad['files'][bad_name]={'sha256':'a'*64,'mode':0o700}
            with self.assertRaises(u.UpdateError):u.manifest_data(json.dumps(bad).encode(),manifest['tag'],self.up.arch)
        with self.assertRaises(u.UpdateError):u.manifest_data(b'{"schema":1,"schema":1}',manifest['tag'],self.up.arch)

    def test_zip_rejects_extra_files_symlinks_and_corrupted_contents(self):
        path=self.root/'archive.zip'
        for kind in ('extra','symlink','corrupt'):
            path.unlink(missing_ok=True)
            with zipfile.ZipFile(path,'w') as archive:
                entry=zipfile.ZipInfo('runtime/bark.py' if kind!='extra' else '../escape')
                entry.create_system=3;entry.external_attr=(stat.S_IFLNK|0o777)<<16 if kind=='symlink' else (stat.S_IFREG|0o600)<<16
                archive.writestr(entry,b'bad')
            with self.assertRaises(u.UpdateError):u.unpack(path,self.root/('extracted-'+kind),{'files':{'runtime/bark.py':{'mode':0o700,'sha256':hashlib.sha256(b'good').hexdigest()}}})
        self.assertFalse((self.root/'escape').exists())

    def transaction(self,fault=None):
        with patch.object(self.up,'verify_signature'),patch.object(self.up,'validate_payload'):
            return self.up.apply(restart=False,fault=fault)

    def test_transaction_preserves_private_data_and_unchanged_entry_paths(self):
        self.stage()
        private = ('config.json','rules.json','sources.json','installation.json','state.sqlite3','skill/SKILL.md')
        for name in private:
            path=self.base/name;path.parent.mkdir(exist_ok=True);path.write_bytes(b'FAKE_PRIVATE_SENTINEL')
        for name in u.RUNTIME: (self.base/name).write_text('# old public program\n')
        self.transaction()
        self.assertEqual(self.up.installed()['tag'],'v2.6.0-preview.1')
        for name in private:self.assertEqual((self.base/name).read_bytes(),b'FAKE_PRIVATE_SENTINEL')
        self.assertEqual((self.base/'bark.py').read_text(),'# new public program\n')
        txn=u.read_json(self.up.transaction_path)
        self.assertEqual(txn['phase'],'complete')
        self.assertEqual((self.up.root/txn['id']/'runtime/bark.py').read_text(),'# old public program\n')

    def test_failure_each_mutation_point_rolls_back_exact_old_programs(self):
        for point in ('before_swap','after_swap','runtime:bark.py','runtime:updater.py','before_commit'):
            with self.subTest(point=point):
                if (self.up.root/'stage').exists():shutil.rmtree(self.up.root/'stage')
                self.stage()
                for name in u.RUNTIME:(self.base/name).write_text('# old '+name)
                def fail(here):
                    if here==point:raise RuntimeError('injected')
                with self.assertRaises(RuntimeError):self.transaction(fail)
                self.assertEqual(self.up.installed()['version'],'2.5.0')
                for name in u.RUNTIME:self.assertEqual((self.base/name).read_text(),'# old '+name)
                self.assertEqual(u.read_json(self.up.transaction_path)['phase'],'rolled_back')

    def test_interruption_after_swap_recovers_on_next_start(self):
        self.stage();(self.base/'bark.py').write_text('# original')
        class SimulatedPowerLoss(BaseException):pass
        def crash(point):
            if point=='runtime:bark.py':raise SimulatedPowerLoss()
        with self.assertRaises(SimulatedPowerLoss):self.transaction(crash)
        self.assertEqual(self.up.installed()['version'],'2.6.0')
        self.up.recover()
        self.assertEqual(self.up.installed()['version'],'2.5.0')
        self.assertEqual((self.base/'bark.py').read_text(),'# original')
        self.assertFalse((self.base/'updater.py').exists())

    def test_replay_and_downgrade_are_rejected_before_mutation(self):
        self.stage()
        release=self.app/'Contents/Resources/release.json'
        release.write_text(json.dumps(dict(tag='v2.6.0-preview.1',channel='preview')))
        with patch.object(self.up,'verify_signature'),self.assertRaisesRegex(u.UpdateError,'not_newer'):
            self.up.apply(restart=False)
        self.assertFalse(self.up.transaction_path.exists())

    def test_exclusive_update_lock_does_not_block_status_or_sender(self):
        with self.up.lock():
            self.assertTrue(self.up.status()['ok'])
            with self.assertRaisesRegex(u.UpdateError,'busy'):self.up.set_automatic(True)
        self.assertFalse((self.base/'.config.lock').exists())

    def test_renamed_running_binary_uses_launch_path_without_reading_arguments(self):
        with patch.object(u.ctypes,'CDLL') as library,patch.object(u.subprocess,'check_output',return_value='/Applications/Bark.app/Contents/MacOS/CodexBarkMenu\n') as ps:
            library.return_value.proc_pidpath.return_value=0
            self.assertEqual(u.process_path(123),'/Applications/Bark.app/Contents/MacOS/CodexBarkMenu')
            self.assertEqual(ps.call_args.args[0],['/bin/ps','-p','123','-o','comm='])

    def test_live_menu_is_stopped_before_app_swap(self):
        self.stage()
        order=[]
        real_swap=u.swap_directories
        def swap(left,right):order.append('swap');return real_swap(left,right)
        def record_stop():order.append('stop')
        with patch.object(self.up,'verify_signature'),patch.object(self.up,'validate_payload'),patch.object(self.up,'stop_menu',side_effect=record_stop),patch.object(self.up,'restart_menu'),patch.object(u,'swap_directories',side_effect=swap),patch.object(u,'process_path',return_value=str(self.app/'Contents/MacOS/CodexBarkMenu')):
            u.write_json(self.up.root/'ready.json',{'tag':'v2.6.0-preview.1','pid':123})
            # readiness is cleared immediately before launch; mock the launch receipt.
            def ready():u.write_json(self.up.root/'ready.json',{'tag':'v2.6.0-preview.1','pid':123})
            self.up.restart_menu.side_effect=ready
            self.up.apply(restart=True)
        self.assertEqual(order[:2],['stop','swap'])


if __name__=='__main__':unittest.main()
