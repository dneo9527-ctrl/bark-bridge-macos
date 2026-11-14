import contextlib,io,json,subprocess,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import test_codex_bark as fixtures

class EventPreferenceTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown
    def event_of(self,kind):return {'kind':kind,'session':'s-'+kind,'fingerprint':'event-'+kind}
    def test_legacy_rules_default_to_all_on_without_writing(self):
        data={'version':1,'quiet_hours':{'enabled':False,'start':'23:00','end':'08:00'},'routes':{}}
        self.m.atomic_json(self.m.RULES,data);before=self.m.RULES.read_bytes()
        self.assertEqual(self.m.rules()['codex_events'],self.m.CODEX_EVENT_DEFAULTS)
        self.assertEqual(self.m.RULES.read_bytes(),before)
    def test_defaults_allow_three_types_and_keep_secret_private(self):
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            for kind in ['permission','question','complete']:self.assertEqual(self.m.deliver(self.event_of(kind),force=True),'accepted')
            self.assertEqual(send.call_count,3)
        self.assertNotIn(self.secret,self.m.LOG.read_text())
    def test_complete_off_skips_before_claim_and_network_even_forced(self):
        self.m.set_codex_events({'complete':False})
        with patch.object(self.m,'send_target') as send,patch.object(self.m,'claim') as claim:
            for forced in [False,True]:self.assertEqual(self.m.deliver(self.event_of('complete'),force=forced),'event_disabled')
            send.assert_not_called();claim.assert_not_called()
        self.assertEqual(self.m.history()[-1]['category'],'skipped')
    def test_other_types_still_deliver_when_complete_disabled(self):
        self.m.set_codex_events({'complete':False})
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            for kind in ['permission','question']:self.assertEqual(self.m.deliver(self.event_of(kind),force=True),'accepted')
            self.assertEqual(send.call_count,2)
    def test_all_off_does_not_block_manual_source_test_or_battery(self):
        self.m.set_codex_events({k:False for k in self.m.CODEX_EVENT_DEFAULTS})
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver({'kind':'source_test','source_id':'codex'},force=True),'accepted')
            e=self.m.battery_event(20,False);self.assertEqual(self.m.deliver(e),'accepted')
            self.assertEqual(send.call_count,2)
    def test_master_and_source_switch_still_govern_disabled_type(self):
        self.m.set_codex_events({'complete':False})
        with contextlib.redirect_stdout(io.StringIO()):self.m.set_enabled(False)
        self.assertEqual(self.m.deliver(self.event_of('complete')),'disabled')
        with contextlib.redirect_stdout(io.StringIO()):self.m.set_enabled(True)
        self.m.change_source('codex',enabled=False)
        self.assertEqual(self.m.deliver(self.event_of('complete')),'source_disabled')
    def test_partial_update_and_other_rule_edits_preserve_every_setting(self):
        self.m.set_codex_events({'complete':False,'question':False})
        self.m.set_quiet({'enabled':False,'start':'22:30','end':'07:30'})
        self.m.set_route('codex',{'mode':'selected','device_ids':['primary']})
        self.m.set_codex_events({'question':True})
        d=self.m.rules();self.assertEqual(d['codex_events'],{'permission':True,'question':True,'complete':False})
        self.assertEqual(d['quiet_hours']['start'],'22:30');self.assertEqual(d['routes']['codex'],['primary'])
    def test_invalid_preferences_do_not_overwrite_rules(self):
        self.m.set_codex_events({'complete':False});before=self.m.RULES.read_bytes()
        for data in [{'complete':'off'},{'complete':0},{'other':True},[],{},None]:
            with self.assertRaises(ValueError):self.m.set_codex_events(data)
        self.assertEqual(self.m.RULES.read_bytes(),before)
    def test_corrupt_stored_preferences_fail_closed(self):
        d=self.m.rules();d['codex_events']={'complete':'PRIVATE_VALUE'};self.m.atomic_json(self.m.RULES,d)
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(self.event_of('permission')),'send_failed');send.assert_not_called()
        self.assertFalse(self.m.snapshot()['policy']['ok']);self.assertNotIn('PRIVATE_VALUE',json.dumps(self.m.snapshot()))
    def test_preferences_do_not_modify_phone_config_and_remain_private(self):
        before=self.m.CONFIG.read_bytes();self.m.set_codex_events({'complete':False})
        self.assertEqual(self.m.CONFIG.read_bytes(),before);self.assertEqual(self.m.RULES.stat().st_mode&0o777,0o600)
        self.assertNotIn(self.secret,self.m.RULES.read_text())
    def test_concurrent_rule_edits_do_not_lose_event_switches(self):
        def edit(n):
            if n%3==0:self.m.set_codex_events({'complete':False})
            elif n%3==1:self.m.set_quiet({'enabled':False,'start':'22:00','end':'06:00'})
            else:self.m.set_route('codex',{'mode':'selected','device_ids':['primary']})
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(edit,range(12)))
        d=self.m.rules();self.assertFalse(d['codex_events']['complete']);self.assertEqual(d['quiet_hours']['start'],'22:00');self.assertEqual(d['routes']['codex'],['primary'])
    def test_cli_and_policy_snapshot_expose_only_booleans(self):
        p=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'events-set'],input='{"complete":false}',capture_output=True,text=True,timeout=2)
        self.assertTrue(json.loads(p.stdout)['ok']);self.assertEqual(p.stderr,'')
        for d in [self.m.policy_status(),self.m.snapshot()['policy']]:
            self.assertEqual(d['codex_events'],{'permission':True,'question':True,'complete':False});self.assertNotIn(self.secret,json.dumps(d))

if __name__=='__main__':unittest.main(verbosity=2)
