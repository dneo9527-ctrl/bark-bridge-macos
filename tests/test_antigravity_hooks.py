import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_codex_bark as fixtures
from test_transport_tolerance import DelayedTLS


class NativeAntigravityTests(unittest.TestCase):
    setUp = fixtures.NotifierTests.setUp
    tearDown = fixtures.NotifierTests.tearDown
    ident = '11111111-2222-4333-8444-555555555555'

    def payload(self, **changes):
        root = Path.home() / '.gemini/antigravity/brain' / self.ident
        return {'conversationId': self.ident, 'executionId': 'synthetic-execution-1',
                'artifactDirectoryPath': str(root),
                'transcriptPath': str(root / '.system_generated/logs/transcript.jsonl'),
                'workspacePaths': ['/fake/project'], 'stepIdx': 2, 'executionNum': 0,
                'fullyIdle': True, 'continuationActive': False, 'terminationReason': 'NO_TOOL_CALL',
                'toolCall': {'name': 'ask_question', 'args': {'questions': [
                    {'question': 'Synthetic choice?', 'options': ['One', 'Two']}] }},
                'lastUserInput': 'PRIVATE_PROMPT', 'finalModelOutput': 'PRIVATE_REPLY',
                'error': 'PRIVATE_ERROR', **changes}

    def enable(self): self.m.enable_template('antigravity')

    def test_native_question_allowlists_metadata_and_never_reads_transcript(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('no log read')):
            event = self.m.antigravity_event(self.payload(), 'pre-tool')
        self.assertEqual(event['source_id'], 'com.google.antigravity')
        self.assertEqual(event['kind'], 'question')
        self.assertEqual(event['questions'][0]['options'], ['One', 'Two'])
        self.assertEqual(event['project_label'], 'project')
        raw = json.dumps(event)
        for excluded in ['PRIVATE_', 'synthetic-execution-1', '.gemini', self.ident]:
            self.assertNotIn(excluded, raw)

    def test_cli_ide_missing_inconsistent_paths_and_subagents_are_ignored(self):
        root = Path.home() / '.gemini/antigravity/brain' / self.ident
        for change in [
            {'artifactDirectoryPath': str(root).replace('/antigravity/', '/antigravity-cli/')},
            {'transcriptPath': str(root).replace('/antigravity/', '/antigravity-ide/') + '/transcript.jsonl'},
            {'artifactDirectoryPath': None, 'transcriptPath': None},
            {'artifactDirectoryPath': '/unrelated'}, {'conversationId': 'bad'},
            {'parentConversationId': self.ident}, {'artifactDirectoryPath': 42}]:
            with self.subTest(change=change):
                self.assertIsNone(self.m.antigravity_event(self.payload(**change), 'pre-tool'))

    def test_ask_permission_has_precise_scope_and_other_tools_do_not_trigger(self):
        event = self.m.antigravity_event(self.payload(toolCall={'name': 'ask_permission', 'args': {'Target': 'PRIVATE_TARGET'}}), 'pre-tool')
        self.assertEqual(event['kind'], 'permission')
        title, body = self.m.antigravity_label(event)
        self.assertIn('新增权限', title); self.assertIn('普通操作审批', body)
        self.assertNotIn('PRIVATE_TARGET', str(event))
        for tool in ['run_command', 'read_file', 'write_to_file', 'ask_question_extra']:
            self.assertIsNone(self.m.antigravity_event(self.payload(toolCall={'name': tool}), 'pre-tool'))
        for step in [-1, True, None]:
            self.assertIsNone(self.m.antigravity_event(self.payload(stepIdx=step), 'pre-tool'))

    def test_only_confirmed_idle_normal_stop_is_completion(self):
        self.assertEqual(self.m.antigravity_event(self.payload(), 'stop')['kind'], 'complete')
        for change in [{'fullyIdle': False}, {'fullyIdle': None}, {'continuationActive': True},
                       {'executionNum': True}, {'executionNum': -1}]:
            self.assertIsNone(self.m.antigravity_event(self.payload(**change), 'stop'))
        for reason in ['USER_CANCELED', 'ERROR', 'MAX_INVOCATIONS', 'TERMINAL_STEP_TYPE', 'INJECTED_RESPONSE', 'TERMINAL_CUSTOM_HOOK', '']:
            self.assertIsNone(self.m.antigravity_event(self.payload(terminationReason=reason), 'stop'))

    def test_same_step_coalesces_and_next_execution_remains_distinct(self):
        self.enable()
        with patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
            ev = self.m.antigravity_event(self.payload(), 'stop')
            self.assertEqual(self.m.deliver(ev), 'accepted')
            self.assertEqual(self.m.deliver(ev), 'deduplicated')
            new = self.m.antigravity_event(self.payload(executionId='next-execution'), 'stop')
            self.assertEqual(self.m.deliver(new), 'accepted')
        self.assertEqual(send.call_count, 2)

    def test_source_policies_and_preview_are_shared_without_codex_lookup(self):
        self.enable(); self.m.set_codex_preview(False)
        with patch.object(self.m, 'metadata_chat_name', side_effect=AssertionError('no Codex lookup')), \
                patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
            self.assertEqual(self.m.deliver(self.m.antigravity_event(self.payload(), 'pre-tool')), 'accepted')
            self.assertEqual(send.call_args.args[1], 'Antigravity · 等待回答')
            self.assertNotIn('Synthetic', str(send.call_args))
            self.assertEqual(send.call_args.args[3], 'Antigravity')
            self.m.change_source('com.google.antigravity', enabled=False)
            self.assertEqual(self.m.deliver(self.m.antigravity_event(self.payload(), 'stop')), 'source_disabled')
            self.m.change_source('com.google.antigravity', enabled=True)
            self.m.set_source_mute('com.google.antigravity', 900)
            self.assertEqual(self.m.deliver(self.m.antigravity_event(self.payload(), 'stop')), 'source_muted')
            self.assertEqual(send.call_count, 1)

    def test_question_choices_render_but_local_history_contains_no_content(self):
        self.enable()
        with patch.object(self.m, 'send_target', return_value=(True, 200, 'accepted')) as send:
            self.assertEqual(self.m.deliver(self.m.antigravity_event(self.payload(), 'pre-tool')), 'accepted')
        self.assertIn('Synthetic choice?', send.call_args.args[2])
        self.assertIn('1. One', send.call_args.args[2])
        raw = self.m.LOG.read_text()
        for value in ('Synthetic', 'PRIVATE_', self.ident, 'project'): self.assertNotIn(value, raw)

    def test_native_hook_output_never_returns_permission_or_execution_decisions(self):
        for data in [self.payload(), {}, [], 'malformed']:
            raw = data if isinstance(data, str) else json.dumps(data)
            result = subprocess.run([sys.executable, '-I', '-B', str(self.base / 'bark.py'), 'antigravity-hook', '--event', 'pre-tool'],
                                    input=raw, text=True, capture_output=True, timeout=2)
            self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, '{}', ''))

    def test_real_isolated_worker_transport_phone_archive_and_observed_source(self):
        self.enable()
        with DelayedTLS(self.base, delay=.01) as server, patch.dict(os.environ, {'CURL_CA_BUNDLE': str(server.cert), 'NO_PROXY': '127.0.0.1'}):
            self.m.atomic_json(self.m.CONFIG, {'server': server.url, 'device_key': self.secret, 'enabled': True})
            result = subprocess.run([sys.executable, '-I', '-B', str(self.base / 'bark.py'), 'antigravity-hook', '--event', 'stop'],
                                    input=json.dumps(self.payload()), capture_output=True, text=True, timeout=2)
            self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, '{}', ''))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                rows = self.m.history(source_id='com.google.antigravity') if self.m.LOG.exists() else []
                if any(x['status'] == 'accepted' for x in rows): break
                time.sleep(.025)
            else: self.fail('native worker did not complete')
        self.assertEqual(next(s['status'] for s in self.m.source_list() if s['id'] == 'com.google.antigravity'), 'observed')
        response = subprocess.CompletedProcess([], 0, b'{"code":200}\n200', b'')
        with patch.object(self.m.subprocess, 'run', return_value=response) as run:
            self.m.deliver(self.m.antigravity_event(self.payload(executionId='another'), 'stop'))
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['isArchive'], '1')


if __name__ == '__main__': unittest.main()
