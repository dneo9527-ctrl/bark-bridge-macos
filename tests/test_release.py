import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class ReleaseTests(unittest.TestCase):
    def manager(self, directory):
        m = module('release_manage', ROOT/'src/runtime/manage.py')
        m.BASE = directory/'runtime'
        m.BASE.mkdir(mode=0o700)
        m.MANIFEST = m.BASE/'installation.json'
        return m

    def test_hook_command_uses_checkout_path_and_shell_quoting(self):
        import shlex
        m = module('release_command', ROOT/'src/runtime/manage.py')
        self.assertEqual(shlex.split(m.CMD), ['/usr/bin/python3', str(m.BASE/'bark.py'), 'hook'])

    def test_owned_hook_recognizes_both_interpreters_and_excludes_other_commands(self):
        import shlex
        with tempfile.TemporaryDirectory(prefix='bark hooks ') as temporary:
            root=Path(temporary);m=self.manager(root)
            script=shlex.quote(str(m.BASE/'bark.py'))
            for interpreter in ['/usr/bin/python3',str(m.BASE/'python/bin/python3')]:
                self.assertTrue(m.is_our_command(shlex.quote(interpreter)+' '+script+' hook'))
            for command in ['evil '+script+' hook', '/usr/bin/python3 '+script+' notify',
                            '/usr/bin/python3 '+script+' hook --extra',
                            '/usr/bin/python3 '+shlex.quote(str(root/'other/bark.py'))+' hook',None]:
                self.assertFalse(m.is_our_command(command))

    def test_uninstall_removes_both_owned_interpreters_and_keeps_third_party(self):
        import shlex
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);m=self.manager(root);home=root/'home';home.mkdir()
            commands=[shlex.quote(interpreter)+' '+shlex.quote(str(m.BASE/'bark.py'))+' hook'
                      for interpreter in ['/usr/bin/python3',str(m.BASE/'python/bin/python3')]]
            commands.append('/usr/bin/true')
            (home/'hooks.json').write_text(json.dumps({'hooks':{'Stop':[{'hooks':[{'command':command} for command in commands]}]}}))
            manifest={'homes':{str(home):{'hooks_existed':True,'trust':{},'skill_link':str(home/'skills/codex-bark-wait')}},'backups':[]}
            m.save_manifest(manifest)
            with contextlib.redirect_stdout(io.StringIO()):m.uninstall()
            remaining=json.loads((home/'hooks.json').read_text())['hooks']['Stop'][0]['hooks']
            self.assertEqual(remaining,[{'command':'/usr/bin/true'}])

    def test_failed_attach_restores_original_hooks_and_removes_new_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary); m=self.manager(directory)
            home=directory/'codex';home.mkdir()
            original='{"hooks":{"Stop":[{"hooks":[{"command":"other-hook"}]}]}}'
            (home/'hooks.json').write_text(original)
            with patch.object(m,'hooks_list', side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):m.attach(home)
            self.assertEqual((home/'hooks.json').read_text(),original)
            self.assertFalse((home/'config.toml').exists())

    def test_attach_does_not_touch_cc_switch_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);m=self.manager(directory)
            (m.BASE/'skill').mkdir()
            fake={'errors':[],'warnings':[],'hooks':[{'command':m.CMD,'key':'key'+str(i),'currentHash':'hash','trustStatus':'trusted'} for i in range(4)]}
            with patch.object(m,'hooks_list',return_value=fake), patch.object(m,'update_common') as common, contextlib.redirect_stdout(io.StringIO()):
                m.attach(directory/'codex')
            common.assert_not_called()

    def test_explicit_cc_switch_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);m=self.manager(directory)
            (m.BASE/'skill').mkdir()
            fake={'errors':[],'warnings':[],'hooks':[{'command':m.CMD,'key':'key'+str(i),'currentHash':'hash','trustStatus':'trusted'} for i in range(4)]}
            with patch.object(m,'hooks_list',return_value=fake), patch.object(m,'update_common',return_value=True) as common, contextlib.redirect_stdout(io.StringIO()):
                m.attach(directory/'codex',with_cc_switch=True)
            common.assert_called_once()
            self.assertTrue(json.loads(m.MANIFEST.read_text())['cc_switch_common_updated'])

    def test_installer_refuses_existing_runtime_without_touching_it(self):
        installer=module('release_install',ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'runtime';base.mkdir()
            sentinel=base/'sentinel';sentinel.write_text('preserve')
            with self.assertRaises(RuntimeError):installer.install(root/'Bark.app',base)
            self.assertEqual(sentinel.read_text(),'preserve')

    def make_payload(self, root):
        import plistlib
        payload = root/'payload'
        (payload/'Bark.app/Contents').mkdir(parents=True)
        (payload/'Bark.app/Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'local.codex.bark.menu', 'CFBundleShortVersionString': '2.5.0'}))
        (payload/'runtime').mkdir()
        for name in ['bark.py','manage.py','claude_bark.py','bark-run']:
            (payload/'runtime'/name).write_text('# public fixture\n')
        (payload/'SKILL.md.in').write_text('@BARK_PYTHON@ "@BARK_BASE@/bark.py" wait')
        (payload/'python/bin').mkdir(parents=True)
        (payload/'python/bin/python3.13').write_text('fixture')
        (payload/'python/bin/python3').symlink_to('python3.13')
        return payload

    def test_binary_install_uses_shared_bundled_interpreter_and_preserves_symlink(self):
        import shlex
        installer=module('binary_install', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory(prefix='bark install ') as temporary:
            root=Path(temporary);payload=self.make_payload(root)
            app=root/'Bark.app';base=root/'runtime';home=root/'codex'
            with patch.object(installer.subprocess,'run') as run:
                result=installer.install(app,base,home,payload=payload)
            self.assertTrue(result['codex_attached'])
            self.assertTrue((base/'python/bin/python3').is_symlink())
            self.assertEqual(shlex.split((base/'skill/SKILL.md').read_text())[:2],
                             [str(base/'python/bin/python3'),str(base/'bark.py')])
            self.assertEqual(run.call_args.args[0][:2], [str(base/'python/bin/python3'),str(base/'manage.py')])
            self.assertEqual(base.stat().st_mode & 0o777, 0o700)
            self.assertEqual((base/'installation.json').stat().st_mode & 0o777, 0o600)

    def test_binary_install_does_not_remove_app_created_concurrently(self):
        installer=module('binary_race', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);payload=self.make_payload(root)
            app=root/'Bark.app';base=root/'runtime'
            def race(*args, **kwargs):
                app.mkdir();(app/'sentinel').write_text('other installation')
            with patch.object(installer.subprocess,'run', side_effect=race):
                with self.assertRaises(FileExistsError):installer.install(app,base,payload=payload)
            self.assertEqual((app/'sentinel').read_text(),'other installation')

    def test_binary_install_failure_preserves_runtime_backups_and_removes_owned_app(self):
        installer=module('binary_failure', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);payload=self.make_payload(root)
            app=root/'Bark.app';base=root/'runtime'
            def run(args, **kwargs):
                if args[0]=='/usr/bin/ditto':
                    import shutil
                    shutil.copytree(payload/'Bark.app',app,dirs_exist_ok=True)
                elif args[0]==str(base/'python/bin/python3'):
                    (base/'backups').mkdir();(base/'backups/original').write_text('keep')
                    raise RuntimeError('attach failed')
            with patch.object(installer.subprocess,'run',side_effect=run):
                with self.assertRaises(RuntimeError):installer.install(app,base,root/'codex',payload=payload)
            self.assertFalse(app.exists())
            self.assertEqual((base/'backups/original').read_text(),'keep')

    def test_installer_refuses_dangling_destination_links(self):
        installer=module('binary_symlink', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'runtime';base.symlink_to(root/'missing')
            with self.assertRaises(RuntimeError):installer.install(root/'Bark.app',base)
            self.assertTrue(base.is_symlink())

    def test_incomplete_app_copy_is_removed_but_runtime_is_preserved(self):
        installer=module('binary_partial', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);payload=self.make_payload(root);app=root/'Bark.app';base=root/'runtime'
            def run(args,**kwargs):
                if args[0]=='/usr/bin/ditto':
                    (app/'partial-file').write_text('partial')
                    raise RuntimeError('copy interrupted')
            with patch.object(installer.subprocess,'run',side_effect=run):
                with self.assertRaises(RuntimeError):installer.install(app,base,payload=payload)
            self.assertFalse(app.exists());self.assertTrue((base/'manage.py').exists())

    def test_invalid_cli_override_is_rejected(self):
        with patch.dict('os.environ',{'CODEX_CLI_BIN':'relative-codex'}):
            with self.assertRaises(RuntimeError):module('release_invalid_cli',ROOT/'src/runtime/manage.py')
