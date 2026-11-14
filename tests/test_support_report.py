import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures

manage=fixtures.module('abd_manage',fixtures.SRC/'manage.py')

class SupportReportTests(unittest.TestCase):
    def test_report_strict_allowlist(self):
        secret='PRIVATE_CANARY_DO_NOT_EXPORT'
        snap={'ok':True,'status':{'configured':True,'enabled':True,'config_file':secret},'devices':[{'name':secret,'device_key':secret}], 'events':[
            {'status':'partial','kind':'question','reason':'multiple_failures','chat_title':secret,'body':secret,'device_results':[
                {'device_id':secret,'name':secret,'reason':'accepted','http':200}, {'reason':'timeout','server':secret}]},
            {'status':'source_muted','category':'skipped'},
            {'status':'send_failed','kind':secret,'reason':secret}, {'status':'partial','kind':'complete','reason':'partial'}]}
        health={'summary':'ready','checks':[{'status':'pass','code':'ok','title':secret,'detail':secret}, {'status':secret,'code':secret}]}
        result=manage.support_report(lambda:snap,lambda:health)
        raw=json.dumps(result)
        self.assertNotIn(secret,raw)
        self.assertNotIn('device_id',raw);self.assertNotIn('http',raw)
        report=result['report']
        self.assertEqual(report['checks'][1],{'item':2,'status':'unknown','code':'api_unknown'})
        self.assertEqual(report['recent_results']['skipped'],1)
        self.assertEqual(report['recent_failures'][0]['devices'][1]['reason'],'timeout')
        self.assertEqual(report['recent_failures'][1]['kind'],'application')
        self.assertEqual(report['recent_failures'][2]['reason'],'partial')
    def test_provider_failure_no_raw_exception(self):
        def fail():raise RuntimeError('PRIVATE_ERROR')
        report=manage.support_report(fail,fail)
        self.assertFalse(report['report']['snapshot_available'])
        self.assertEqual(report['report']['health_summary'],'unknown')
        self.assertNotIn('PRIVATE_ERROR',json.dumps(report))
    def test_reports_bounded_and_no_production_writes(self):
        rows=[{'status':'send_failed','kind':'permission','reason':'timeout','device_results':[{'reason':'timeout'}]*30}]*500
        report=manage.support_report(lambda:{'ok':True,'events':rows},lambda:{'checks':[{'status':'pass','code':'ok'}]*300})['report']
        self.assertEqual(len(report['checks']),100);self.assertEqual(len(report['recent_failures']),20)
        self.assertEqual(report['recent_results']['send_failed'],200)
        self.assertEqual(len(report['recent_failures'][0]['devices']),16)
    def test_version_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=Path(tmp);(app/'Contents').mkdir();info=app/'Contents/Info.plist'
            with patch.object(manage,'APP_PATH',app):
                info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'PRIVATE_VERSION'}))
                self.assertEqual(manage.support_report(lambda:{},lambda:{})['report']['app_version'],'unknown')
                info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'2.3.0'}))
                self.assertEqual(manage.support_report(lambda:{},lambda:{})['report']['app_version'],'2.3.0')
