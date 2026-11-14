import json,subprocess,unittest
from unittest.mock import patch
import test_codex_bark as fixtures

class CompletionTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown
    def completion(self,turn='turn1'):
        return self.m.event_from_hook({'hook_event_name':'Stop','session_id':'private-session','turn_id':turn,'last_assistant_message':'PRIVATE_RESULT_DO_NOT_TRANSMIT','transcript_path':'PRIVATE_PATH'})
    def test_stop_only_main_turn_id_and_payload_privacy(self):
        e=self.completion();self.assertEqual(e['kind'],'complete')
        self.assertNotIn('PRIVATE',json.dumps(e));self.assertNotIn('private-session',json.dumps(e))
        for name in ['SubagentStop','SessionEnd','Interrupt']:
            self.assertIsNone(self.m.event_from_hook({'hook_event_name':name,'turn_id':'t'}))
        self.assertIsNone(self.m.event_from_hook({'hook_event_name':'Stop'}))
    def test_complete_dedup_per_turn_keeps_next_turn(self):
        e=self.completion();self.assertTrue(self.m.claim(e));self.assertFalse(self.m.claim(e))
        self.assertTrue(self.m.claim(self.completion('turn2')))
    def test_question_and_immediate_stop_coalesce_without_sliding_window(self):
        e=self.completion()
        with patch.object(self.m.time,'time',return_value=1000):
            self.assertTrue(self.m.claim({'kind':'question','session':e['session'],'fingerprint':'ask'}))
            self.assertFalse(self.m.claim(e))
        with patch.object(self.m.time,'time',return_value=1016):self.assertTrue(self.m.claim(e))
    def test_complete_uses_codex_route_quiet_and_no_result_body(self):
        e=self.completion()
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver(e),'accepted')
            self.assertIn('本轮',send.call_args.args[1]);self.assertNotIn('PRIVATE',str(send.call_args))
        self.assertEqual(self.m.history()[-1]['source_id'],'codex')
        self.m.set_route('codex',{'mode':'selected','device_ids':[]})
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(self.completion('t2')),'route_no_targets');send.assert_not_called()
        with patch.object(self.m,'quiet_now',return_value=True),patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(self.completion('t3')),'quiet_hours');send.assert_not_called()
    def test_stop_hook_output_is_nonblocking_and_no_decisions(self):
        # Quiet rule ensures the detached worker performs no external network operation.
        self.m.set_quiet({'enabled':True,'start':'00:00','end':'23:59'})
        e={'hook_event_name':'Stop','session_id':'test','turn_id':'t','last_assistant_message':'PRIVATE_RESULT'}
        r=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'hook'],input=json.dumps(e),text=True,capture_output=True,timeout=1)
        self.assertEqual((r.returncode,r.stdout,r.stderr),(0,'{}\n',''))
        self.assertNotIn('decision',r.stdout)
if __name__=='__main__':unittest.main(verbosity=2)
