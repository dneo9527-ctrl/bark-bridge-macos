import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_codex_bark as fixtures
from test_transport_tolerance import DelayedTLS


class AgyNotificationTests(unittest.TestCase):
    setUp = fixtures.NotifierTests.setUp
    tearDown = fixtures.NotifierTests.tearDown

    def agy(self, status='completed', **extra):
        return self.m.agy_event({'status': status, 'conversation_id': 'fake-conversation',
                                 'turn_id': 'fake-turn', 'chat_title': 'Demo task', **extra})

    def enable(self): self.m.enable_template('agy')

    def test_strict_terminal_events_and_metadata_allowlist(self):
        for status in ('queued', 'running', 'cancelled', 'interrupted', 'question', 'permission', None):
            self.assertIsNone(self.agy(status))
        for invalid in (None, [], {}, {'status': 'completed'},
                        {'status': 'completed', 'conversation_id': '../escape', 'turn_id': 'x'}):
            self.assertIsNone(self.m.agy_event(invalid))
        event = self.agy(prompt='PRIVATE_PROMPT', response='PRIVATE_REPLY', error='PRIVATE_ERROR')
        self.assertEqual(set(event), {'kind', 'source_id', 'origin', 'session', 'fingerprint', 'task_success', 'chat_title'})
        self.assertNotIn('fake-turn', json.dumps(event))
        self.assertNotIn('PRIVATE_', json.dumps(event))

    def test_template_is_idempotent_and_preserves_disabled_source(self):
        self.enable(); self.m.change_source('agy-console', enabled=False); self.enable()
        rows = [s for s in self.m.source_list() if s['id'] == 'agy-console']
        self.assertEqual(len(rows), 1); self.assertFalse(rows[0]['enabled'])

    def test_outcomes_have_own_title_group_and_private_history(self):
        self.enable()
        for status, label in [('completed', '本轮已完成'), ('failed', '任务未完成')]:
            with patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
                self.assertEqual(self.m.deliver(self.agy(status)), 'accepted')
                self.assertEqual(send.call_args.args[1], 'Demo task · Agy · ' + label)
                self.assertEqual(send.call_args.args[3:5], ('Agy Console', 'agy-console'))
        raw = self.m.LOG.read_text()
        self.assertNotIn('Demo task', raw); self.assertNotIn('fake-turn', raw)
        self.assertEqual(self.m.history(source_id='agy-console')[0]['source_id'], 'agy-console')

    def test_preview_off_hides_title_without_codex_lookup(self):
        self.enable(); self.m.set_codex_preview(False)
        with patch.object(self.m, 'chat_name', side_effect=AssertionError('no Codex lookup')), \
                patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
            self.assertEqual(self.m.deliver(self.agy()), 'accepted')
        self.assertEqual(send.call_args.args[1], 'Agy · 本轮已完成')
        self.assertNotIn('Demo task', str(send.call_args))

    def test_duplicate_event_is_coalesced(self):
        self.enable()
        with patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
            self.assertEqual(self.m.deliver(self.agy()), 'accepted')
            self.assertEqual(self.m.deliver(self.agy()), 'deduplicated')
        self.assertEqual(send.call_count, 1)

    def test_disabled_unknown_muted_quiet_and_global_pause_do_not_send(self):
        with patch.object(self.m, 'send_target') as send:
            self.assertEqual(self.m.deliver(self.agy()), 'unknown_source')
            self.enable(); self.m.change_source('agy-console', enabled=False)
            self.assertEqual(self.m.deliver(self.agy()), 'source_disabled')
            self.m.change_source('agy-console', enabled=True); self.m.set_source_mute('agy-console', 900)
            self.assertEqual(self.m.deliver(self.agy()), 'source_muted')
            self.m.set_source_mute('agy-console', 0)
            with patch.object(self.m, 'quiet_now', return_value=True):
                self.assertEqual(self.m.deliver(self.agy()), 'quiet_hours')
            self.m.set_enabled(False, emit=False)
            self.assertEqual(self.m.deliver(self.agy()), 'disabled')
            send.assert_not_called()

    def test_routing_and_phone_history(self):
        self.enable()
        second = self.m.add_device('https://example.test/SECOND_FAKE_KEY', 'fixture')['id']
        self.m.set_route('agy-console', {'mode': 'selected', 'device_ids': [second]})
        response = subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')
        with patch.object(self.m.subprocess, 'run', return_value=response) as run:
            self.assertEqual(self.m.deliver(self.agy()), 'accepted')
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['device_key'], 'SECOND_FAKE_KEY')
        self.assertEqual(payload['isArchive'], '1'); self.assertNotIn('Codex', payload['title'])

    def test_cli_worker_transport_and_observed_source(self):
        self.enable()
        with DelayedTLS(self.base, delay=.01) as server, patch.dict(os.environ, {'CURL_CA_BUNDLE': str(server.cert), 'NO_PROXY': '127.0.0.1'}):
            self.m.atomic_json(self.m.CONFIG, {'server': server.url, 'device_key': self.secret, 'enabled': True})
            event = {'status': 'completed', 'conversation_id': 'fake-conversation', 'turn_id': 'fake-turn', 'chat_title': 'Synthetic preview'}
            result = subprocess.run([sys.executable, '-I', '-B', str(self.base / 'bark.py'), 'agy-event'],
                                    input=json.dumps(event), capture_output=True, text=True, timeout=2)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            until = time.monotonic() + 4
            while time.monotonic() < until:
                if self.m.LOG.exists() and any(r.get('source_id') == 'agy-console' and r['status'] == 'accepted' for r in self.m.history()): break
                time.sleep(.025)
            else: self.fail('isolated Agy worker did not complete')
        self.assertEqual(next(s['status'] for s in self.m.source_list() if s['id'] == 'agy-console'), 'observed')


if __name__ == '__main__': unittest.main()
