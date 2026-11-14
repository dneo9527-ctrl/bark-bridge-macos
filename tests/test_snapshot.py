import contextlib,datetime,io,json,os,subprocess,unittest
from unittest.mock import patch
import test_codex_bark as fixtures

class SnapshotTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown
    def test_menu_reads_each_component_once_and_never_logs(self):
        with patch.object(self.m,'config',wraps=self.m.config) as cfg,patch.object(self.m,'source_list',wraps=self.m.source_list) as sources,patch.object(self.m,'rules',wraps=self.m.rules) as rules,patch.object(self.m,'history_lines') as history:
            d=self.m.snapshot('menu')
            self.assertEqual((cfg.call_count,sources.call_count,rules.call_count),(1,1,1));history.assert_not_called()
        self.assertNotIn('events',d);self.assertNotIn('latest_event',d)
        self.assertEqual(d['devices'][0]['id'],'primary');self.assertTrue(d['status']['enabled'])
    def test_panel_latest_is_safe_and_full_history_is_lazy(self):
        self.m.log('accepted','complete',source_id='codex')
        self.m.log('disabled','control');self.m.log('deduplicated','question',source_id='codex')
        with patch.object(self.m,'history',side_effect=AssertionError('full history must be lazy')):
            d=self.m.snapshot('panel')
        self.assertEqual(d['latest_event']['kind'],'complete');self.assertNotIn('events',d)
        self.assertNotIn(self.secret,json.dumps(d))
    def test_history_view_parses_once_and_keeps_limit(self):
        for _ in range(205):self.m.log('accepted','complete',source_id='codex')
        with patch.object(self.m,'history',wraps=self.m.history) as history,patch.object(self.m,'latest_event') as latest:
            d=self.m.snapshot('history');history.assert_called_once();latest.assert_not_called()
        self.assertEqual(len(d['events']),200);self.assertEqual(d['latest_event'],d['events'][-1])
    def test_device_and_source_metadata_never_serialize_extra_secret_fields(self):
        self.m.atomic_json(self.m.SOURCES,{'sources':[{'id':'codex','name':'Codex','type':'builtin','enabled':True,'status':'connected','device_key':self.secret,'server':'https://PRIVATE_SERVER','token':'PRIVATE_ACCOUNT_TOKEN'}]})
        d=self.m.snapshot('history');s=json.dumps(d)
        for value in [self.secret,'PRIVATE_SERVER','PRIVATE_ACCOUNT_TOKEN','device_key','token','server']:
            self.assertNotIn(value,s)
        self.assertEqual(set(d['devices'][0]),{'id','name','enabled'})
    def test_bad_rules_do_not_erase_good_device_or_master_state(self):
        self.m.atomic_json(self.m.RULES,{'invalid':self.secret})
        d=self.m.snapshot('panel')
        self.assertTrue(d['status']['configured']);self.assertTrue(d['status']['enabled'])
        self.assertEqual(len(d['devices']),1);self.assertFalse(d['policy']['ok'])
        self.assertEqual(d['errors']['policy'],'invalid_rules');self.assertNotIn(self.secret,json.dumps(d))
    def test_bad_sources_do_not_erase_good_config_and_policy(self):
        self.m.atomic_json(self.m.SOURCES,{'sources':[{'name':self.secret}]})
        d=self.m.snapshot('panel')
        self.assertTrue(d['status']['enabled']);self.assertTrue(d['policy']['ok'])
        self.assertEqual(d['errors']['sources'],'local_sources_error');self.assertEqual(d['sources'],[])
        self.assertNotIn(self.secret,json.dumps(d))
    def test_unsafe_or_bad_config_is_local_and_not_echoed(self):
        os.chmod(self.m.CONFIG,0o644)
        d=self.m.snapshot();self.assertFalse(d['status']['configured']);self.assertEqual(d['devices'],[])
        self.assertTrue(d['policy']['ok']);self.assertNotIn(self.secret,json.dumps(d))
        os.chmod(self.m.CONFIG,0o600)
        for value in ['PRIVATE_INVALID',float('inf'),float('nan')]:
            cfg=self.m.config();cfg['resume_at']=value;self.m.atomic_json(self.m.CONFIG,cfg)
            d=self.m.snapshot();self.assertEqual(d['errors']['config'],'local_config_error')
            self.assertFalse(d['status']['configured']);self.assertNotIn('PRIVATE',json.dumps(d))
    def test_snapshot_is_read_only_for_all_views(self):
        self.m.log('accepted','test');self.m.atomic_json(self.m.RULES,self.m.rules())
        self.m.modify_sources(lambda entries:None)
        paths=[self.m.CONFIG,self.m.LOG,self.m.RULES,self.m.SOURCES]
        before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths}
        for view in ['menu','panel','history']:self.m.snapshot(view)
        self.assertEqual(before,{str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in paths})
    def test_latest_matches_previous_overview_last_200_semantics(self):
        self.m.log('accepted','complete',source_id='codex')
        for _ in range(199):self.m.log('enabled','control')
        self.assertEqual(self.m.latest_event()['kind'],'complete')
        self.m.log('enabled','control');self.assertIsNone(self.m.latest_event())
    def test_history_failure_is_isolated_from_config_and_policy(self):
        with patch.object(self.m,'history_lines',side_effect=OSError(self.secret)):
            for view in ['panel','history']:
                d=self.m.snapshot(view);self.assertTrue(d['status']['enabled']);self.assertTrue(d['policy']['ok'])
                self.assertEqual(d['errors']['history'],'history_unavailable');self.assertNotIn(self.secret,json.dumps(d))
    def test_old_status_output_remains_compatible(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:self.m.status()
        self.assertEqual(json.loads(out.getvalue()),self.m.snapshot('menu')['status'])
        self.assertNotIn(self.secret,out.getvalue())
    def test_cli_all_views_are_single_safe_json_responses(self):
        for view in ['menu','panel','history']:
            p=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'snapshot','--view',view],capture_output=True,text=True,timeout=2)
            self.assertEqual((p.returncode,p.stderr),(0,''));d=json.loads(p.stdout)
            self.assertTrue(d['ok']);self.assertEqual(d['version'],1);self.assertNotIn(self.secret,p.stdout)

if __name__=='__main__':unittest.main(verbosity=2)
