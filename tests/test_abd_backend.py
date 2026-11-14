import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
import unittest
from unittest.mock import patch
import test_codex_bark as fixtures


class ABDBackendTests(unittest.TestCase):
    setUp = fixtures.NotifierTests.setUp
    tearDown = fixtures.NotifierTests.tearDown

    def run_cli(self, *args, stdin_text=None):
        return subprocess.run(
            ["/usr/bin/python3", str(self.base / "bark.py"), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def create_dummy_app(self, name="DemoApp", bundle_id="com.example.demo"):
        app_dir = self.base / f"{name}.app/Contents"
        app_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "CFBundleIdentifier": bundle_id,
            "CFBundleDisplayName": name,
            "CFBundleName": name,
        }
        (app_dir / "Info.plist").write_bytes(plistlib.dumps(info))
        return str(self.base / f"{name}.app")

    def test_legacy_rules_and_invalid_or_expired_source_mute_validation(self):
        # 1. 旧版无 source_mute_until 字段的规则文件默认空字典且不写盘
        legacy = {
            "version": 1,
            "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
            "routes": {},
        }
        self.m.atomic_json(self.m.RULES, legacy)
        before = self.m.RULES.read_bytes()
        parsed = self.m.rules()
        self.assertEqual(parsed["source_mute_until"], {})
        self.assertEqual(self.m.RULES.read_bytes(), before)

        # 2. 过期正数时间戳合法保留原值，且按当前时间判定为未静音（不补发、不改写文件）
        expired_ts = time.time() - 600
        self.m.atomic_json(self.m.RULES, {**legacy, "source_mute_until": {"codex": expired_ts}})
        snap = self.m.snapshot("panel")
        self.assertTrue(snap["policy"]["ok"])
        self.assertAlmostEqual(snap["policy"]["source_mute_until"]["codex"], expired_ts, places=2)
        codex_row = next(x for x in snap["sources"] if x["id"] == "codex")
        self.assertAlmostEqual(codex_row["mute_until"], expired_ts, places=2)
        self.assertFalse(self.m.source_muted_now("codex"))

        # 3. 非法结构/布尔/负数/无穷大/非法 ID 拒绝并触发 invalid_rules
        for bad in (
            {"codex": True},
            {"codex": -10},
            {"codex": "900"},
            {"bad id!": 900},
            ["codex"],
        ):
            self.m.atomic_json(self.m.RULES, {**legacy, "source_mute_until": bad})
            with self.assertRaises(ValueError):
                self.m.rules()
            s = self.m.snapshot("panel")
            self.assertFalse(s["policy"]["ok"])
            self.assertEqual(s["errors"]["policy"], "invalid_rules")

        # 4. 仅允许对已存在来源设置静音，且秒数必须合法
        self.m.atomic_json(self.m.RULES, legacy)
        with self.assertRaises(ValueError):
            self.m.set_source_mute("nonexistent-source", 900)
        for bad_sec in (-1, True, "900", 604801):
            with self.assertRaises(ValueError):
                self.m.set_source_mute("codex", bad_sec)

    def test_source_mute_skips_before_claim_preserves_other_sources_and_never_resends(self):
        res = self.m.set_source_mute("codex", 900)
        self.assertTrue(res["ok"])
        self.assertGreater(res["mute_until"], time.time() + 800)

        # Codex 三类自动事件均被 source_muted 拦截，不触碰 claim 或网络
        with patch.object(self.m, "send_target") as send, patch.object(self.m, "claim") as claim:
            for kind in ("permission", "question", "complete"):
                status = self.m.deliver({"kind": kind, "session": "s1", "fingerprint": f"fp-{kind}"})
                self.assertEqual(status, "source_muted")
            send.assert_not_called()
            claim.assert_not_called()

        last_row = self.m.history()[-1]
        self.assertEqual(last_row["status"], "source_muted")
        self.assertEqual(last_row["reason"], "source_muted")
        self.assertEqual(last_row["category"], "skipped")

        # 静音 Codex 期间，Mac 电池提醒与其他来源不受影响
        with patch.object(self.m, "send_target", return_value=(True, 200, "accepted")) as send:
            bat_ev = self.m.battery_event(20, False)
            self.assertEqual(self.m.deliver(bat_ev), "accepted")
            self.assertEqual(send.call_count, 1)

        # 取消静音（seconds=0）后不会补发旧通知，也不改变来源本身的 enabled 状态
        cancel = self.m.set_source_mute("codex", 0)
        self.assertEqual(cancel["mute_until"], 0)
        self.assertNotIn("codex", self.m.rules()["source_mute_until"])
        codex_src = next(x for x in self.m.source_list() if x["id"] == "codex")
        self.assertTrue(codex_src["enabled"])

    def test_permanent_source_disable_takes_priority_and_expiry_does_not_reenable(self):
        self.m.set_source_mute("codex", 900)
        self.m.change_source("codex", enabled=False)

        with patch.object(self.m, "send_target") as send:
            self.assertEqual(self.m.deliver(self.event), "source_disabled")
            send.assert_not_called()

        # 静音过期后，手动永久关闭的来源仍保持 source_disabled
        d = self.m.rules()
        d["source_mute_until"]["codex"] = time.time() - 10
        self.m.atomic_json(self.m.RULES, d)
        with patch.object(self.m, "send_target") as send:
            self.assertEqual(self.m.deliver(self.event), "source_disabled")
            send.assert_not_called()

    def test_manual_test_source_test_and_diagnose_bypass_temporary_mute(self):
        self.m.set_source_mute("codex", 3600)
        self.m.set_source_mute("mac-battery", 3600)
        with patch.object(self.m, "send_target", return_value=(True, 200, "accepted")) as send:
            self.assertEqual(
                self.m.deliver({"kind": "source_test", "source_id": "codex", "session": "t", "fingerprint": "t"}, force=True),
                "accepted",
            )
            self.assertEqual(
                self.m.deliver({"kind": "test", "session": "t", "fingerprint": "t"}, force=True),
                "accepted",
            )
            self.assertTrue(self.m.diagnose("primary")["ok"])
            self.assertEqual(send.call_count, 3)

            # 自动电池事件遵守 mac-battery 临时静音
            self.assertEqual(
                self.m.deliver({"kind": "battery", "source_id": "mac-battery", "session": "b", "fingerprint": "b", "battery_percent": 15, "battery_level": 20}),
                "source_muted",
            )

    def test_source_remove_cleans_up_route_and_mute_entries(self):
        app_path = self.create_dummy_app("CleanMe", "com.example.cleanme")
        added = self.m.add_sources([app_path])
        sid = added[0]
        self.m.set_route(sid, {"mode": "selected", "device_ids": ["primary"]})
        self.m.set_source_mute(sid, 900)
        self.assertIn(sid, self.m.rules()["routes"])
        self.assertIn(sid, self.m.rules()["source_mute_until"])

        self.m.change_source(sid, remove=True)
        after = self.m.rules()
        self.assertNotIn(sid, after["routes"])
        self.assertNotIn(sid, after["source_mute_until"])

    def test_toggle_flips_fresh_state_for_master_source_and_device(self):
        # 1. set-enabled toggle（含定时暂停已到期与未到期场景）
        p1 = self.run_cli("set-enabled", "toggle", "--with-snapshot", "--view", "menu")
        d1 = json.loads(p1.stdout)
        self.assertTrue(d1["ok"])
        self.assertFalse(d1["enabled"])
        self.assertFalse(d1["snapshot"]["status"]["enabled"])

        p2 = self.run_cli("set-enabled", "toggle", "--with-snapshot", "--view", "menu")
        d2 = json.loads(p2.stdout)
        self.assertTrue(d2["enabled"])
        self.assertTrue(d2["snapshot"]["status"]["enabled"])

        # 2. source-toggle --source-enabled toggle
        p3 = self.run_cli("source-toggle", "--source-id", "codex", "--source-enabled", "toggle", "--with-snapshot")
        d3 = json.loads(p3.stdout)
        self.assertTrue(d3["ok"])
        codex_item = next(x for x in d3["snapshot"]["sources"] if x["id"] == "codex")
        self.assertFalse(codex_item["enabled"])

        p4 = self.run_cli("source-toggle", "--source-id", "codex", "--source-enabled", "toggle", "--with-snapshot")
        d4 = json.loads(p4.stdout)
        codex_item_2 = next(x for x in d4["snapshot"]["sources"] if x["id"] == "codex")
        self.assertTrue(codex_item_2["enabled"])

        # 3. device-toggle --device-enabled toggle
        p5 = self.run_cli("device-toggle", "--device-id", "primary", "--device-enabled", "toggle", "--with-snapshot")
        d5 = json.loads(p5.stdout)
        self.assertTrue(d5["ok"])
        self.assertFalse(d5["snapshot"]["devices"][0]["enabled"])

        p6 = self.run_cli("device-toggle", "--device-id", "primary", "--device-enabled", "toggle", "--with-snapshot")
        d6 = json.loads(p6.stdout)
        self.assertTrue(d6["snapshot"]["devices"][0]["enabled"])

    def test_with_snapshot_on_mutating_actions_and_never_leaks_secrets(self):
        # 默认不传 --with-snapshot 时保持原返回结构兼容
        plain = json.loads(self.run_cli("pause-for", "--seconds", "900").stdout)
        self.assertTrue(plain["ok"])
        self.assertNotIn("snapshot", plain)

        # 传入 --with-snapshot 时返回原字段 + 对应 view 的安全快照，且绝不含密钥/URL
        muted = json.loads(
            self.run_cli("source-mute", "--source-id", "codex", "--seconds", "900", "--with-snapshot", "--view", "panel").stdout
        )
        self.assertTrue(muted["ok"])
        self.assertGreater(muted["mute_until"], time.time())
        self.assertIn("snapshot", muted)
        self.assertIn("codex", muted["snapshot"]["policy"]["source_mute_until"])
        raw_json = json.dumps(muted, ensure_ascii=False)
        self.assertNotIn(self.secret, raw_json)
        self.assertNotIn("device_key", raw_json)
        self.assertNotIn("https://", raw_json)

        # 失败动作不得伪造 ok:true 或返回快照
        bad = json.loads(
            self.run_cli("source-mute", "--source-id", "unknown-id", "--seconds", "900", "--with-snapshot").stdout
        )
        self.assertFalse(bad["ok"])
        self.assertNotIn("snapshot", bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
