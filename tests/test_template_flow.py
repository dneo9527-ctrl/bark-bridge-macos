import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures
from test_transport_tolerance import DelayedTLS

class TemplateFlowTests(unittest.TestCase):
    setUp=fixtures.NotifierTests.setUp
    tearDown=fixtures.NotifierTests.tearDown
    def wait_for_event(self, expected):
        end=time.monotonic()+4
        while time.monotonic()<end:
            if self.m.LOG.exists():
                rows=[json.loads(x) for x in self.m.LOG.read_text().splitlines()]
                if any(x.get('source_id')==expected and x['status']=='accepted' for x in rows):return rows
            time.sleep(.025)
        self.fail('template worker did not finish')
    def test_wrapper_and_shortcut_command_complete_worker_transport(self):
        shutil.copy2(fixtures.SRC/'bark-run',self.base/'bark-run');os.chmod(self.base/'bark-run',0o700)
        self.m.enable_template('terminal');self.m.enable_template('shortcuts')
        with DelayedTLS(self.base,delay=.01) as server,patch.dict(os.environ,{'CURL_CA_BUNDLE':str(server.cert),'NO_PROXY':'127.0.0.1'}):
            self.m.atomic_json(self.m.CONFIG,{'server':server.url,'device_key':self.secret,'enabled':True})
            r=subprocess.run([str(self.base/'bark-run'),'/bin/sh','-c','printf "LOCAL_PRIVATE_OUTPUT"; exit 7'],capture_output=True,text=True,timeout=2)
            self.assertEqual(r.returncode,7);self.assertEqual(r.stdout,'LOCAL_PRIVATE_OUTPUT')
            rows=self.wait_for_event('terminal-task')
            r=subprocess.run(['/usr/bin/python3',str(self.base/'bark.py'),'template-notify','--template','shortcuts'],capture_output=True,text=True,timeout=2)
            self.assertEqual((r.returncode,r.stdout,r.stderr),(0,'',''))
            rows=self.wait_for_event('shortcuts')
        raw=self.m.LOG.read_text();self.assertNotIn('LOCAL_PRIVATE_OUTPUT',raw);self.assertNotIn(self.secret,raw)
        for sid in ['terminal-task','shortcuts']:
            self.assertEqual(next(x['status'] for x in self.m.source_list() if x['id']==sid),'observed')
