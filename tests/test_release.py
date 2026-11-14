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

    def test_invalid_cli_override_is_rejected(self):
        with patch.dict('os.environ',{'CODEX_CLI_BIN':'relative-codex'}):
            with self.assertRaises(RuntimeError):module('release_invalid_cli',ROOT/'src/runtime/manage.py')
