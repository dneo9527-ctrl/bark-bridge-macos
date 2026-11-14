import contextlib
import datetime
import io
import json
import os
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import test_codex_bark as fixtures


class ApprovedFeatureTests(unittest.TestCase):
    setUp = fixtures.NotifierTests.setUp
    tearDown = fixtures.NotifierTests.tearDown

    def second(self):
        return self.m.add_device('https://example.test/SECOND_FAKE_KEY', '备用手机')['id']

    def route(self, ids):
        return self.m.set_route('codex', {'mode':'selected','device_ids':ids})

    def test_defaults_and_policy_file_do_not_change_phone_credentials(self):
        before = self.m.CONFIG.read_bytes()
        self.assertFalse(self.m.policy_status()['quiet_hours']['enabled'])
        self.assertEqual(self.m.rules()['routes'], {})
        self.route(['primary'])
        self.m.set_quiet({'enabled':False,'start':'22:30','end':'07:00'})
        self.assertEqual(self.m.CONFIG.read_bytes(), before)
        self.assertEqual(self.m.RULES.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.secret, self.m.RULES.read_text())

    def test_inherit_selected_empty_and_reset_routes(self):
        sid = self.second()
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver(self.event,force=True),'accepted')
            self.assertEqual(send.call_count,2)
            send.reset_mock(); self.route([sid])
            self.assertEqual(self.m.deliver(self.event,force=True),'accepted')
            self.assertEqual(send.call_count,1)
            self.assertEqual(send.call_args.args[0]['id'],sid)
            self.route([]); send.reset_mock()
            self.assertEqual(self.m.deliver(self.event,force=True),'route_no_targets')
            send.assert_not_called()
            self.m.set_route('codex',{'mode':'all'}); send.reset_mock()
            self.assertEqual(self.m.deliver(self.event,force=True),'accepted')
            self.assertEqual(send.call_count,2)

    def test_unavailable_selected_device_never_falls_back(self):
        sid=self.second(); self.route([sid]); self.m.change_device(sid,enabled=False)
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(self.event),'route_no_targets')
            self.m.change_device(sid,remove=True)
            self.assertEqual(self.m.deliver(self.event),'route_no_targets')
            send.assert_not_called()
        self.assertTrue(self.m.device_list()[0]['enabled'])

    def test_new_device_does_not_expand_explicit_route(self):
        self.route(['primary']); self.second()
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver(self.event),'accepted')
            self.assertEqual(send.call_count,1)
            self.assertEqual(send.call_args.args[0]['id'],'primary')

    def test_unknown_routes_are_rejected_without_writes(self):
        self.route(['primary']); before=self.m.RULES.read_bytes()
        with self.assertRaises(ValueError): self.route(['unregistered-device'])
        with self.assertRaises(ValueError): self.m.set_route('unregistered-source',{'mode':'all'})
        self.assertEqual(self.m.RULES.read_bytes(),before)

    def test_quiet_cross_midnight_and_daytime_boundaries(self):
        for start,end,quiet_times,awake_times in [
            ('23:00','08:00',[(23,0),(0,0),(7,59)],[(22,59),(8,0),(12,0)]),
            ('09:00','17:30',[(9,0),(12,0),(17,29)],[(8,59),(17,30),(0,0)])]:
            self.m.set_quiet({'enabled':True,'start':start,'end':end})
            for h,m in quiet_times:self.assertTrue(self.m.quiet_now(now=datetime.datetime(2026,11,12,h,m)))
            for h,m in awake_times:self.assertFalse(self.m.quiet_now(now=datetime.datetime(2026,11,12,h,m)))
        before=self.m.RULES.read_bytes()
        for start,end in [('12:00','12:00'),('24:00','07:00'),('9:00','17:00'),('23:60','08:00')]:
            with self.assertRaises(ValueError): self.m.set_quiet({'enabled':True,'start':start,'end':end})
        self.assertEqual(self.m.RULES.read_bytes(),before)

    def test_quiet_skips_automatic_events_before_dedup_or_network(self):
        with patch.object(self.m,'quiet_now',return_value=True), patch.object(self.m,'send_target') as send, patch.object(self.m,'claim') as claim:
            for kind in ('permission','question','application'):
                self.assertEqual(self.m.deliver({'kind':kind}),'quiet_hours')
            send.assert_not_called();claim.assert_not_called()
        self.assertEqual(self.m.history()[-1]['reason'],'quiet_hours')

    def test_manual_diagnosis_bypasses_schedule_but_respects_master_and_device(self):
        with patch.object(self.m,'quiet_now',return_value=True), patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            report=self.m.diagnose('primary')
            self.assertTrue(report['ok']);self.assertEqual(send.call_count,1)
            self.m.change_device('primary',enabled=False);send.reset_mock()
            self.assertEqual(self.m.diagnose('primary')['result'],'no_devices_enabled');send.assert_not_called()
            self.m.change_device('primary',enabled=True)
            with contextlib.redirect_stdout(io.StringIO()): self.m.set_enabled(False)
            self.assertEqual(self.m.diagnose('primary')['result'],'disabled');send.assert_not_called()

    def test_source_test_obeys_route_while_direct_device_test_does_not(self):
        self.route([])
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver({'kind':'source_test','source_id':'codex'},force=True),'route_no_targets')
            send.assert_not_called()
            self.assertTrue(self.m.diagnose('primary')['ok']);self.assertEqual(send.call_count,1)

    def test_classified_failure_reasons_and_sensitive_response_privacy(self):
        cases=[(6,0,'dns_error'),(7,0,'connect_error'),(28,0,'timeout'),(60,0,'tls_error'),(0,403,'http_auth'),(0,404,'endpoint_missing'),(0,429,'rate_limited'),(0,503,'server_error'),(0,422,'request_rejected'),(0,200,'api_rejected')]
        for exitcode,http,reason in cases:
            response=json.dumps({'code':400,'message':self.secret+'https://PRIVATE_URL'}).encode()+b'\n'+str(http).encode()
            with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],exitcode,response,b'PRIVATE_STDERR')):
                report=self.m.diagnose('primary')
                self.assertEqual(report['reason'],reason)
                self.assertNotIn(self.secret,json.dumps(report))
                self.assertNotIn('PRIVATE',json.dumps(report))
        log=self.m.LOG.read_text()
        for value in [self.secret,'PRIVATE_URL','PRIVATE_STDERR','https://']:
            self.assertNotIn(value,log)

    def test_invalid_response_and_python_timeout_are_fixed_enums(self):
        for body in [b'not json',b'[]',b'null']:
            with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,body+b'\n200',b'')):
                self.assertEqual(self.m.diagnose('primary')['reason'],'invalid_response')
        with patch.object(self.m.subprocess,'run',side_effect=subprocess.TimeoutExpired(['PRIVATE_COMMAND'],6)):
            self.assertEqual(self.m.diagnose('primary')['reason'],'timeout')
        self.assertNotIn('PRIVATE_COMMAND',self.m.LOG.read_text())

    def test_partial_report_has_only_safe_device_ids_and_fixed_reasons(self):
        sid=self.second()
        def send(d,*args):return (True,200,'accepted') if d['id']=='primary' else (False,0,'timeout')
        with patch.object(self.m,'send_target',side_effect=send):
            report=self.m.diagnose()
        self.assertEqual(report['result'],'partial')
        self.assertEqual((report['delivered'],report['total']),(1,2))
        self.assertEqual(report['reasons'],{'accepted':1,'timeout':1})
        self.assertEqual({x['device_id'] for x in report['device_results']},{'primary',sid})
        self.assertNotIn(self.secret,json.dumps(report));self.assertNotIn('SECOND_FAKE_KEY',json.dumps(report))

    def test_history_legacy_unknown_and_field_whitelist(self):
        row={'at':'2026-11-12T01:00:00+00:00','kind':'question','status':'accepted','device_key':self.secret,'url':'https://PRIVATE_URL'}
        self.m.LOG.write_text(json.dumps(row)+'\nmalformed json\n');os.chmod(self.m.LOG,0o600)
        before=self.m.LOG.read_bytes()
        rows=self.m.history()
        self.assertEqual(rows[0]['source_id'],'unknown')
        self.assertNotIn(self.secret,json.dumps(rows));self.assertNotIn('PRIVATE_URL',json.dumps(rows))
        self.assertEqual(self.m.LOG.read_bytes(),before)
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')):self.m.deliver(self.event)
        self.assertEqual(self.m.history(source_id='codex')[-1]['source_id'],'codex')
        self.assertEqual(len(self.m.history(source_id='unknown')),1)

    def test_history_filters_limit_and_control_category(self):
        for status in ['accepted','partial','send_failed','not_configured','quiet_hours','route_no_targets','disabled','enabled']:
            self.m.log(status,'control',source_id='system')
        self.assertEqual(len(self.m.history(result='success')),1)
        self.assertEqual(len(self.m.history(result='failed')),3)
        self.assertEqual(len(self.m.history(result='skipped')),3)
        for _ in range(210):self.m.log('accepted','test')
        self.assertEqual(len(self.m.history()),200)
        self.assertEqual(len(self.m.history(limit=9)),9)

    def test_unknown_input_source_is_not_echoed_in_log(self):
        self.assertEqual(self.m.deliver({'kind':'application','source_id':'PRIVATE_UNREGISTERED_SECRET'}),'unknown_source')
        self.assertNotIn('PRIVATE_UNREGISTERED_SECRET',self.m.LOG.read_text())

    def test_concurrent_policy_edits_keep_both_fields_and_permissions(self):
        def change(index):
            if index%2:self.route(['primary'])
            else:self.m.set_quiet({'enabled':False,'start':'21:00','end':'06:00'})
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(change,range(12)))
        self.assertEqual(self.m.rules()['routes']['codex'],['primary'])
        self.assertEqual(self.m.rules()['quiet_hours']['start'],'21:00')
        self.assertEqual(self.m.RULES.stat().st_mode&0o777,0o600)

    def test_detached_hook_obeys_quiet_without_blocking_or_policy_output(self):
        now=datetime.datetime.now().astimezone();m=now.hour*60+now.minute
        def clock(n):return f'{(n%1440)//60:02d}:{n%60:02d}'
        # The end is exclusive: leave a full minute for an async worker
        # even when this test starts just before the minute rolls over.
        self.m.set_quiet({'enabled':True,'start':clock(m-1),'end':clock(m+2)})
        self.assertTrue(self.m.quiet_now(now=now+datetime.timedelta(minutes=1)))
        start=time.monotonic()
        p=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'hook'],input='{"hook_event_name":"PermissionRequest","tool_input":{"command":"PRIVATE_TOOL"}}',capture_output=True,text=True,timeout=1)
        self.assertEqual((p.returncode,p.stdout,p.stderr),(0,'',''));self.assertLess(time.monotonic()-start,1)
        for _ in range(40):
            if self.m.LOG.exists():break
            time.sleep(.05)
        self.assertEqual(self.m.history()[-1]['status'],'quiet_hours')
        self.assertNotIn('PRIVATE_TOOL',self.m.LOG.read_text())

class ReviewBoundaryTests(ApprovedFeatureTests):
    # Only additional review tests are selected explicitly by the runner below.
    def test_source_test_name_resolves_route_and_missing_source_is_rejected(self):
        self.route([])
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver({'kind':'source_test','source':'Codex'},force=True),'route_no_targets')
            self.assertEqual(self.m.deliver({'kind':'source_test'},force=True),'unknown_source')
            send.assert_not_called()

    def test_history_reads_rotation_without_rewriting_old_records(self):
        for _ in range(7):self.m.log('accepted','test')
        previous=self.base/'events.previous.jsonl'
        self.m.LOG.replace(previous)
        before=previous.read_bytes()
        self.m.log('send_failed','test',reason='timeout')
        self.assertEqual(len(self.m.history()),8)
        self.assertEqual(len(self.m.history(result='success')),7)
        self.assertEqual(previous.read_bytes(),before)

    def test_corrupt_rules_stop_network_with_fixed_reason(self):
        self.route(['primary']);self.m.RULES.write_text('{broken PRIVATE_TEXT')
        report={}
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(self.event,report=report),'send_failed')
            send.assert_not_called()
        self.assertEqual(report['reason'],'invalid_rules')
        self.assertNotIn('PRIVATE_TEXT',self.m.LOG.read_text())

    def test_log_repairs_permissions_on_existing_file(self):
        self.m.LOG.write_text('');os.chmod(self.m.LOG,0o644)
        self.m.log('accepted','test')
        self.assertEqual(self.m.LOG.stat().st_mode&0o777,0o600)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ApprovedFeatureTests)
    for name in ReviewBoundaryTests.__dict__:
        if name.startswith('test_'):suite.addTest(ReviewBoundaryTests(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
