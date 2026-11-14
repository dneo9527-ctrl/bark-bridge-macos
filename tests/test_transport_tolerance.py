import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures


class DelayedTLS:
    def __init__(self,root,delay=2.5):
        self.delay=delay;self.stop=threading.Event();self.clients=[]
        root=Path(root);self.cert=root/'cert.pem';key=root/'key.pem';conf=root/'tls.cnf'
        conf.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n')
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(key),'-out',str(self.cert),'-config',str(conf)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        self.ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.ctx.load_cert_chain(self.cert,key)
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen(4);self.sock.settimeout(.2)
        self.url='https://127.0.0.1:'+str(self.sock.getsockname()[1])

    def handler(self,conn):
        try:
            with self.ctx.wrap_socket(conn,server_side=True,do_handshake_on_connect=False) as s:
                s.settimeout(5);time.sleep(self.delay);s.do_handshake()
                data=b''
                while b'\r\n\r\n' not in data:
                    part=s.recv(4096)
                    if not part:return
                    data+=part
                head,body=data.split(b'\r\n\r\n',1)
                match=re.search(br'(?im)^content-length:\s*(\d+)',head);length=int(match.group(1)) if match else 0
                while len(body)<length:
                    part=s.recv(4096)
                    if not part:return
                    body+=part
                response=b'{"code":200}'
                s.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(response)).encode()+b'\r\nConnection: close\r\n\r\n'+response)
        except (OSError,ssl.SSLError):pass

    def accept(self):
        while not self.stop.is_set():
            try:conn,_=self.sock.accept()
            except socket.timeout:continue
            except OSError:break
            t=threading.Thread(target=self.handler,args=(conn,),daemon=True);self.clients.append(t);t.start()

    def __enter__(self):
        self.thread=threading.Thread(target=self.accept,daemon=True);self.thread.start();return self

    def __exit__(self,*_):
        self.stop.set();self.sock.close();self.thread.join(1)
        for t in self.clients:t.join(6)


class TransportTests(fixtures.NotifierTests):
    def test_handshake_25_seconds_old_budget_fails_new_budget_succeeds(self):
        old=fixtures.module('old_transport',Path(__file__).parent/'transport-tolerance/before/bark.py')
        with DelayedTLS(self.base) as server,patch.dict(os.environ,{'CURL_CA_BUNDLE':str(server.cert),'NO_PROXY':'127.0.0.1'}):
            device={'device_key':self.secret,'server':server.url}
            self.assertEqual(old.send_target(device,'fixture','fixture','fixture'),(False,0,'timeout'))
            self.assertEqual(self.m.send_target(device,'fixture','fixture','fixture'),(True,200,'accepted'))

    def test_hook_still_returns_before_delayed_network_finishes(self):
        with DelayedTLS(self.base) as server,patch.dict(os.environ,{'CURL_CA_BUNDLE':str(server.cert),'NO_PROXY':'127.0.0.1'}):
            self.m.atomic_json(self.m.CONFIG,{'server':server.url,'device_key':self.secret,'enabled':True})
            start=time.monotonic()
            r=subprocess.run([sys.executable,str(self.base/'bark.py'),'hook'],input=json.dumps({'hook_event_name':'PermissionRequest','session_id':'delayed-fixture','tool_name':'Bash','tool_input':{'command':'PRIVATE_COMMAND'}}),capture_output=True,text=True,timeout=1)
            self.assertLess(time.monotonic()-start,1);self.assertEqual((r.returncode,r.stdout,r.stderr),(0,'',''))
            deadline=time.monotonic()+8
            while not self.m.LOG.exists() and time.monotonic()<deadline:time.sleep(.05)
            row=json.loads(self.m.LOG.read_text().splitlines()[-1]);self.assertEqual(row['status'],'accepted');self.assertNotIn('PRIVATE_COMMAND',self.m.LOG.read_text())

    def test_bounds_and_no_retry_or_secret_in_arguments(self):
        with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'{"code":200}\n200',b'')) as run:
            self.m.send_target({'server':'https://example.test','device_key':self.secret},'title','body','group')
            args=run.call_args.args[0];self.assertEqual(args[args.index('--connect-timeout')+1],'4');self.assertEqual(args[args.index('--max-time')+1],'8');self.assertEqual(args[args.index('--retry')+1],'0');self.assertEqual(run.call_args.kwargs['timeout'],9)
            self.assertEqual(run.call_count,1);self.assertNotIn(self.secret,str(args))

    def test_timeout_remains_fixed_reason_and_attempted_once(self):
        with patch.object(self.m.subprocess,'run',side_effect=subprocess.TimeoutExpired('curl',9)) as run:
            self.assertEqual(self.m.send_target({'server':'https://example.test','device_key':self.secret},'title','body','group'),(False,0,'timeout'))
            self.assertEqual(run.call_count,1)

if __name__=='__main__':
    suite=unittest.TestSuite(TransportTests(n) for n in TransportTests.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
