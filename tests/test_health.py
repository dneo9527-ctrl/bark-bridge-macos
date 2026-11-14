import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock

import test_codex_bark as fixtures
manage = fixtures.module("health_manage", fixtures.SRC / "manage.py")


class ManageHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "support"
        self.base.mkdir(mode=0o700)
        os.chmod(self.base, 0o700)

        for name in ("bark.py", "manage.py"):
            p = self.base / name
            p.write_text("#!/usr/bin/python3\n")
            os.chmod(p, 0o700)

        self.skill_dir = self.base / "skill"
        self.skill_dir.mkdir(mode=0o700)
        (self.skill_dir / "SKILL.md").write_text("# codex-bark-wait\n")

        self.home1 = self.root / "codex_home_1"
        (self.home1 / "skills").mkdir(parents=True)
        (self.home1 / "skills/codex-bark-wait").symlink_to(self.skill_dir)

        self.manifest = self.base / "installation.json"
        self.manifest.write_text(json.dumps({"homes": {str(self.home1): {"attached": True}}, "backups": []}))
        os.chmod(self.manifest, 0o600)

        self.app_path = self.root / "Bark.app"
        self.app_path.mkdir()
        self.login_file = self.root / "local.codex.bark.menu.plist"
        self._write_valid_login()

        self.patchers = [
            mock.patch.object(manage, "BASE", self.base),
            mock.patch.object(manage, "MANIFEST", self.manifest),
            mock.patch.object(manage, "LOGIN_FILE", self.login_file),
            mock.patch.object(manage, "APP_PATH", self.app_path),
            mock.patch.object(manage, "hooks_list", side_effect=self._healthy_hooks),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def _write_valid_login(self):
        data = {
            "Label": "local.codex.bark.menu",
            "ProgramArguments": ["/usr/bin/open", "-g", str(self.app_path), "--args", "--background"],
            "RunAtLoad": True,
        }
        self.login_file.write_bytes(plistlib.dumps(data))

    def _healthy_hooks(self, home, timeout=15):
        return {
            "hooks": [
                {"eventName": "preToolUse", "command": manage.CMD, "enabled": True, "trustStatus": "trusted", "matcher": manage.QUESTION_MATCHER},
                {"eventName": "permissionRequest", "command": manage.CMD, "enabled": True, "trustStatus": "trusted"},
                {"eventName": "sessionStart", "command": manage.CMD, "enabled": True, "trustStatus": "trusted"},
                {"eventName": "stop", "command": manage.CMD, "enabled": True, "trustStatus": "trusted"},
            ]
        }

    def test_all_pass(self):
        report = manage.health_report()
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"], "ready")
        self.assertTrue(all(c["status"] == "pass" for c in report["checks"]))

    def test_login_off_is_still_ready(self):
        self.login_file.unlink()
        report = manage.health_report()
        self.assertEqual(report["summary"], "ready")
        login_check = next(c for c in report["checks"] if c["id"] == "login_item")
        self.assertEqual(login_check["status"], "off")
        self.assertEqual(login_check["code"], "login_off")

    def test_untrusted_missing_duplicate_and_disabled_hooks(self):
        # 1) 未信任
        def untrusted(home, timeout=15):
            data = self._healthy_hooks(home)
            data["hooks"][1]["trustStatus"] = "untrusted"
            return data

        with mock.patch.object(manage, "hooks_list", side_effect=untrusted):
            r1 = manage.health_report()
            self.assertEqual(r1["summary"], "attention")
            c1 = next(c for c in r1["checks"] if c["id"] == "home_1_hooks")
            self.assertEqual(c1["code"], "hook_untrusted")

        # 2) 缺失 stop
        def missing(home, timeout=15):
            data = self._healthy_hooks(home)
            data["hooks"] = [h for h in data["hooks"] if h["eventName"] != "stop"]
            return data

        with mock.patch.object(manage, "hooks_list", side_effect=missing):
            r2 = manage.health_report()
            c2 = next(c for c in r2["checks"] if c["id"] == "home_1_hooks")
            self.assertEqual(c2["code"], "missing_hook")

        # 3) 重复条目
        def duplicate(home, timeout=15):
            data = self._healthy_hooks(home)
            data["hooks"].append(dict(data["hooks"][0]))
            return data

        with mock.patch.object(manage, "hooks_list", side_effect=duplicate):
            r3 = manage.health_report()
            c3 = next(c for c in r3["checks"] if c["id"] == "home_1_hooks")
            self.assertEqual(c3["code"], "duplicate_hook")

    def test_api_exception_never_leaks_secret(self):
        def boom(home, timeout=15):
            raise RuntimeError(f"fatal error token=FAKE_SECRET_999 path={home}")

        with mock.patch.object(manage, "hooks_list", side_effect=boom):
            report = manage.health_report()
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertEqual(report["summary"], "unknown")
            self.assertNotIn("FAKE_SECRET", serialized)
            self.assertNotIn(str(self.home1), serialized)
            hook_check = next(c for c in report["checks"] if c["id"] == "home_1_hooks")
            self.assertEqual(hook_check["status"], "unknown")
            self.assertEqual(hook_check["code"], "api_unavailable")

    def test_corrupt_manifest(self):
        self.manifest.write_text("{corrupt json FAKE_SECRET")
        report = manage.health_report()
        self.assertEqual(report["summary"], "attention")
        m_check = next(c for c in report["checks"] if c["id"] == "manifest")
        self.assertEqual(m_check["code"], "invalid_manifest")
        self.assertNotIn("FAKE_SECRET", json.dumps(report, ensure_ascii=False))

    def test_nonexistent_home_skips_cli_and_does_not_create_dir(self):
        missing_home = self.root / "missing_codex_home"
        self.manifest.write_text(json.dumps({"homes": {str(missing_home): {"attached": True}}}))
        mock_cli = mock.MagicMock()
        with mock.patch.object(manage, "hooks_list", mock_cli):
            report = manage.health_report()
            mock_cli.assert_not_called()
            self.assertFalse(missing_home.exists())
            self.assertEqual(report["summary"], "attention")
            h_check = next(c for c in report["checks"] if c["id"] == "home_1")
            self.assertEqual(h_check["code"], "home_missing")

    def test_broken_skill_link(self):
        wrong_dir = self.root / "wrong_skill"
        wrong_dir.mkdir()
        link = self.home1 / "skills/codex-bark-wait"
        link.unlink()
        link.symlink_to(wrong_dir)
        report = manage.health_report()
        self.assertEqual(report["summary"], "attention")
        s_check = next(c for c in report["checks"] if c["id"] == "home_1_skill")
        self.assertEqual(s_check["code"], "invalid_skill_link")

    def test_strictly_readonly_preserves_content_and_mtime(self):
        tracked = [self.manifest, self.login_file, self.base / "bark.py", self.skill_dir / "SKILL.md"]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tracked}
        _ = manage.health_report()
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tracked}
        self.assertEqual(before, after)

class HealthBoundaryTests(ManageHealthTests):
    def test_managed_trust_is_valid_and_unknown_schema_is_not_false_failure(self):
        d=self._healthy_hooks(self.home1)
        for hook in d['hooks']:hook['trustStatus']='managed'
        self.assertEqual(manage._evaluate_hooks_payload(d,manage.CMD)[0],'pass')
        d['hooks'][0]['trustStatus']='FUTURE_VALUE'
        self.assertEqual(manage._evaluate_hooks_payload(d,manage.CMD)[0],'unknown')

    def test_disabled_hook_and_bad_matcher_are_reported(self):
        d=self._healthy_hooks(self.home1);d['hooks'][1]['enabled']=False
        self.assertEqual(manage._evaluate_hooks_payload(d,manage.CMD)[1],'hook_disabled')
        d=self._healthy_hooks(self.home1);d['hooks'][0]['matcher']='.*'
        self.assertEqual(manage._evaluate_hooks_payload(d,manage.CMD)[1],'hook_matcher_mismatch')

    def test_login_must_match_launcher_and_run_at_load(self):
        self.login_file.write_bytes(plistlib.dumps({'Label':'local.codex.bark.menu','ProgramArguments':['/bad/launcher',str(self.app_path)],'RunAtLoad':True}))
        self.assertEqual(manage.health_report()['summary'],'attention')
        self._write_valid_login();d=plistlib.loads(self.login_file.read_bytes());d['RunAtLoad']=False;self.login_file.write_bytes(plistlib.dumps(d))
        self.assertEqual(manage.health_report()['summary'],'attention')

    def test_private_files_are_not_opened(self):
        (self.home1/'auth.json').write_text('FAKE_ACCOUNT_SECRET')
        (self.base/'config.json').write_text('FAKE_PHONE_SECRET')
        original=Path.open;blocked=[]
        def guard(path,*args,**kwargs):
            if path.name in ('auth.json','config.json'):
                blocked.append(path.name);raise AssertionError('private file must not be opened')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'open',guard):report=manage.health_report()
        self.assertEqual(report['summary'],'ready');self.assertEqual(blocked,[])

    def test_relative_home_is_not_used_or_created(self):
        self.manifest.write_text(json.dumps({'homes':{'.':{}}}))
        with mock.patch.object(manage,'hooks_list') as query:
            d=manage.health_report();query.assert_not_called()
        self.assertEqual(d['summary'],'attention')

    def test_global_budget_skips_query_and_remains_unknown(self):
        with mock.patch.object(manage,'hooks_list') as query:
            d=manage.health_report(budget_seconds=1);query.assert_not_called()
        self.assertEqual(d['summary'],'unknown')

if __name__ == "__main__":
    suite=unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ManageHealthTests))
    for name in ['test_managed_trust_is_valid_and_unknown_schema_is_not_false_failure','test_disabled_hook_and_bad_matcher_are_reported','test_login_must_match_launcher_and_run_at_load','test_private_files_are_not_opened','test_relative_home_is_not_used_or_created','test_global_budget_skips_query_and_remains_unknown']:
        suite.addTest(HealthBoundaryTests(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
