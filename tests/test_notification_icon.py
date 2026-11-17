import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import test_codex_bark as fixtures


class NotificationIconTests(unittest.TestCase):
    setUp = fixtures.NotifierTests.setUp
    tearDown = fixtures.NotifierTests.tearDown

    def capture(self, event):
        response = subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')
        with patch.object(self.m.subprocess, 'run', return_value=response) as run:
            self.assertEqual(self.m.deliver(event, force=True), 'accepted')
        return run.call_args_list

    def test_codex_events_and_source_test_include_static_icon(self):
        for kind in ('permission', 'question', 'complete', 'source_test'):
            with self.subTest(kind=kind):
                call = self.capture({'kind': kind, 'source_id': 'codex'})[0]
                payload = json.loads(call.kwargs['input'])
                self.assertEqual(payload['icon'], self.m.SOURCE_ICONS['codex'])
                self.assertEqual(payload['isArchive'], '1')

    def test_hook_inferred_source_has_icon_without_chat_preview(self):
        self.m.set_codex_preview(False)
        event = self.m.event_from_hook({'hook_event_name': 'PermissionRequest', 'session_id': 'fixture',
                                       'tool_name': 'Bash', 'tool_input': {'command': 'PRIVATE_COMMAND'}})
        payload = json.loads(self.capture(event)[0].kwargs['input'])
        self.assertEqual(payload['icon'], self.m.SOURCE_ICONS['codex'])
        self.assertEqual(payload['isArchive'], '1')
        self.assertEqual(payload['title'], 'Codex 等待授权')
        self.assertNotIn('PRIVATE_COMMAND', json.dumps(payload))

    def test_non_codex_sources_do_not_inherit_icon_from_group_label(self):
        sources = [
            {'id': 'claude-code', 'name': 'Claude Code', 'type': 'application', 'enabled': True},
            {'id': 'other-app', 'name': 'Codex', 'type': 'application', 'enabled': True},
        ]
        self.m.atomic_json(self.m.SOURCES, {'sources': sources})
        for event in ({'kind': 'permission', 'source_id': 'claude-code'},
                      {'kind': 'application', 'source_id': 'other-app'},
                      {'kind': 'source_test', 'source_id': 'other-app'},
                      {'kind': 'battery', 'battery_percent': 20}, {'kind': 'test'}):
            with self.subTest(event=event):
                payload = json.loads(self.capture(event)[0].kwargs['input'])
                self.assertNotIn('icon', payload)
                self.assertEqual(payload['isArchive'], '0')

    def test_every_routed_device_gets_icon_and_partial_failure_is_preserved(self):
        second = self.m.add_device('https://example.test/SECOND_FAKE_KEY', 'fixture')['id']
        self.m.set_route('codex', {'mode': 'selected', 'device_ids': ['primary', second]})
        def respond(argv, **kwargs):
            payload = json.loads(kwargs['input'])
            ok = payload['device_key'] == self.secret
            return subprocess.CompletedProcess(argv, 0, b'{"code":200}\n200' if ok else b'{"code":500}\n503')
        with patch.object(self.m.subprocess, 'run', side_effect=respond) as run:
            self.assertEqual(self.m.deliver(self.event), 'partial')
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            payload = json.loads(call.kwargs['input'])
            self.assertEqual(payload['icon'], self.m.SOURCE_ICONS['codex'])
            self.assertEqual(payload['isArchive'], '1')
        row = json.loads(self.m.LOG.read_text().splitlines()[-1])
        self.assertEqual((row['delivered'], row['total']), (1, 2))

    def test_legacy_four_argument_call_has_no_implicit_icon(self):
        response = subprocess.CompletedProcess([], 0, b'{"code":200}\n200')
        with patch.object(self.m.subprocess, 'run', return_value=response) as run:
            self.assertEqual(self.m.send_target({'server': 'https://example.test', 'device_key': self.secret},
                                               'title', 'body', 'Codex'), (True, 200, 'accepted'))
        self.assertNotIn('icon', json.loads(run.call_args.kwargs['input']))
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['isArchive'], '0')

    def test_icon_adds_no_fetch_retry_or_private_arguments_and_logs(self):
        calls = self.capture(dict(self.event))
        self.assertEqual(len(calls), 1)
        argv = calls[0].args[0]
        self.assertEqual(argv[argv.index('--data-binary') + 1], '@-')
        self.assertEqual(argv[argv.index('--connect-timeout') + 1], '4')
        self.assertEqual(argv[argv.index('--max-time') + 1], '8')
        self.assertEqual(argv[argv.index('--retry') + 1], '0')
        self.assertEqual(calls[0].kwargs['timeout'], 9)
        self.assertNotIn(self.secret, str(argv))
        self.assertNotIn(self.m.SOURCE_ICONS['codex'], str(argv))
        self.assertNotIn(self.secret, self.m.LOG.read_text())
        self.assertNotIn(self.m.SOURCE_ICONS['codex'], self.m.LOG.read_text())
        url = urlsplit(self.m.SOURCE_ICONS['codex'])
        self.assertEqual((url.scheme, url.hostname, url.query, url.fragment),
                         ('https', 'raw.githubusercontent.com', '', ''))
        self.assertRegex(url.path, r'^/dneo9527-ctrl/bark-bridge-macos/[0-9a-f]{40}/docs/media/codex-source-icon\.png$')

    def test_policy_skip_and_timeout_do_not_fall_back_to_duplicate_sends(self):
        with patch.object(self.m, 'quiet_now', return_value=True), patch.object(self.m.subprocess, 'run') as run:
            self.assertEqual(self.m.deliver(dict(self.event)), 'quiet_hours')
            run.assert_not_called()
        with patch.object(self.m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 9)) as run:
            self.assertEqual(self.m.deliver(dict(self.event)), 'send_failed')
            self.assertEqual(self.m.deliver(dict(self.event)), 'deduplicated')
            self.assertEqual(run.call_count, 1)
