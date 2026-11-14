#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BARK_SRC_DIR = Path(os.environ.get("BARK_TEST_SOURCE", str(Path(__file__).resolve().parents[1] / "src/runtime")))
ADAPTER_SRC = BARK_SRC_DIR / "claude_bark.py"


class ClaudeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app_dir = self.root / "app"
        self.claude_dir = self.root / "claude_config"
        self.bin_dir = self.root / "bin"
        self.app_dir.mkdir(mode=0o700)
        self.claude_dir.mkdir(mode=0o700)
        self.bin_dir.mkdir(mode=0o700)

        bark_candidate = BARK_SRC_DIR / "bark.py"
        local_bark = Path(__file__).resolve().parent / "bark.py"
        if bark_candidate.exists():
            shutil.copy2(bark_candidate, self.app_dir / "bark.py")
        elif local_bark.exists():
            shutil.copy2(local_bark, self.app_dir / "bark.py")
        else:
            raise RuntimeError("bark.py not found for test fixture")

        shutil.copy2(ADAPTER_SRC, self.app_dir / "claude_bark.py")
        os.chmod(self.app_dir / "bark.py", 0o600)
        os.chmod(self.app_dir / "claude_bark.py", 0o600)

        self.fake_claude = self.bin_dir / "claude"
        self.fake_claude.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo \"2.1.34 (Claude Code)\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        os.chmod(self.fake_claude, 0o700)

        self.env_patch = mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": str(self.claude_dir)},
            clear=False,
        )
        self.env_patch.start()

        sys.path.insert(0, str(self.app_dir))
        self.bark = self._load_module("bark", self.app_dir / "bark.py")
        self.adapter = self._load_module("claude_bark", self.app_dir / "claude_bark.py")
        self.adapter.CLAUDE_BIN = self.fake_claude

    def tearDown(self) -> None:
        self.env_patch.stop()
        if str(self.app_dir) in sys.path:
            sys.path.remove(str(self.app_dir))
        self.tmp.cleanup()

    @staticmethod
    def _load_module(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_merge_preserves_unrelated_settings_and_backups_only_hooks(self) -> None:
        spath = self.adapter.settings_path()
        initial = {
            "model": "claude-sonnet-4-20250514",
            "env": {"ANTHROPIC_API_KEY": "sk-ant-super-secret-token"},
            "permissions": {"allow": ["Read"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "^Bash$", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}
                ],
                "Notification": [
                    {"hooks": [{"type": "command", "command": "/usr/bin/printf notify"}]}
                ],
            },
        }
        spath.write_text(json.dumps(initial), encoding="utf-8")
        os.chmod(spath, 0o600)

        res = self.adapter.attach()
        self.assertEqual(res, {"ok": True, "configured": True, "reason": "attached"})

        st_mode = stat.S_IMODE(os.lstat(spath).st_mode)
        self.assertEqual(st_mode, 0o600)

        merged = json.loads(spath.read_text(encoding="utf-8"))
        self.assertEqual(merged["model"], "claude-sonnet-4-20250514")
        self.assertEqual(merged["env"]["ANTHROPIC_API_KEY"], "sk-ant-super-secret-token")
        self.assertEqual(merged["permissions"], {"allow": ["Read"]})
        self.assertEqual(merged["hooks"]["Notification"], initial["hooks"]["Notification"])
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 2)
        self.assertTrue(self.adapter.is_configured(merged))

        merged_raw = json.dumps(merged["hooks"])
        self.assertNotIn("decision", merged_raw)
        self.assertNotIn("async", merged_raw)

        mpath = self.adapter.manifest_path()
        self.assertTrue(mpath.exists())
        self.assertEqual(stat.S_IMODE(os.lstat(mpath).st_mode), 0o600)
        manifest_raw = mpath.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant-super-secret-token", manifest_raw)
        self.assertNotIn("ANTHROPIC_API_KEY", manifest_raw)
        manifest = json.loads(manifest_raw)
        self.assertEqual(manifest["original_hooks"], initial["hooks"])

    def test_attach_is_idempotent(self) -> None:
        self.assertEqual(self.adapter.attach()["reason"], "attached")
        first_settings = json.loads(self.adapter.settings_path().read_text(encoding="utf-8"))
        first_manifest = json.loads(self.adapter.manifest_path().read_text(encoding="utf-8"))

        self.assertEqual(self.adapter.attach()["reason"], "attached")
        second_settings = json.loads(self.adapter.settings_path().read_text(encoding="utf-8"))
        second_manifest = json.loads(self.adapter.manifest_path().read_text(encoding="utf-8"))

        self.assertEqual(first_settings["hooks"], second_settings["hooks"])
        for event_name in ("PermissionRequest", "PreToolUse", "Stop"):
            self.assertEqual(len(second_settings["hooks"][event_name]), 1)
        self.assertFalse(second_manifest["hooks_existed"])
        self.assertIsNone(second_manifest["original_hooks"])
        self.assertEqual(first_manifest["original_hooks"], second_manifest["original_hooks"])

    def test_detach_preserves_third_party_hooks_and_works_without_cli(self) -> None:
        spath = self.adapter.settings_path()
        spath.write_text(
            json.dumps(
                {
                    "env": {"FOO": "BAR"},
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/true keep-stop"}]}]
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(spath, 0o600)
        self.assertTrue(self.adapter.attach()["ok"])

        current = json.loads(spath.read_text(encoding="utf-8"))
        current["hooks"]["PostToolUse"] = [
            {"hooks": [{"type": "command", "command": "/usr/bin/true keep-post"}]}
        ]
        spath.write_text(json.dumps(current), encoding="utf-8")
        os.chmod(spath, 0o600)

        # Remove CLI and ensure detach still works
        self.adapter.CLAUDE_BIN = self.bin_dir / "nonexistent-claude"
        res = self.adapter.detach()
        self.assertEqual(res, {"ok": True, "configured": False, "reason": "detached"})

        after = json.loads(spath.read_text(encoding="utf-8"))
        self.assertEqual(after["env"], {"FOO": "BAR"})
        self.assertEqual(
            after["hooks"]["Stop"],
            [{"hooks": [{"type": "command", "command": "/usr/bin/true keep-stop"}]}],
        )
        self.assertEqual(
            after["hooks"]["PostToolUse"],
            [{"hooks": [{"type": "command", "command": "/usr/bin/true keep-post"}]}],
        )
        self.assertNotIn("PermissionRequest", after["hooks"])
        self.assertNotIn("PreToolUse", after["hooks"])

    def test_rejects_symlink_and_unsafe_permissions_and_invalid_json(self) -> None:
        spath = self.adapter.settings_path()
        target = self.root / "outside.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o600)
        spath.symlink_to(target)

        self.assertEqual(self.adapter.status()["reason"], "unsafe_permissions")
        self.assertEqual(self.adapter.attach()["reason"], "unsafe_permissions")
        spath.unlink()

        spath.write_text('{"model": "ok"}', encoding="utf-8")
        os.chmod(spath, 0o666)
        self.assertEqual(self.adapter.status()["reason"], "unsafe_permissions")
        self.assertEqual(self.adapter.attach()["reason"], "unsafe_permissions")
        self.assertEqual(self.adapter.detach()["reason"], "unsafe_permissions")

        os.chmod(spath, 0o600)
        spath.write_text("{corrupt json", encoding="utf-8")
        self.assertEqual(self.adapter.status()["reason"], "invalid_settings")
        self.assertEqual(self.adapter.attach()["reason"], "invalid_settings")
        self.assertEqual(spath.read_text(encoding="utf-8"), "{corrupt json")

    def test_missing_cli_blocks_attach_without_touching_settings(self) -> None:
        self.adapter.CLAUDE_BIN = self.bin_dir / "missing-claude"
        st = self.adapter.status()
        self.assertEqual(
            st,
            {
                "ok": True,
                "available": False,
                "version": None,
                "configured": False,
                "reason": "cli_missing",
            },
        )
        att = self.adapter.attach()
        self.assertEqual(att, {"ok": False, "configured": False, "reason": "cli_missing"})
        self.assertFalse(self.adapter.settings_path().exists())

    def test_event_mapping_permission_and_question_scrubs_sensitive_fields(self) -> None:
        perm_payload = {
            "session_id": "sess-raw-001",
            "cwd": "/Users/example/private/dirs/my-awesome-repo",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "curl -H 'Authorization: Bearer SECRET_TOKEN' https://example.com"},
        }
        ev = self.adapter.event_from_claude_hook(json.dumps(perm_payload).encode("utf-8"))
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev["kind"], "permission")
        self.assertEqual(ev["source_id"], "claude-code")
        self.assertEqual(ev["source"], "Claude Code")
        self.assertEqual(ev["tool"], "Bash")
        self.assertEqual(ev["project_label"], "my-awesome-repo")
        self.assertNotIn("sess-raw-001", json.dumps(ev))
        self.assertNotIn("/Users/example/private", json.dumps(ev))
        self.assertNotIn("SECRET_TOKEN", json.dumps(ev))
        self.assertNotIn("chat_id", ev)

        ask_payload = {
            "session_id": "sess-raw-001",
            "cwd": "/Users/example/private/dirs/my-awesome-repo",
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "Which environment should we deploy to?",
                        "options": [{"label": "Staging"}, {"label": "Production"}],
                    }
                ]
            },
        }
        q_ev = self.adapter.event_from_claude_hook(json.dumps(ask_payload).encode("utf-8"))
        self.assertIsNotNone(q_ev)
        assert q_ev is not None
        self.assertEqual(q_ev["kind"], "question")
        self.assertTrue(len(q_ev.get("questions", [])) == 1)

        other_pre = dict(ask_payload, tool_name="Bash")
        self.assertIsNone(self.adapter.event_from_claude_hook(json.dumps(other_pre).encode("utf-8")))

    def test_event_mapping_stop_dedup_and_stop_hook_active_guard(self) -> None:
        active_stop = {
            "session_id": "sess-stop-1",
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "last_assistant_message": "Still continuing",
        }
        self.assertIsNone(self.adapter.event_from_claude_hook(json.dumps(active_stop).encode("utf-8")))

        normal_stop = {
            "session_id": "sess-stop-1",
            "cwd": "/tmp/work/demo-project",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": "Task finished with secret=12345",
        }
        ev1 = self.adapter.event_from_claude_hook(json.dumps(normal_stop).encode("utf-8"))
        ev2 = self.adapter.event_from_claude_hook(json.dumps(normal_stop).encode("utf-8"))
        self.assertIsNotNone(ev1)
        assert ev1 is not None and ev2 is not None
        self.assertEqual(ev1["kind"], "complete")
        self.assertEqual(ev1["fingerprint"], ev2["fingerprint"])
        self.assertNotIn("12345", json.dumps(ev1))

        no_msg_stop = {"session_id": "sess-stop-1", "hook_event_name": "Stop"}
        with mock.patch("time.time", return_value=2400.0):
            ev_bucket = self.adapter.event_from_claude_hook(json.dumps(no_msg_stop).encode("utf-8"))
        self.assertIsNotNone(ev_bucket)

    def test_hook_cli_always_exits_zero_silently(self) -> None:
        dispatched: list[dict] = []
        with mock.patch.object(self.bark, "dispatch", side_effect=lambda e: dispatched.append(e)):
            payload = json.dumps(
                {
                    "session_id": "s1",
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Bash",
                }
            ).encode("utf-8")
            stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
            fake_stdin = mock.Mock(buffer=io.BytesIO(payload))
            with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(
                sys, "stdout", stdout_buf
            ), mock.patch.object(sys, "stderr", stderr_buf):
                rc = self.adapter.main(["hook"])
            self.assertEqual(rc, 0)
            self.assertEqual(stdout_buf.getvalue(), "")
            self.assertEqual(stderr_buf.getvalue(), "")
            self.assertEqual(len(dispatched), 1)

        # Malformed input and internal dispatch exceptions still return 0 silently
        with mock.patch.object(self.bark, "dispatch", side_effect=RuntimeError("boom")):
            for bad_bytes in (b"{bad json", b"x" * (self.adapter.MAX_STDIN_BYTES + 10), payload):
                stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
                fake_stdin = mock.Mock(buffer=io.BytesIO(bad_bytes))
                with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(
                    sys, "stdout", stdout_buf
                ), mock.patch.object(sys, "stderr", stderr_buf):
                    rc = self.adapter.main(["hook"])
                self.assertEqual(rc, 0)
                self.assertEqual(stdout_buf.getvalue(), "")
                self.assertEqual(stderr_buf.getvalue(), "")

    def test_same_basename_other_script_is_preserved(self):
        other = "/usr/bin/python3 /another/vendor/claude_bark.py hook"
        data = {"hooks":{"Stop":[{"hooks":[{"type":"command","command":other}]},{"hooks":[]}]}}
        merged = self.adapter.merge_hooks_into_settings(data)
        cleaned = self.adapter.remove_hooks_from_settings(merged)
        self.assertEqual(cleaned, data)

    def test_missing_cli_status_does_not_read_settings(self):
        self.adapter.CLAUDE_BIN = self.bin_dir / "missing"
        with mock.patch.object(self.adapter, "read_safe_json_dict", side_effect=AssertionError("no settings read")):
            self.assertEqual(self.adapter.status()["reason"],"cli_missing")

    def test_detach_uses_registered_target_and_attach_other_profile_refuses(self):
        original = self.adapter.settings_path()
        self.assertTrue(self.adapter.attach()["ok"])
        alternate = self.root / "alternate" / "settings.json"
        self.adapter.SETTINGS_PATH = alternate
        self.assertEqual(self.adapter.attach()["reason"],"other_configuration_registered")
        self.assertTrue(self.adapter.detach()["ok"])
        self.assertFalse(original.exists())

    def test_relative_configuration_path_has_fixed_reason(self):
        self.adapter.SETTINGS_PATH = None
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR":"relative/path"}):
            for fn in [self.adapter.status, self.adapter.attach, self.adapter.detach]:
                self.assertEqual(fn()["reason"],"invalid_settings")

    def test_missing_manifest_still_cleans_our_existing_hooks(self):
        self.assertTrue(self.adapter.attach()["ok"])
        self.adapter.manifest_path().unlink()
        self.adapter.CLAUDE_BIN = self.bin_dir / "missing"
        self.assertTrue(self.adapter.status()["configured"])
        self.assertTrue(self.adapter.detach()["ok"])
        current = json.loads(self.adapter.settings_path().read_text())
        self.assertFalse(self.adapter.has_any_our_hook(current))


if __name__ == "__main__":
    unittest.main()
