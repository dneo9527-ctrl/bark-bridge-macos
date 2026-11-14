import contextlib
import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import test_codex_bark as fixtures

class BatteryTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown

    def test_default_enabled_and_builtin_protected(self):
        self.assertTrue(next(x for x in self.m.source_list() if x['id']=='mac-battery')['enabled'])
        with self.assertRaises(ValueError):self.m.change_source('mac-battery',remove=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:self.m.status()
        self.assertIn('"battery_enabled": true',out.getvalue())

    def test_normal_discharge_bounce_and_critical(self):
        levels=[]
        for pct in [80,26,25,21,20,19,22,18,11,10,9,12,8]:
            e=self.m.battery_event(pct,False)
            if e:levels.append(e['battery_level'])
        self.assertEqual(levels,[20,10])

    def test_cold_start_critical_suppresses_low(self):
        e=self.m.battery_event(8,False)
        self.assertEqual((e['battery_level'],e['battery_percent']),(10,8))
        for pct in [8,9,20,19,10]:self.assertIsNone(self.m.battery_event(pct,False))

    def test_ac_and_recovery_reset(self):
        self.assertIsNotNone(self.m.battery_event(20,False))
        self.assertIsNone(self.m.battery_event(18,True))
        self.assertEqual(self.m.battery_event(18,False)['battery_level'],20)
        self.assertIsNone(self.m.battery_event(26,False))
        self.assertEqual(self.m.battery_event(20,False)['battery_level'],20)

    def test_disable_enable_rearms_but_idempotent_enable_does_not(self):
        self.m.battery_event(20,False)
        self.m.change_source('mac-battery',enabled=True)
        self.assertIsNone(self.m.battery_event(20,False))
        self.m.change_source('mac-battery',enabled=False)
        self.assertIsNone(self.m.battery_event(8,False))
        self.m.change_source('mac-battery',enabled=True)
        self.assertEqual(self.m.battery_event(8,False)['battery_level'],10)

    def test_concurrent_claims_once_and_restart_persists(self):
        self.m.battery_db().close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows=list(pool.map(lambda _:self.m.battery_event(20,False),range(12)))
        self.assertEqual(sum(e is not None for e in rows),1)
        again=fixtures.module('battery_reload',self.base/'bark.py')
        self.assertIsNone(again.battery_event(19,False))
        self.assertEqual(again.STATE.stat().st_mode&0o777,0o600)

    def test_invalid_readings_never_create_cycle(self):
        for pct,ac in [(None,False),(101,False),(-1,False),(True,False),(20,'no'),(20.5,False)]:
            with self.assertRaises(ValueError):self.m.battery_event(pct,ac)
        self.assertFalse(self.m.STATE.exists())

    def test_battery_honors_quiet_and_does_not_catch_up(self):
        with patch.object(self.m,'send_target') as send,patch.object(self.m,'quiet_now',return_value=True):
            e=self.m.battery_event(20,False)
            self.assertEqual(self.m.deliver(e),'quiet_hours')
            row=self.m.history()[-1]
            self.assertEqual((row['battery_percent'],row['battery_level']),(20,20))
            for pct in [19,18,11]:self.assertIsNone(self.m.battery_event(pct,False))
            send.assert_not_called()
        self.assertIsNone(self.m.battery_event(19,False))

    def test_master_switch_and_explicit_route_no_fallback(self):
        e=self.m.battery_event(20,False)
        with contextlib.redirect_stdout(io.StringIO()):self.m.set_enabled(False)
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(e),'disabled');send.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()):self.m.set_enabled(True)
        self.m.set_route('mac-battery',{'mode':'selected','device_ids':[]})
        with patch.object(self.m,'send_target') as send:
            self.assertEqual(self.m.deliver(e),'route_no_targets');send.assert_not_called()

    def test_payload_and_history_only_safe_battery_metadata(self):
        e=self.m.battery_event(9,False)
        with patch.object(self.m,'send_target',return_value=(True,200,'accepted')) as send:
            self.assertEqual(self.m.deliver(e),'accepted')
            self.assertIn('9%',send.call_args.args[2])
        row=self.m.history()[-1]
        self.assertEqual((row['source_id'],row['battery_percent'],row['battery_level']),('mac-battery',9,10))
        self.assertNotIn(self.secret,str(row))

class CliBoundaryTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown

    def test_invalid_quiet_and_route_return_private_fixed_json(self):
        import json,subprocess
        for action,data,extra in [('quiet-set',{'enabled':True,'start':'12:00','end':'12:00'},[]),('route-set',{'mode':'selected','device_ids':['UNREGISTERED_PRIVATE']},['--source-id','codex'])]:
            r=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),action]+extra,input=json.dumps(data),capture_output=True,text=True,timeout=2)
            self.assertEqual(json.loads(r.stdout),{'ok':False,'error':'operation_failed'})
            self.assertEqual(r.stderr,'')

    def test_corrupt_policy_cli_returns_json_not_silent_success(self):
        import json,subprocess
        self.m.RULES.write_text('not json'); self.m.RULES.chmod(0o600)
        r=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'policy-get'],capture_output=True,text=True,timeout=2)
        self.assertEqual(json.loads(r.stdout),{'ok':False,'error':'operation_failed'})

if __name__=='__main__':unittest.main(verbosity=2)
