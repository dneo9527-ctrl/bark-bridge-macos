import json
import os
import sqlite3
import unittest
from unittest.mock import patch
from test_chat_preview import PreviewTests

class IdentityTests(PreviewTests):
    def hook(self, **extra):
        data = {"hook_event_name":"Stop", "session_id":"33333333-4444-4555-8666-777777777777", "turn_id":"turn"}
        data.update(extra)
        return self.m.event_from_hook(data)

    def transcript(self):
        return "/not-opened/rollout-2026-11-14T21-23-00-"+self.ident+".jsonl"

    def test_transient_session_resolves_transcript_chat_and_dedup_identity(self):
        event = self.hook(transcript_path=self.transcript())
        self.assertEqual(event["chat_id"],self.ident)
        self.assertEqual(event["session"],self.m.digest(self.ident))
        self.assertEqual(self.m.chat_name(event),"【示例聊天】")
        self.assertNotIn("transcript_path",event)

    def test_explicit_thread_identity_wins_over_session_and_filename(self):
        event=self.hook(thread_id=self.ident, transcript_path="/invalid.jsonl")
        self.assertEqual(event["chat_id"],self.ident)

    def test_different_inherited_environment_never_relabels_known_session(self):
        with patch.dict(os.environ,{"CODEX_THREAD_ID":self.ident}):
            event=self.hook()
        self.assertEqual(event["chat_id"],"33333333-4444-4555-8666-777777777777")
        self.assertEqual(self.m.chat_name(event),"聊天 33333333")

    def test_opaque_session_does_not_borrow_environment_name(self):
        with patch.dict(os.environ,{"CODEX_THREAD_ID":self.ident}):
            event=self.hook(session_id="opaque-session")
        self.assertNotIn("chat_id",event)

    def test_missing_session_can_use_explicit_environment(self):
        with patch.dict(os.environ,{"CODEX_THREAD_ID":self.ident}):
            self.assertEqual(self.m.hook_chat_id({}),self.ident)

    def test_filename_validation_never_opens_transcript(self):
        for path in ["/tmp/"+self.ident+".jsonl",self.transcript()+".backup","/tmp/rollout-not-a-date-"+self.ident+".jsonl"]:
            self.assertEqual(self.hook(transcript_path=path)["chat_id"],"33333333-4444-4555-8666-777777777777")
        with patch.object(self.m.Path,"open",side_effect=AssertionError("no transcript open")):
            self.assertEqual(self.hook(transcript_path=self.transcript())["chat_id"],self.ident)

    def test_index_only_name_works_without_sqlite(self):
        self.db.unlink()
        index=self.home/"session_index.jsonl"
        index.write_text(json.dumps({"id":self.ident,"thread_name":"索引聊天名"})+"\n")
        before=index.stat().st_mtime_ns
        self.assertEqual(self.m.chat_name(self.event),"索引聊天名")
        self.assertEqual(index.stat().st_mtime_ns,before)

    def test_latest_index_rename_wins_and_unrelated_names_are_ignored(self):
        rows=[{"id":self.ident,"thread_name":"旧名称"},{"id":"other","thread_name":"OTHER"},{"id":self.ident,"thread_name":"新名称"}]
        (self.home/"session_index.jsonl").write_text("\n".join(json.dumps(x) for x in rows)+"\n")
        self.assertEqual(self.m.chat_name(self.event),"新名称")

    def test_bad_index_records_do_not_leak_messages_or_block_sqlite(self):
        rows=["broken",json.dumps([self.ident]),json.dumps({"id":self.ident,"thread_name":"PRIVATE\nPROMPT"}),json.dumps({"id":self.ident,"first_user_message":"PRIVATE_TRANSCRIPT"})]
        (self.home/"session_index.jsonl").write_text("\n".join(rows)+"\n")
        self.assertEqual(self.m.chat_name(self.event),"【示例聊天】")

    def test_index_symlink_is_not_followed(self):
        other=self.base/"index.jsonl";other.write_text(json.dumps({"id":self.ident,"thread_name":"WRONG_NAME"}))
        (self.home/"session_index.jsonl").symlink_to(other)
        self.assertEqual(self.m.chat_name(self.event),"【示例聊天】")

    def test_large_index_tail_is_bounded_and_skips_cut_record(self):
        index=self.home/"session_index.jsonl"
        index.write_bytes(b"x"*1100000+b"\n"+json.dumps({"id":self.ident,"thread_name":"尾部名称"}).encode()+b"\n")
        self.assertEqual(self.m.chat_name(self.event),"尾部名称")

    def test_question_hook_and_skill_coalesce_after_session_alias_resolution(self):
        hook={"hook_event_name":"PreToolUse","tool_name":"request_user_input","session_id":"33333333-4444-4555-8666-777777777777","transcript_path":self.transcript(),"tool_input":{"questions":[{"question":"测试"}]}}
        self.assertTrue(self.m.claim(self.event))
        self.assertFalse(self.m.claim(self.m.event_from_hook(hook)))

    def test_conflicting_explicit_thread_and_transcript_refuse_name(self):
        event=self.hook(thread_id="44444444-5555-4666-8777-888888888888",transcript_path=self.transcript())
        self.assertNotIn("chat_id",event)
        self.assertEqual(self.m.chat_name(event),"Codex")

    def test_group_writable_index_is_not_used(self):
        index=self.home/"session_index.jsonl"
        index.write_text(json.dumps({"id":self.ident,"thread_name":"UNSAFE_NAME"}))
        index.chmod(0o666)
        self.assertEqual(self.m.chat_name(self.event),"【示例聊天】")
