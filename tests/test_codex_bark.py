import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SRC = Path(os.environ.get('BARK_TEST_SOURCE', str(Path(__file__).resolve().parents[1] / 'src/runtime')))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        os.chmod(self.base, 0o700)
        shutil.copy2(SRC / 'bark.py', self.base / 'bark.py')
        self.m = module('bark_test', self.base / 'bark.py')
        self.secret = 'LOCAL_TEST_KEY_NEVER_REAL'
        self.m.atomic_json(self.m.CONFIG, {'server': 'https://api.day.app', 'device_key': self.secret, 'enabled': True})
        self.event = {'kind': 'permission', 'session': 'session_hash', 'fingerprint': 'request_hash'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_payload_privacy_and_permission_neutrality(self):
        event = self.m.event_from_hook({'hook_event_name': 'PermissionRequest', 'session_id': 's',
                                       'turn_id': 't', 'tool_name': 'Bash',
                                       'tool_input': {'command': 'PRIVATE_COMMAND'}})
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')) as run:
            self.assertEqual(self.m.deliver(event), 'accepted')
            args, kwargs = run.call_args
            self.assertNotIn(self.secret, str(args))
            self.assertNotIn('PRIVATE_COMMAND', str(args) + str(kwargs))
            self.assertEqual(json.loads(kwargs['input'])['device_key'], self.secret)
            self.assertEqual(kwargs['timeout'], 9)
        self.assertNotIn(self.secret, self.m.LOG.read_text())
        self.assertNotIn('PRIVATE_COMMAND', self.m.LOG.read_text())

    def test_concurrent_duplicate_suppression(self):
        self.m.claim({'kind': 'test', 'session': 'warmup', 'fingerprint': 'warmup'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.m.claim(self.event), range(16)))
        self.assertEqual(results.count(True), 1)

    def test_question_hook_and_skill_coalesce(self):
        self.assertTrue(self.m.claim({'kind': 'question', 'session': 'same', 'fingerprint': 'skill'}))
        self.assertFalse(self.m.claim({'kind': 'question', 'session': 'same', 'fingerprint': 'hook'}))
        self.assertTrue(self.m.claim({'kind': 'question', 'session': 'another', 'fingerprint': 'hook'}))
        self.assertTrue(self.m.claim({'kind': 'permission', 'session': 'same', 'fingerprint': 'permission'}))

    def test_failure_never_returns_hook_decision(self):
        self.m.atomic_json(self.m.CONFIG, {'server': 'https://127.0.0.1:9', 'device_key': self.secret, 'enabled': True})
        start = time.monotonic()
        r = subprocess.run(['/usr/bin/python3', str(self.base / 'bark.py'), 'hook'],
                           input=json.dumps({'hook_event_name': 'PermissionRequest', 'session_id': 's', 'tool_name': 'Bash', 'tool_input': {}}),
                           text=True, capture_output=True, timeout=1)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        for _ in range(40):
            if self.m.LOG.exists():
                break
            time.sleep(.05)
        self.assertIn('send_failed', self.m.LOG.read_text())

    def test_unsafe_permissions_skip_send(self):
        os.chmod(self.m.CONFIG, 0o644)
        with patch.object(self.m.subprocess, 'run') as run:
            self.assertEqual(self.m.deliver(self.event), 'send_failed')
            run.assert_not_called()

    def test_pause_retains_key_and_prevents_network(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.set_enabled(False)
            self.m.status()
        self.assertNotIn(self.secret, output.getvalue())
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['configured'])
        self.assertFalse(self.m.config()['enabled'])
        self.assertEqual(self.m.config()['devices'][0]['device_key'], self.secret)
        with patch.object(self.m.subprocess, 'run') as run:
            self.assertEqual(self.m.deliver(self.event), 'disabled')
            run.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.set_enabled(True)
        self.assertTrue(self.m.config()['enabled'])
        self.assertEqual(self.m.CONFIG.stat().st_mode & 0o777, 0o600)

    def test_timed_pause_blocks_then_expires_without_running_ui(self):
        with patch.object(self.m.time, 'time', return_value=1000):
            self.m.pause_for(900)
            self.assertFalse(self.m.effective_enabled(self.m.config()))
            with patch.object(self.m.subprocess, 'run') as run:
                self.assertEqual(self.m.deliver(self.event), 'disabled')
                run.assert_not_called()
        with patch.object(self.m.time, 'time', return_value=1901):
            self.assertTrue(self.m.effective_enabled(self.m.config()))
            with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')):
                self.assertEqual(self.m.deliver(self.event), 'accepted')
        self.assertEqual(self.m.config()['devices'][0]['device_key'], self.secret)

    def test_manual_switch_cancels_timed_resume(self):
        self.m.pause_for(900)
        with contextlib.redirect_stdout(io.StringIO()): self.m.set_enabled(False)
        self.assertNotIn('resume_at', self.m.config())
        self.assertFalse(self.m.effective_enabled(self.m.config()))
        with contextlib.redirect_stdout(io.StringIO()): self.m.set_enabled(True)
        self.assertTrue(self.m.effective_enabled(self.m.config()))

    def test_pause_status_is_private_and_reports_deadline(self):
        with patch.object(self.m.time, 'time', return_value=1000):
            self.m.pause_for(3600)
            with contextlib.redirect_stdout(io.StringIO()) as out: self.m.status()
            d = json.loads(out.getvalue())
            self.assertFalse(d['enabled'])
            self.assertEqual(d['resume_at'], 4600)
            self.assertNotIn(self.secret, out.getvalue())

    def test_gui_configuration_uses_private_stdin(self):
        value = 'curl -X GET https://example.test/' + self.secret + '/title/body?group=example&ttl=600'
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({'address': value}).encode()))
        with patch.object(self.m.sys, 'stdin', stdin), patch.object(self.m, 'deliver', return_value='accepted'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.m.configure_json()
        self.assertEqual(json.loads(output.getvalue()), {'ok': True, 'result': 'accepted'})
        self.assertNotIn(self.secret, output.getvalue())
        self.assertEqual(self.m.config()['devices'][-1]['server'], 'https://example.test')
        self.assertEqual(len(self.m.config()['devices']), 2)
        self.assertEqual(self.m.config()['devices'][0]['device_key'], self.secret)
        self.assertEqual(self.m.CONFIG.stat().st_mode & 0o777, 0o600)

    def test_other_application_uses_same_private_sender(self):
        event = {'kind': 'application', 'source': 'Example App', 'session': 'app', 'fingerprint': 'event'}
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')) as run:
            self.assertEqual(self.m.deliver(event), 'accepted')
            payload = json.loads(run.call_args.kwargs['input'])
            self.assertIn('Example App', payload['body'])
            self.assertEqual(payload['group'], 'Example App')
            self.assertEqual(payload['device_key'], self.secret)
            self.assertNotIn(self.secret, str(run.call_args.args))
        self.assertNotIn(self.secret, self.m.LOG.read_text())

    def test_filter_and_session_start(self):
        for name in ('request_user_input', 'request_user_input_async', 'functions.request_user_input', 'mcp__example__ask_user_question'):
            self.assertIsNotNone(self.m.event_from_hook({'hook_event_name': 'PreToolUse', 'tool_name': name}))
        self.assertIsNone(self.m.event_from_hook({'hook_event_name': 'PreToolUse', 'tool_name': 'exec_command'}))
        r = subprocess.run(['/usr/bin/python3', str(self.base / 'bark.py'), 'hook'], input='{"hook_event_name":"SessionStart"}',
                           text=True, capture_output=True, timeout=1)
        d = json.loads(r.stdout)
        self.assertEqual(list(d), ['hookSpecificOutput'])
        self.assertIn('codex-bark-wait', d['hookSpecificOutput']['additionalContext'])


class SourceTests(unittest.TestCase):
    setUp = NotifierTests.setUp
    tearDown = NotifierTests.tearDown
    # Exercise source routing with actual metadata files and the real shared sender boundary.
    def add_app(self):
        app = self.base / 'Chosen App.app'
        (app / 'Contents').mkdir(parents=True, exist_ok=True)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'test.chosen.app', 'CFBundleName': 'Chosen App'}))
        self.m.add_sources([str(app)])
        return 'test.chosen.app'

    def test_source_add_is_idempotent_and_private(self):
        sid = self.add_app()
        self.add_app()
        entries = self.m.source_list()
        self.assertEqual({x["id"] for x in entries}, {"codex", "mac-battery", sid})
        self.assertEqual(next(x for x in entries if x['id']==sid)['status'], 'pending')
        self.assertEqual(next(x for x in entries if x['id']==sid)['id'], sid)
        self.assertNotIn(self.secret, self.m.SOURCES.read_text())
        self.assertEqual(self.m.SOURCES.stat().st_mode & 0o777, 0o600)

    def test_disabled_source_blocks_network_and_preserves_others(self):
        sid = self.add_app()
        self.m.change_source(sid, enabled=False)
        with patch.object(self.m.subprocess, 'run') as run:
            result = self.m.deliver({'kind': 'application', 'source_id': sid, 'session': 'app', 'fingerprint': 'a'})
            self.assertEqual(result, 'source_disabled')
            run.assert_not_called()
        self.assertTrue(self.m.source_list()[0]['enabled'])

    def test_codex_source_switch_does_not_change_global_config(self):
        self.m.change_source('codex', enabled=False)
        with patch.object(self.m.subprocess, 'run') as run:
            self.assertEqual(self.m.deliver(self.event), 'source_disabled')
            run.assert_not_called()
        self.assertTrue(self.m.config()['enabled'])
        self.assertEqual(self.m.config()['devices'][0]['device_key'], self.secret)

    def test_manual_test_does_not_claim_event_integration(self):
        sid = self.add_app()
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')):
            self.assertEqual(self.m.deliver({'kind': 'source_test', 'source_id': sid, 'origin': 'manual_test'}, force=True), 'accepted')
        self.assertEqual(next(x for x in self.m.source_list() if x['id']==sid)['status'], 'pending')

    def test_real_event_records_observation_and_routes_registered_name(self):
        sid = self.add_app()
        event = {'kind': 'application', 'source_id': sid, 'origin': 'integration', 'session': 'app', 'fingerprint': 'a'}
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')) as run:
            self.assertEqual(self.m.deliver(event), 'accepted')
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['group'], 'Chosen App')
        self.assertEqual(next(x for x in self.m.source_list() if x['id']==sid)['status'], 'observed')
        self.assertIn('last_received', next(x for x in self.m.source_list() if x['id']==sid))

    def test_remove_stops_events_and_builtin_is_protected(self):
        sid = self.add_app()
        self.m.change_source(sid, remove=True)
        with patch.object(self.m.subprocess, 'run') as run:
            self.assertEqual(self.m.deliver({'kind': 'application', 'source_id': sid}), 'unknown_source')
            run.assert_not_called()
        with self.assertRaises(ValueError):
            self.m.change_source('codex', remove=True)


class DeviceTests(unittest.TestCase):
    setUp = NotifierTests.setUp
    tearDown = NotifierTests.tearDown

    def second(self):
        return self.m.add_device('https://example.test/SECOND_FAKE_KEY', '备用手机')['id']

    def test_legacy_migration_preserves_pause_secret_and_private_permissions(self):
        d = json.loads(self.m.CONFIG.read_text())
        d.update(enabled=False, resume_at=123456)
        self.m.atomic_json(self.m.CONFIG, d)
        result = self.m.migrate_devices()
        saved = json.loads(self.m.CONFIG.read_text())
        self.assertEqual(result, {'ok': True, 'count': 1})
        self.assertNotIn('device_key', saved)
        self.assertNotIn('server', saved)
        self.assertEqual(saved['devices'][0]['device_key'], self.secret)
        self.assertEqual(saved['resume_at'], 123456)
        self.assertFalse(saved['enabled'])
        self.assertEqual(self.m.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.m.migrate_devices(), result)

    def test_metadata_never_contains_addresses_or_keys(self):
        self.second()
        data = json.dumps(self.m.device_list())
        for secret in (self.secret, 'SECOND_FAKE_KEY', 'https://', 'example.test', 'api.day.app'):
            self.assertNotIn(secret, data)
        with contextlib.redirect_stdout(io.StringIO()) as out: self.m.status()
        self.assertEqual(json.loads(out.getvalue())['active_device_count'], 2)
        self.assertNotIn(self.secret, out.getvalue())

    def test_duplicate_address_is_remembered_without_duplicate_push(self):
        sid = self.second()
        self.m.change_device(sid, enabled=False)
        added = self.m.add_device('https://example.test/SECOND_FAKE_KEY/title/body?group=a', '新名称')
        self.assertTrue(added['existing'])
        self.assertEqual(added['id'], sid)
        self.assertEqual(len(self.m.device_list()), 2)
        self.assertEqual(self.m.device_list()[1]['name'], '新名称')
        self.assertFalse(self.m.device_list()[1]['enabled'])

    def test_all_active_devices_get_one_private_post_and_shared_event_deduplication(self):
        self.second()
        with patch.object(self.m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')) as run:
            self.assertEqual(self.m.deliver(self.event), 'accepted')
            self.assertEqual(run.call_count, 2)
            payloads = [json.loads(call.kwargs['input']) for call in run.call_args_list]
            self.assertEqual({x['device_key'] for x in payloads}, {self.secret, 'SECOND_FAKE_KEY'})
            for call in run.call_args_list:
                self.assertNotIn(self.secret, str(call.args))
                self.assertNotIn('SECOND_FAKE_KEY', str(call.args))
            self.assertEqual(self.m.deliver(self.event), 'deduplicated')
            self.assertEqual(run.call_count, 2)
        for secret in (self.secret, 'SECOND_FAKE_KEY'):
            self.assertNotIn(secret, self.m.LOG.read_text())

    def test_targets_run_concurrently_and_one_failure_does_not_cancel_another(self):
        import threading
        self.second()
        barrier = threading.Barrier(2, timeout=2)
        def send(device, *args):
            barrier.wait()
            return (device['id'] == 'primary'), 200
        with patch.object(self.m, 'send_target', side_effect=send) as run:
            self.assertEqual(self.m.deliver(self.event), 'partial')
            self.assertEqual(run.call_count, 2)
        row = json.loads(self.m.LOG.read_text().splitlines()[-1])
        self.assertEqual((row['delivered'], row['total']), (1, 2))

    def test_individual_switch_and_targeted_test(self):
        sid = self.second()
        self.m.change_device('primary', enabled=False)
        with patch.object(self.m, 'send_target', return_value=(True, 200)) as run:
            self.assertEqual(self.m.deliver(self.event), 'accepted')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0]['id'], sid)
        self.m.change_device('primary', enabled=True)
        with patch.object(self.m, 'send_target', return_value=(True, 200)) as run:
            self.assertEqual(self.m.deliver({'kind': 'test'}, force=True, device_id=sid), 'accepted')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0]['id'], sid)

    def test_rename_remove_and_all_disabled_are_persistent(self):
        sid = self.second()
        self.m.change_device(sid, name='常用手机')
        self.assertEqual(self.m.device_list()[1]['name'], '常用手机')
        self.m.change_device(sid, remove=True)
        self.assertEqual(len(self.m.device_list()), 1)
        self.m.change_device('primary', enabled=False)
        with patch.object(self.m, 'send_target') as send:
            self.assertEqual(self.m.deliver(self.event), 'no_devices_enabled')
            send.assert_not_called()
        self.m.change_device('primary', remove=True)
        self.assertEqual(self.m.deliver(self.event), 'not_configured')

    def test_add_preserves_global_pause_and_rejects_unsafe_input(self):
        with contextlib.redirect_stdout(io.StringIO()): self.m.set_enabled(False)
        self.second()
        self.assertFalse(self.m.config()['enabled'])
        for address in ['http://example.test/KEY', 'https://user:pass@example.test/KEY', 'https://example.test/']:
            with self.assertRaises(ValueError): self.m.add_device(address)
        self.assertEqual(len(self.m.device_list()), 2)


class ConfigTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("BARK_RUN_CODEX_INTEGRATION") == "1", "explicit local CLI integration only")
    def test_merge_trust_and_surgical_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Path(tmp) / 'shared'
            b.mkdir(mode=0o700)
            (b / 'skill').mkdir()
            shutil.copy2(SRC / 'skill/SKILL.md', b / 'skill/SKILL.md')
            m = module('manager_test', SRC / 'manage.py')
            m.BASE = b
            m.MANIFEST = b / 'installation.json'
            home = Path(tmp) / 'home'
            home.mkdir()
            before = 'approval_policy = "never"\nnotify = ["existing-notifier"]\n'
            (home / 'config.toml').write_text(before)
            existing = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': '/usr/bin/true'}]}]}}
            (home / 'hooks.json').write_text(json.dumps(existing))
            with patch.object(m, 'update_common', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                m.attach(home)
                d = m.hooks_list(home)
                ours = [h for h in d['hooks'] if h.get('command') == m.CMD]
                self.assertEqual(len(ours), 4)
                self.assertTrue(all(h['trustStatus'] == 'trusted' for h in ours))
                self.assertEqual(len(d['hooks']), 5)
                m.attach(home)
                self.assertEqual(len(m.hooks_list(home)['hooks']), 5)
                p = home / 'config.toml'
                p.write_text(p.read_text() + '\n[desktop]\nfuture_setting = true\n')
                m.uninstall()
            self.assertIn(before, p.read_text())
            self.assertIn('future_setting = true', p.read_text())
            self.assertNotIn('trusted_hash', p.read_text())
            self.assertEqual(json.loads((home / 'hooks.json').read_text()), existing)
            self.assertFalse((home / 'skills/codex-bark-wait').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
