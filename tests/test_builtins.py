import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures

class BuiltinTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown
    def test_presets_preserve_policies_and_device_secret(self):
        self.m.set_route('codex',{'mode':'selected','device_ids':['primary']})
        self.m.set_quiet({'enabled':True,'start':'23:00','end':'08:00'})
        self.m.set_source_mute('mac-battery',3600)
        config=self.m.CONFIG.read_bytes(); sources=self.m.source_list()
        before=self.m.rules()
        self.m.apply_preset('need');self.assertEqual(self.m.preset_state(self.m.rules()),'need')
        self.assertFalse(self.m.rules()['codex_events']['complete'])
        self.m.apply_preset('focus',3600);self.assertEqual(self.m.preset_state(self.m.rules()),'focus')
        self.m.apply_preset('away');self.assertEqual(self.m.preset_state(self.m.rules()),'away')
        after=self.m.rules()
        for key in ['quiet_hours','routes','codex_preview']:self.assertEqual(before[key],after[key])
        self.assertEqual(before['source_mute_until']['mac-battery'],after['source_mute_until']['mac-battery'])
        self.assertEqual(config,self.m.CONFIG.read_bytes());self.assertEqual(sources,self.m.source_list())
    def test_disabled_and_expiry_and_custom(self):
        self.m.change_source('codex',enabled=False)
        self.m.apply_preset('away')
        with patch.object(self.m,'send_target') as send:self.assertEqual(self.m.deliver(self.event),'source_disabled');send.assert_not_called()
        self.m.apply_preset('focus',900)
        d=self.m.rules()
        with patch.object(self.m.time,'time',return_value=d['source_mute_until']['codex']+1):self.assertEqual(self.m.preset_state(d),'away')
        self.m.set_codex_events({'question':False});self.assertEqual(self.m.preset_state(self.m.rules()),'focus')
        self.m.set_source_mute('codex',0);self.assertEqual(self.m.preset_state(self.m.rules()),'custom')
    def test_invalid_preset_has_no_writes(self):
        self.m.apply_preset('away');before=self.m.RULES.read_bytes()
        for mode,seconds in [('bad',900),('focus',-1),('focus',True),('focus',0)]:
            with self.assertRaises(ValueError):self.m.apply_preset(mode,seconds)
        self.assertEqual(before,self.m.RULES.read_bytes())
    def test_template_registration_preserves_disabled_existing_source(self):
        result=self.m.enable_template('terminal');self.assertEqual(result['source_id'],'terminal-task')
        self.m.change_source('terminal-task',enabled=False)
        self.m.enable_template('terminal')
        self.assertFalse(next(x['enabled'] for x in self.m.source_list() if x['id']=='terminal-task'))
        self.assertEqual(len([x for x in self.m.source_list() if x['id']=='terminal-task']),1)
    def test_task_labels_status_and_privacy(self):
        self.m.enable_template('terminal')
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver(self.m.template_event('terminal',1)),'accepted')
            self.assertIn('执行失败',send.call_args.args[1]);self.assertNotIn('Codex',send.call_args.args[1])
        self.assertNotIn('task_success',self.m.LOG.read_text())
        with self.assertRaises(ValueError):self.m.template_event('claude',0)
    def test_claude_does_not_query_codex_db_or_send_response(self):
        self.m.enable_template('claude')
        ev={'kind':'complete','source_id':'claude-code','session':'claude','fingerprint':'hash','project_label':'Demo'}
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send,patch.object(self.m,'chat_name',side_effect=AssertionError('must not query Codex')):
            self.assertEqual(self.m.deliver(ev),'accepted');self.assertIn('Claude',send.call_args.args[1])
        self.assertNotIn('Demo',self.m.LOG.read_text())
    def test_wrapper_exit_code_spaces_and_notifier_failure(self):
        wrapper=fixtures.SRC/'bark-run';shutil.copy2(wrapper,self.base/'bark-run');os.chmod(self.base/'bark-run',0o700)
        # Failure inside the notifier must not replace the wrapped program's exit code.
        (self.base/'bark.py').write_text('raise RuntimeError("test failure")\n')
        r=subprocess.run([str(self.base/'bark-run'),'/bin/sh','-c','printf "%s" "$1"; exit 7','ignored','literal spaces ; $HOME'],capture_output=True,text=True,timeout=2)
        self.assertEqual(r.returncode,7);self.assertEqual(r.stdout,'literal spaces ; $HOME');self.assertEqual(r.stderr,'')

    def test_claude_removal_requires_detach_success(self):
        self.m.enable_template('claude');(self.base/'claude_bark.py').write_text('# isolated stub')
        with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'{"ok":false}',b'')):
            with self.assertRaises(ValueError):self.m.change_source('claude-code',remove=True)
        self.assertTrue(any(x['id']=='claude-code' for x in self.m.source_list()))
        with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'{"ok":true}',b'')) as detach:
            self.m.change_source('claude-code',remove=True)
            self.assertIn('detach',detach.call_args.args[0])
        self.assertFalse(any(x['id']=='claude-code' for x in self.m.source_list()))
