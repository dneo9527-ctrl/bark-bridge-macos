import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures

class PreviewTests(fixtures.NotifierTests):
    def setUp(self):
        super().setUp()
        self.ident='11111111-2222-4333-8444-555555555555'
        self.home=self.base/'codex';self.home.mkdir()
        self.db=self.home/'state_5.sqlite'
        c=sqlite3.connect(self.db)
        c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,name TEXT,title TEXT,first_user_message TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?)',(self.ident,'【示例聊天】','PRIVATE_INITIAL_PROMPT\n'*50,'PRIVATE_TRANSCRIPT'))
        c.commit();c.close()
        self.env=patch.dict(os.environ,{'CODEX_HOME':str(self.home)});self.env.start()
        self.event={'kind':'question','session':self.m.digest(self.ident),'fingerprint':'preview-1','chat_id':self.ident,'questions':[{'question':'选择哪一种方案？','options':['保留现状','采用新方案']}]}

    def tearDown(self):
        self.env.stop();super().tearDown()

    def payload(self,event=None):
        with patch.object(self.m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'{"code":200}\n200',b'')) as run:
            self.assertEqual(self.m.deliver(dict(event or self.event)),'accepted')
            self.assertNotIn('选择哪一种',str(run.call_args.args))
            return json.loads(run.call_args.kwargs['input'])

    def test_display_name_and_questions_use_stdin_not_logs_or_state(self):
        p=self.payload();self.assertIn('【示例聊天】',p['title']);self.assertEqual(p['body'],'选择哪一种方案？\n1. 保留现状\n2. 采用新方案')
        for path in [self.m.LOG,self.m.STATE]:
            data=path.read_bytes();self.assertNotIn('示例聊天'.encode(),data);self.assertNotIn('选择哪一种'.encode(),data);self.assertNotIn(b'PRIVATE_TRANSCRIPT',data)
        self.assertNotIn('PRIVATE_INITIAL',json.dumps(p))

    def test_name_rename_is_reflected_without_cache(self):
        self.assertEqual(self.m.chat_name(self.event),'【示例聊天】')
        c=sqlite3.connect(self.db);c.execute('UPDATE threads SET name=?',('重命名后',));c.commit();c.close()
        self.assertEqual(self.m.chat_name(self.event),'重命名后')

    def test_two_chats_never_share_titles(self):
        other='22222222-3333-4444-8555-666666666666'
        c=sqlite3.connect(self.db);c.execute('INSERT INTO threads VALUES(?,?,?,?)',(other,'另一个聊天','短标题','PRIVATE'));c.commit();c.close()
        self.assertEqual(self.m.chat_name({'chat_id':other}),'另一个聊天')
        self.assertEqual(self.m.chat_name(self.event),'【示例聊天】')

    def test_locked_metadata_database_falls_back_promptly(self):
        import time
        c=sqlite3.connect(self.db);c.execute('BEGIN EXCLUSIVE')
        try:
            start=time.monotonic();self.assertEqual(self.m.chat_name(self.event),'聊天 11111111');self.assertLess(time.monotonic()-start,.3)
        finally:c.rollback();c.close()

    def test_future_schema_falls_back_without_reading_unrelated_tables(self):
        c=sqlite3.connect(self.home/'state_6.sqlite');c.execute('CREATE TABLE unrelated(secret TEXT)');c.execute('INSERT INTO unrelated VALUES(?)',('PRIVATE',));c.commit();c.close()
        self.assertEqual(self.m.chat_name(self.event),'聊天 11111111')

    def test_long_prompt_title_rejected_and_legacy_short_title_works(self):
        c=sqlite3.connect(self.db);c.execute('UPDATE threads SET name=NULL');c.commit();c.close()
        self.assertEqual(self.m.chat_name(self.event),'聊天 11111111')
        c=sqlite3.connect(self.db);c.execute('UPDATE threads SET title=?',('短标题',));c.commit();c.close()
        self.assertEqual(self.m.chat_name(self.event),'短标题')

    def test_missing_or_symlink_database_is_not_created_or_followed(self):
        self.db.rename(self.home/'other.sqlite');self.assertEqual(self.m.chat_name(self.event),'聊天 11111111');self.assertFalse(self.db.exists())
        self.db.symlink_to(self.home/'other.sqlite');self.assertEqual(self.m.chat_name(self.event),'聊天 11111111')

    def test_readonly_database_and_only_expected_columns(self):
        before=self.db.stat().st_mtime_ns;real=self.m.sqlite3.connect;connections=[]
        def connect(*args,**kw):
            self.assertIn('mode=ro',args[0]);c=real(*args,**kw)
            c.set_authorizer(lambda action,a,b,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and b=='first_user_message' else sqlite3.SQLITE_OK)
            connections.append(c);return c
        with patch.object(self.m.sqlite3,'connect',side_effect=connect):self.assertEqual(self.m.chat_name(self.event),'【示例聊天】')
        self.assertTrue(connections);self.assertEqual(before,self.db.stat().st_mtime_ns)

    def test_question_tool_schemas_and_unknown_fields(self):
        a=self.m.event_from_hook({'hook_event_name':'PreToolUse','tool_name':'functions.request_user_input_async','session_id':self.ident,'tool_input':{'questions':[{'title':'异步问题','options':['选项甲','选项乙']}],'command':'PRIVATE_COMMAND'}})
        b=self.m.event_from_hook({'hook_event_name':'PreToolUse','tool_name':'request_user_input','session_id':self.ident,'tool_input':{'questions':[{'question':'同步问题','header':'标签','options':[{'label':'甲','description':'PRIVATE_DESCRIPTION'}]}]}})
        self.assertEqual(a['questions'][0]['question'],'异步问题');self.assertEqual(b['questions'][0]['options'],['甲'])
        self.assertNotIn('PRIVATE',json.dumps(a)+json.dumps(b));self.assertEqual(a['chat_id'],self.ident)
        self.assertEqual(self.m.question_preview({'body':'PRIVATE_BODY','command':'PRIVATE_COMMAND'}),[])

    def test_authorization_shows_tool_but_never_command_or_args(self):
        e=self.m.event_from_hook({'hook_event_name':'PermissionRequest','session_id':self.ident,'tool_name':'Bash','tool_input':{'command':'PRIVATE_COMMAND','description':'PRIVATE_REASON','question':'DO_NOT_USE'}})
        p=self.payload(e);self.assertIn('等待授权',p['title']);self.assertIn('Bash',p['body']);self.assertNotIn('PRIVATE',json.dumps(p));self.assertNotIn('DO_NOT_USE',json.dumps(p))

    def test_completion_includes_chat_without_response(self):
        e=self.m.event_from_hook({'hook_event_name':'Stop','session_id':self.ident,'turn_id':'turn','last_assistant_message':'PRIVATE_RESPONSE'})
        p=self.payload(e);self.assertIn('示例聊天',p['title']);self.assertIn('本轮已完成',p['title']);self.assertNotIn('PRIVATE_RESPONSE',json.dumps(p))

    def test_preview_off_is_generic_and_skips_name_lookup(self):
        self.m.set_codex_preview(False)
        with patch.object(self.m,'chat_name',side_effect=AssertionError('must not lookup')):p=self.payload()
        self.assertEqual(p['title'],'Codex 等待回答');self.assertNotIn('选择哪一种',p['body'])

    def test_preview_setting_survives_other_rule_edits(self):
        self.m.set_codex_preview(False);self.m.set_codex_events({'complete':False});self.m.set_quiet({'enabled':False,'start':'22:00','end':'08:00'});self.m.set_route('codex',{'mode':'selected','device_ids':['primary']})
        self.assertFalse(self.m.rules()['codex_preview']);self.assertFalse(self.m.rules()['codex_events']['complete']);self.assertEqual(self.m.rules()['routes']['codex'],['primary'])
        self.m.set_codex_preview(True);self.assertEqual(self.m.rules()['routes']['codex'],['primary'])
        with self.assertRaises(ValueError):self.m.set_codex_preview('on')

    def test_bounded_utf8_and_transport_size(self):
        self.event['questions']=[{'question':'问题😀'*500,'options':['选项😀'*500]*10}]*5
        e=self.m.event_from_hook({'hook_event_name':'PreToolUse','tool_name':'request_user_input_async','session_id':self.ident,'tool_input':{'questions':self.event['questions']}})
        self.assertEqual(len(e['questions']),2);self.assertLess(len(json.dumps(e,ensure_ascii=False).encode()),4096)
        p=self.payload(e);self.assertLessEqual(len(p['body'].encode()),1400);self.assertLessEqual(len(p['title'].encode()),180);self.assertLess(len(json.dumps(p,ensure_ascii=False).encode()),3500)

    def test_common_secrets_links_and_secret_question_are_masked(self):
        text='地址 https://api.day.app/FAKE_DEVICE_KEY/title 密码=FAKE_PASSWORD token:FAKE_TOKEN Bearer FAKE_BEARER sk-proj-FAKEKEY12345678901234567890'
        clean=self.m.preview_text(text,1000)
        for secret in ['FAKE_DEVICE_KEY','FAKE_PASSWORD','FAKE_TOKEN','FAKE_BEARER','FAKEKEY']:self.assertNotIn(secret,clean)
        self.assertNotIn('SECRET_INPUT',json.dumps(self.m.question_preview({'questions':[{'question':'SECRET_INPUT','isSecret':True}]})))
        self.assertEqual(self.m.preview_text('x\ud800y',100),'x y')

    def test_enriched_skill_and_hook_still_coalesce(self):
        self.assertTrue(self.m.claim(self.event))
        e=self.m.event_from_hook({'hook_event_name':'PreToolUse','tool_name':'request_user_input_async','session_id':self.ident,'tool_input':{'questions':[{'title':'工具问题'}]}})
        self.assertFalse(self.m.claim(e))

    def test_cli_preview_uses_stdin_and_keeps_old_wait(self):
        data=json.dumps({'questions':[{'question':'CLI问题','options':['甲','乙']}]})
        with patch.object(self.m.sys,'argv',['bark.py','wait','--preview-json']),patch.object(self.m.sys,'stdin',io.TextIOWrapper(io.BytesIO(data.encode()))),patch.object(self.m,'dispatch') as dispatch:
            self.m.main();self.assertEqual(dispatch.call_args.args[0]['questions'][0]['question'],'CLI问题')
        with patch.object(self.m.sys,'argv',['bark.py','wait']),patch.object(self.m,'dispatch') as dispatch:
            self.m.main();self.assertNotIn('questions',dispatch.call_args.args[0])

    def test_invalid_preview_falls_back_to_notification(self):
        with patch.object(self.m.sys,'argv',['bark.py','wait','--preview-json']),patch.object(self.m.sys,'stdin',io.TextIOWrapper(io.BytesIO(b'{broken'))),patch.object(self.m,'dispatch') as dispatch:
            self.m.main();self.assertEqual(dispatch.call_count,1);self.assertNotIn('questions',dispatch.call_args.args[0])

    def test_interactive_stdin_does_not_echo_question(self):
        import pty,termios,time
        with __import__('contextlib').redirect_stdout(io.StringIO()):self.m.set_enabled(False)
        master,slave=pty.openpty()
        try:
            child=subprocess.Popen([sys.executable,str(self.base/'bark.py'),'wait','--preview-json'],stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline=time.monotonic()+2
            while termios.tcgetattr(slave)[3] & termios.ECHO:
                if time.monotonic()>deadline:self.fail('stdin did not disable echo')
                time.sleep(.01)
            os.write(master,b'{"questions":[{"question":"PRIVATE_TTY_QUESTION"}]}\n')
            stdout,stderr=child.communicate(timeout=2)
            self.assertEqual((child.returncode,stdout,stderr),(0,b'',b''))
            os.set_blocking(master,False)
            try:echo=os.read(master,4096)
            except (BlockingIOError,OSError):echo=b''
            self.assertNotIn(b'PRIVATE_TTY_QUESTION',echo)
        finally:os.close(master);os.close(slave)

if __name__=='__main__':
    # Run only new tests here; inherited baseline tests are run by their own suite.
    suite=unittest.TestSuite(PreviewTests(n) for n in PreviewTests.__dict__ if n.startswith('test_'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
