"""Temporary native-App Hook configuration only; no real user settings/network."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest import mock

import test_codex_bark as fixtures

manage = fixtures.module("native_antigravity_manage", fixtures.SRC / "manage.py")


class AntigravityAttachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / "runtime"
        self.base.mkdir(mode=0o700)
        self.hooks = self.root / "gemini/config/hooks.json"
        self.hooks.parent.mkdir(parents=True, mode=0o700)
        self.app = self.root / "Apps with spaces/Antigravity.app"
        contents = self.app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "com.google.antigravity", "CFBundleExecutable": "Antigravity",
            "CFBundleShortVersionString": "2.32.2", "CFBundlePackageType": "APPL"}))
        binary = contents / "MacOS/Antigravity"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        self.patchers = [mock.patch.object(manage, "BASE", self.base),
                         mock.patch.object(manage, "ANTIGRAVITY_HOOKS_PATH", self.hooks)]
        for p in self.patchers: p.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patchers)])

    def write(self, data):
        self.hooks.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self.hooks.chmod(0o600)

    def read(self): return json.loads(self.hooks.read_text())

    def test_attach_uses_native_named_schema_and_preserves_unrelated_policy(self):
        unrelated = {"third-party": {"enabled": False, "PreToolUse": [{"matcher": "run_command", "hooks": [
            {"type": "command", "command": "/bin/echo preserve-policy", "timeout": 9}]}]},
            "future-policy": {"mode": "preserve"}}
        self.write(unrelated)
        before = self.hooks.read_bytes()
        result = manage.antigravity_attach(self.app)
        self.assertEqual(result, {"ok": True, "configured": True, "reason": "attached"})
        data = self.read()
        for key, val in unrelated.items(): self.assertEqual(data[key], val)
        own = data[manage.ANTIGRAVITY_HOOK_NAME]
        self.assertEqual(set(own), {"enabled", "PreToolUse", "Stop"})
        self.assertEqual(own["PreToolUse"][0]["matcher"], r"^(ask_question|ask_permission)$")
        self.assertTrue(manage._antigravity_own_command(own["Stop"][0]["command"]))
        for token in ("decision", "auto_approve", "permissionOverrides", "async"):
            self.assertNotIn(token, json.dumps(own))
        self.assertEqual(self.hooks.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((self.base / "antigravity_installation.json").read_text())
        backup = Path(manifest["backups"][0]) / "hooks.json"
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("preserve-policy", json.dumps(manifest))

    def test_attach_is_idempotent_without_new_backup_or_rewrite(self):
        self.assertTrue(manage.antigravity_attach(self.app)["ok"])
        before = self.hooks.read_bytes(), self.hooks.stat().st_mtime_ns
        manifest = (self.base / "antigravity_installation.json").read_bytes()
        self.assertTrue(manage.antigravity_attach(self.app)["ok"])
        self.assertEqual((self.hooks.read_bytes(), self.hooks.stat().st_mtime_ns), before)
        self.assertEqual((self.base / "antigravity_installation.json").read_bytes(), manifest)
        self.assertTrue(manage.antigravity_status()["configured"])

    def test_disabled_own_hook_stays_disabled(self):
        manage.antigravity_attach(self.app)
        data = self.read(); data[manage.ANTIGRAVITY_HOOK_NAME]["enabled"] = False; self.write(data)
        self.assertTrue(manage.antigravity_attach(self.app)["ok"])
        self.assertFalse(self.read()[manage.ANTIGRAVITY_HOOK_NAME]["enabled"])
        result = manage.antigravity_status()
        self.assertTrue(result["configured"]); self.assertFalse(result["enabled"])
        self.assertEqual(result["reason"], "disabled")

    def test_same_name_unrelated_hook_refuses_without_changes(self):
        self.write({manage.ANTIGRAVITY_HOOK_NAME: {"Stop": [{"command": "/usr/bin/true"}]}})
        before = self.hooks.read_bytes()
        self.assertEqual(manage.antigravity_attach(self.app)["reason"], "adapter_conflict")
        self.assertEqual(self.hooks.read_bytes(), before)
        self.assertFalse((self.base / "antigravity_installation.json").exists())

    def test_attach_and_detach_preserve_user_additions_inside_named_group(self):
        manage.antigravity_attach(self.app)
        data = self.read(); own = data[manage.ANTIGRAVITY_HOOK_NAME]
        own["Stop"].append({"command": "/bin/echo keep-stop"})
        own["PreToolUse"][0]["hooks"].append({"command": "/bin/echo keep-pre"})
        own["PostInvocation"] = [{"command": "/bin/echo keep-post"}]
        own["user_note"] = "preserve"
        data["later-third-party"] = {"Stop": [{"command": "/bin/echo keep-third"}]}
        self.write(data)
        self.assertTrue(manage.antigravity_attach(self.app)["ok"])
        self.assertTrue(manage.antigravity_detach()["ok"])
        after = self.read()
        self.assertEqual(after["later-third-party"], data["later-third-party"])
        remaining = after[manage.ANTIGRAVITY_HOOK_NAME]
        raw = json.dumps(remaining)
        for text in ("keep-stop", "keep-pre", "keep-post", "preserve"): self.assertIn(text, raw)
        self.assertNotIn("antigravity-hook", raw)

    def test_detach_removes_created_empty_file_and_does_not_require_app(self):
        manage.antigravity_attach(self.app)
        shutil.rmtree(self.app)
        self.assertTrue(manage.antigravity_detach()["ok"])
        self.assertFalse(self.hooks.exists())
        self.assertFalse((self.base / "antigravity_installation.json").exists())

    def test_detach_preserves_preexisting_empty_file(self):
        self.write({})
        manage.antigravity_attach(self.app)
        self.assertTrue(manage.antigravity_detach()["ok"])
        self.assertEqual(self.read(), {})

    def test_unsafe_or_invalid_target_is_unchanged(self):
        for raw, mode in ((b'{broken', 0o600), (b'[]', 0o600), (b'{"duplicate":{},"duplicate":{}}', 0o600), (b'{}', 0o666)):
            with self.subTest(raw=raw, mode=mode):
                self.hooks.write_bytes(raw); self.hooks.chmod(mode)
                self.assertFalse(manage.antigravity_attach(self.app)["ok"])
                self.assertEqual(self.hooks.read_bytes(), raw)
        self.hooks.unlink()
        foreign = self.root / "foreign.json"; foreign.write_text("{}"); foreign.chmod(0o600)
        self.hooks.symlink_to(foreign)
        self.assertFalse(manage.antigravity_attach(self.app)["ok"])
        self.assertEqual(foreign.read_text(), "{}")

    def test_symlink_parent_and_unsafe_parent_are_rejected(self):
        self.hooks.parent.rename(self.hooks.parent.with_name("real-config"))
        self.hooks.parent.symlink_to(self.hooks.parent.with_name("real-config"), target_is_directory=True)
        self.assertFalse(manage.antigravity_attach(self.app)["ok"])
        self.hooks.parent.unlink()
        self.hooks.parent.mkdir(); self.hooks.parent.chmod(0o777)
        self.assertFalse(manage.antigravity_attach(self.app)["ok"])

    def test_manifest_conflict_cannot_redirect_detach(self):
        manage.antigravity_attach(self.app)
        p = self.base / "antigravity_installation.json"
        data = json.loads(p.read_text()); data["hook_target"] = str(self.root / "other.json")
        p.write_text(json.dumps(data)); p.chmod(0o600)
        before = self.hooks.read_bytes()
        self.assertEqual(manage.antigravity_detach()["reason"], "adapter_conflict")
        self.assertEqual(self.hooks.read_bytes(), before)

    def test_failed_manifest_write_rolls_back_hook_bytes(self):
        self.write({"third-party": {"enabled": False}})
        before = self.hooks.read_bytes()
        real_atomic = manage.atomic
        def fail(path, text, mode=0o600):
            if Path(path).name == "antigravity_installation.json": raise OSError("synthetic failure")
            return real_atomic(path, text, mode)
        with mock.patch.object(manage, "atomic", side_effect=fail):
            self.assertFalse(manage.antigravity_attach(self.app)["ok"])
        self.assertEqual(self.hooks.read_bytes(), before)

    def test_concurrent_hook_edit_is_preserved_instead_of_overwritten(self):
        self.write({"third-party": {"enabled": False}})
        real_atomic = manage.atomic
        changed = {"third-party": {"enabled": False}, "added-concurrently": {"Stop": []}}
        def edit_during_backup(path, text, mode=0o600):
            result = real_atomic(path, text, mode)
            if Path(path).parent.name.startswith("antigravity-"):
                self.write(changed)
            return result
        with mock.patch.object(manage, "atomic", side_effect=edit_during_backup):
            self.assertEqual(manage.antigravity_attach(self.app)["reason"], "adapter_conflict")
        self.assertEqual(self.read(), changed)

    def test_busy_configuration_lock_returns_without_waiting(self):
        lock = self.hooks.parent / ".bark-native-hooks.lock"
        with lock.open("w") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(manage.antigravity_attach(self.app)["reason"], "adapter_busy")
        self.assertFalse(self.hooks.exists())

    def test_status_and_cli_never_echo_hook_commands_or_paths(self):
        self.write({"third-party": {"Stop": [{"command": "SYNTHETIC_PRIVATE_COMMAND"}]}})
        manage.antigravity_attach(self.app)
        result = manage.antigravity_status()
        self.assertEqual(result["version"], "2.32.2")
        self.assertNotIn("SYNTHETIC_PRIVATE_COMMAND", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        out = io.StringIO()
        with mock.patch("sys.argv", ["manage.py", "antigravity-status"]), contextlib.redirect_stdout(out):
            manage.main()
        self.assertEqual(json.loads(out.getvalue()), result)

    def test_full_uninstall_stops_before_deleting_data_when_detach_conflicts(self):
        (self.base / "antigravity_installation.json").write_text("{}")
        with mock.patch.object(manage, "antigravity_detach", return_value={"ok": False}), \
                mock.patch.object(manage, "load_manifest", side_effect=AssertionError("must stop first")), \
                contextlib.redirect_stdout(io.StringIO()):
            manage.uninstall()


if __name__ == "__main__": unittest.main()
