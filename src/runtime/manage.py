#!/usr/bin/python3
"""Attach/uninstall this user's shared notifier. Never accesses account auth stores."""
import argparse
import base64
import ctypes
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent


def python_executable():
    bundled = BASE / "python/bin/python3"
    return str(bundled) if bundled.is_file() else "/usr/bin/python3"

MANIFEST = BASE / "installation.json"
def discover_codex_cli():
    override = os.environ.get("CODEX_CLI_BIN")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("CODEX_CLI_BIN must name an executable absolute path")
        return path
    found = shutil.which("codex")
    if found: return Path(found)
    for app in [Path("/Applications/Codex.app"), Path("/Applications/ChatGPT.app"), Path.home()/"Applications/Codex.app", Path.home()/"Applications/ChatGPT.app"]:
        for suffix in ["Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", "Contents/Resources/codex"]:
            candidate = app / suffix
            if candidate.is_file() and os.access(candidate, os.X_OK): return candidate
    return Path("/nonexistent/codex")

EXE = discover_codex_cli()
START = "# BEGIN CODEX BARK SHARED HOOK TRUST"
END = "# END CODEX BARK SHARED HOOK TRUST"
CMD = shlex.quote(python_executable()) + " " + shlex.quote(str(BASE / "bark.py")) + " hook"
QUESTION_MATCHER = r"(^|__|\.)(request_user_input|request_user_input_async|ask_user|ask_user_question)$"


def is_our_command(command):
    if not isinstance(command, str): return False
    try:
        tokens = shlex.split(command)
        return (len(tokens) == 3 and tokens[0] in {"/usr/bin/python3", str(BASE / "python/bin/python3")}
                and Path(tokens[1]).is_absolute()
                and Path(tokens[1]).resolve() == (BASE / "bark.py").resolve()
                and tokens[2] == "hook")
    except (ValueError, OSError):
        return False


def atomic(path, text, mode=0o600):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".codex-bark-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def strip_block(text):
    return re.sub(r"(?m)^" + re.escape(START) + r"\n.*?^" + re.escape(END) + r"\n?", "", text, flags=re.S)


def backup(path, directory, label):
    if path.exists():
        shutil.copy2(path, directory / label)
        os.chmod(directory / label, 0o600)


import plistlib
import stat

LOGIN_FILE = Path.home() / "Library/LaunchAgents/local.codex.bark.menu.plist"
APP_PATH = Path("/Applications/Bark.app")
REQUIRED_HOOK_EVENTS = ("preToolUse", "permissionRequest", "sessionStart", "stop")
MAX_HEALTH_HOMES = 16
APP_IDENTIFIER = "local.codex.bark.menu"
UPDATE_PUBLIC_KEY = "z1ADse6t11ZmEymVZflP7r9fk2z3vb8rUOTNN+fZaNo="


def _runtime_base(base=None):
    return Path(BASE if base is None else base).resolve()


def _public_json(path):
    """Read only our bounded public installation/location/update metadata."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("app_location_invalid")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_size > 262144 or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError("app_location_invalid")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError("app_location_invalid")
    return data


def _validate_app(app, base=None):
    """Validate one selected bundle, never search for another installation."""
    app = Path(app)
    if not app.is_absolute() or app.is_symlink() or not app.is_dir():
        raise RuntimeError("app_location_unavailable")
    app = app.resolve()
    if (app.suffix.lower() != ".app" or any(part.lower().startswith(".bark-update-") for part in app.parts)
            or any(part.lower() in (".trash", ".trashes") for part in app.parts)):
        raise RuntimeError("app_location_invalid")
    if app.is_relative_to(_runtime_base(base) / "updates"):
        raise RuntimeError("app_location_invalid")

    def resource(relative, executable=False):
        path = app / relative
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(app)
                or (executable and not os.access(path, os.X_OK))):
            raise RuntimeError("app_location_invalid")
        return path

    for prefix, identifier, executable in [
        ("Contents", APP_IDENTIFIER, "CodexBarkMenu"),
        ("Contents/Helpers/Codex Bark Panel.app/Contents", "local.codex.bark.panel", "CodexBarkPanel"),
    ]:
        info = plistlib.loads(resource(prefix + "/Info.plist").read_bytes())
        if (info.get("CFBundleIdentifier") != identifier or info.get("CFBundleExecutable") != executable
                or info.get("CFBundlePackageType") != "APPL"):
            raise RuntimeError("app_location_invalid")
        resource(prefix + "/MacOS/" + executable, executable=True)
    resource("Contents/Helpers/BarkUpdateVerifier", executable=True)
    release = json.loads(resource("Contents/Resources/release.json").read_text())
    if not isinstance(release, dict) or release.get("update_protocol") != 1:
        raise RuntimeError("app_location_invalid")
    if resource("Contents/Resources/update-public-key.txt").read_text().strip() != UPDATE_PUBLIC_KEY:
        raise RuntimeError("app_location_invalid")
    return app


def _core_foundation():
    if sys.platform != "darwin":
        raise RuntimeError("app_bookmark_unavailable")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = ctypes.c_void_p
    signatures = {
        "CFURLCreateFromFileSystemRepresentation": ([pointer, pointer, ctypes.c_long, ctypes.c_ubyte], pointer),
        "CFURLCreateBookmarkData": ([pointer, pointer, ctypes.c_ulong, pointer, pointer, pointer], pointer),
        "CFDataCreate": ([pointer, pointer, ctypes.c_long], pointer),
        "CFDataGetLength": ([pointer], ctypes.c_long),
        "CFDataGetBytePtr": ([pointer], pointer),
        "CFURLCreateByResolvingBookmarkData":
            ([pointer, pointer, ctypes.c_ulong, pointer, pointer, pointer, pointer], pointer),
        "CFURLGetFileSystemRepresentation": ([pointer, ctypes.c_ubyte, pointer, ctypes.c_long], ctypes.c_ubyte),
        "CFRelease": ([pointer], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(cf, name)
        function.argtypes, function.restype = arguments, result
    return cf


def _create_app_bookmark(app):
    cf = _core_foundation()
    encoded = os.fsencode(app)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, encoded, len(encoded), True)
    if not url:
        raise RuntimeError("app_bookmark_unavailable")
    bookmark = None
    try:
        # Normal bookmarks need no sandbox entitlement or additional permission.
        bookmark = cf.CFURLCreateBookmarkData(None, url, 0, None, None, None)
        if not bookmark:
            raise RuntimeError("app_bookmark_unavailable")
        return base64.b64encode(ctypes.string_at(cf.CFDataGetBytePtr(bookmark),
                                                cf.CFDataGetLength(bookmark))).decode("ascii")
    finally:
        if bookmark:
            cf.CFRelease(bookmark)
        cf.CFRelease(url)


def _resolve_app_bookmark(record):
    try:
        encoded = record["bookmark"]
        if not isinstance(encoded, str) or len(encoded) > 131072:
            raise ValueError()
        value = base64.b64decode(encoded, validate=True)
        if not value:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("app_location_invalid") from None
    cf = _core_foundation()
    data = cf.CFDataCreate(None, value, len(value))
    url = None
    try:
        stale = ctypes.c_ubyte()
        # kCFURLBookmarkResolutionWithoutUI | WithoutMounting: never prompt or mount.
        url = cf.CFURLCreateByResolvingBookmarkData(None, data, (1 << 8) | (1 << 9), None, None,
                                                   ctypes.byref(stale), None)
        if not url:
            raise RuntimeError("app_location_unavailable")
        buffer = ctypes.create_string_buffer(32768)
        if not cf.CFURLGetFileSystemRepresentation(url, True, buffer, len(buffer)):
            raise RuntimeError("app_location_unavailable")
        return Path(os.fsdecode(buffer.value)), bool(stale.value)
    finally:
        if url:
            cf.CFRelease(url)
        if data:
            cf.CFRelease(data)


def _location_record(base=None):
    path = _runtime_base(base) / "app-location.json"
    if not path.exists() and not path.is_symlink():
        return None
    record = _public_json(path)
    if (record.get("schema") != 1 or record.get("bundle_id") != APP_IDENTIFIER
            or not isinstance(record.get("path"), str) or not Path(record["path"]).is_absolute()):
        raise RuntimeError("app_location_invalid")
    return record


def _pending_app(base=None, record=None):
    path = _runtime_base(base) / "updates/transaction.json"
    if not path.exists() and not path.is_symlink():
        return None
    transaction = _public_json(path)
    if transaction.get("phase") not in ("applying", "awaiting_start"):
        return None
    target = transaction.get("app_path")
    # Protocol-1 transactions from older versions did not store the destination.
    if target is None:
        manifest = _public_json(_runtime_base(base) / "installation.json")
        menu_app = manifest.get("menu_app")
        present = transaction.get("runtime_present")
        if (not isinstance(menu_app, dict) or not isinstance(present, list)
                or len(set(present)) != len(present)
                or any(name not in ("bark.py", "manage.py", "claude_bark.py", "bark-run", "updater.py")
                       for name in present)):
            raise RuntimeError("app_location_invalid")
        target = menu_app.get("path")
    if (not isinstance(target, str) or not Path(target).is_absolute()
            or not re.fullmatch(r"backup-[0-9a-f]{32}", transaction.get("id", ""))):
        raise RuntimeError("app_location_invalid")
    app = _validate_app(target, base)
    if record is not None and Path(record["path"]).resolve() != app:
        bookmarked, _ = _resolve_app_bookmark(record)
        # RENAME_SWAP makes the old app's bookmark follow the generated sibling.
        sibling = app.with_name(".Bark-update-" + transaction["id"] + ".app")
        if bookmarked.resolve() not in (app, sibling):
            raise RuntimeError("app_location_invalid")
    return app


def resolve_app(base=None):
    """Resolve the selected installation without touching preferences or secrets."""
    record = _location_record(base)
    pending = _pending_app(base, record)
    if pending is not None:
        return pending
    if record is None:
        return _validate_app(APP_PATH, base)
    app, _ = _resolve_app_bookmark(record)
    return _validate_app(app, base)


def login_arguments(base=None):
    root = _runtime_base(base)
    bundled = root / "python/bin/python3"
    python = str(bundled) if bundled.is_file() else "/usr/bin/python3"
    return [python, "-I", "-B", str(root / "manage.py"), "launch-app"]


def _write_login(on, base=None, only_existing=False):
    existing = None
    if LOGIN_FILE.is_symlink():
        raise RuntimeError("invalid_login_item")
    if LOGIN_FILE.exists():
        existing = plistlib.loads(LOGIN_FILE.read_bytes())
        if not isinstance(existing, dict) or existing.get("Label") != APP_IDENTIFIER:
            raise RuntimeError("invalid_login_item")
    if not on:
        if existing is not None:
            LOGIN_FILE.unlink()
        return
    if only_existing and (existing is None or existing.get("RunAtLoad") is not True):
        return
    wanted = dict(existing or {})
    wanted.update(Label=APP_IDENTIFIER, ProgramArguments=login_arguments(base), RunAtLoad=True)
    wanted.setdefault("KeepAlive", False)
    wanted.setdefault("ProcessType", "Interactive")
    if existing != wanted:
        atomic(LOGIN_FILE, plistlib.dumps(wanted).decode("utf-8"))


def register_app(app, base=None, migrate_login=True):
    """Remember an explicitly launched installation, preserving every user setting."""
    root = _runtime_base(base)
    if not root.is_dir() or root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("app_location_invalid")
    app = _validate_app(app, root)
    previous = _location_record(root)
    pending = _pending_app(root, previous)
    if pending is not None and pending != app:
        raise RuntimeError("app_location_busy")
    info = app.stat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    unchanged = (previous is not None and previous.get("path") == str(app)
                 and previous.get("identity") == identity)
    if unchanged:
        try:
            resolved, stale = _resolve_app_bookmark(previous)
            unchanged = resolved.resolve() == app and not stale
        except Exception:
            unchanged = False
    manifest_path = root / "installation.json"
    manifest = _public_json(manifest_path) if manifest_path.exists() or manifest_path.is_symlink() else None
    if not unchanged:
        record = {"schema": 1, "bundle_id": APP_IDENTIFIER, "path": str(app), "identity": identity,
                  "bookmark": _create_app_bookmark(app)}
        atomic(root / "app-location.json", json.dumps(record, sort_keys=True) + "\n")
    if manifest is not None and isinstance(manifest.get("menu_app"), dict):
        if manifest["menu_app"].get("path") != str(app):
            manifest["menu_app"]["path"] = str(app)
            atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    # A version-49 worker can still roll back to a manager without launch-app.
    # Keep its old login entry until the replacement has committed; the native
    # startup handoff registers again once the transaction is terminal.
    if migrate_login and pending is None:
        _write_login(True, root, only_existing=True)
    return app


def set_login(on, base=None):
    if on:
        register_app(resolve_app(base), base, migrate_login=False)
    _write_login(bool(on), base)


def launch_app(base=None):
    app = resolve_app(base)
    register_app(app, base)
    subprocess.run(["/usr/bin/open", "-g", str(app), "--args", "--background"], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return app


def _app_for_status():
    # Historical installations have no location record; retain their read-only
    # diagnostics behavior until their first native launch registers the app.
    if _location_record() is None and _pending_app() is None:
        return APP_PATH
    return resolve_app()


def hooks_list(home, timeout=15):
    exe = str(EXE) if EXE.exists() else shutil.which("codex")
    if not exe:
        raise RuntimeError("Codex CLI not found")
    env = dict(os.environ, CODEX_HOME=str(home))
    p = subprocess.Popen([exe, "app-server", "--stdio"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, env=env)
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)

    def call(i, method, params):
        p.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            if sel.select(min(0.5, max(0.05, deadline - time.monotonic()))):
                line = p.stdout.readline()
                if not line:
                    break
                r = json.loads(line)
                if r.get("id") == i:
                    if "error" in r:
                        raise RuntimeError("Codex local hooks API returned an error")
                    return r["result"]
        raise TimeoutError("Codex local hooks API timed out")

    try:
        call(1, "initialize", {"clientInfo": {"name": "codex_bark_installer", "version": "1.0"},
                               "capabilities": {"experimentalApi": True}})
        p.stdin.write('{"method":"initialized"}\n')
        p.stdin.flush()
        return call(2, "hooks/list", {"cwds": [str(home)]})["data"][0]
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        sel.close()
        p.stdin.close()
        p.stdout.close()


def _item(cid, title, status, code, detail):
    return {"id": cid, "title": title, "status": status, "code": code, "detail": detail}


def _safe_file_ok(path, uid, mask=0o022):
    try:
        if path.is_symlink() or not path.exists():
            return False
        st = path.stat()
        return st.st_uid == uid and (stat.S_IMODE(st.st_mode) & mask) == 0
    except OSError:
        return False


def _query_hooks(fn, home, timeout):
    return fn(home, timeout=timeout)


def _evaluate_hooks_payload(payload, expected_cmd):
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), list):
        return ("unknown", "api_unknown", "本地 Hook 接口返回格式未知，无法确认状态。")
    hooks = [h for h in payload["hooks"] if isinstance(h, dict)]
    for event_name in REQUIRED_HOOK_EVENTS:
        matched = [h for h in hooks if h.get("eventName") == event_name and h.get("command") == expected_cmd]
        if not matched:
            return ("warning", "missing_hook", "部分 Bark Hook 缺失，请重新运行接入配置。")
        if len(matched) > 1:
            return ("warning", "duplicate_hook", "检测到重复的 Bark Hook 条目，建议清理后重新接入。")
        hook = matched[0]
        if not isinstance(hook.get("enabled"), bool) or hook.get("trustStatus") not in ("trusted", "managed", "untrusted", "modified"):
            return ("unknown", "api_unknown", "本地 Hook 接口返回格式未知，无法确认状态。")
        if not hook["enabled"]:
            return ("warning", "hook_disabled", "部分 Bark Hook 已被停用。")
        if hook["trustStatus"] in ("untrusted", "modified"):
            return ("warning", "hook_untrusted", "部分 Bark Hook 尚未获得信任授权。")
        if event_name == "preToolUse" and "matcher" in hook and hook["matcher"] != QUESTION_MATCHER:
            return ("warning", "hook_matcher_mismatch", "提问工具 Hook 匹配规则异常，请重新运行接入配置。")
    return ("pass", "ok", "4 条 Hook 均已启用且受信任。")


def _check_login_item(login_path, app_path):
    try:
        if not login_path.is_symlink() and not login_path.exists():
            return _item("login_item", "登录启动项", "off", "login_off", "未开启登录时启动（按需手动启动）。")
        if login_path.is_symlink() or not login_path.is_file():
            return _item("login_item", "登录启动项", "warning", "invalid_login_item", "登录启动项文件异常，可在设置中重新切换。")
        plist = plistlib.loads(login_path.read_bytes())
        args = plist.get("ProgramArguments") if isinstance(plist, dict) else None
        allowed = [login_arguments()]
        if _location_record() is None:
            allowed.append(["/usr/bin/open", "-g", str(app_path), "--args", "--background"])
        if (
            not isinstance(plist, dict)
            or plist.get("Label") != "local.codex.bark.menu"
            or not isinstance(args, list)
            or args not in allowed
            or plist.get("RunAtLoad") is not True
            or app_path is None or not app_path.is_dir()
        ):
            return _item("login_item", "登录启动项", "warning", "invalid_login_item", "登录启动项指向异常，可在设置中重新切换。")
        return _item("login_item", "登录启动项", "pass", "ok", "登录启动项配置正常。")
    except Exception:
        return _item("login_item", "登录启动项", "warning", "invalid_login_item", "登录启动项无法解析，可在设置中重新切换。")


def health_report(budget_seconds=20.0, per_home_timeout=4.0):
    started = time.monotonic()
    deadline = started + max(1.0, float(budget_seconds))
    uid = os.getuid()
    checks = []

    # 1. 核心脚本与目录权限
    bark_py = BASE / "bark.py"
    manage_py = BASE / "manage.py"
    if not (BASE.exists() and bark_py.is_file() and manage_py.is_file()):
        checks.append(_item("runtime_files", "核心脚本与目录权限", "warning", "missing_runtime", "核心脚本缺失，请重新安装组件。"))
    elif not (_safe_file_ok(BASE, uid, 0o077) and _safe_file_ok(bark_py, uid, 0o022) and _safe_file_ok(manage_py, uid, 0o022)):
        checks.append(_item("runtime_files", "核心脚本与目录权限", "warning", "unsafe_permissions", "目录或脚本权限不安全，请修复权限。"))
    else:
        checks.append(_item("runtime_files", "核心脚本与目录权限", "pass", "ok", "核心脚本与数据目录权限正常。"))

    # 2. 共享 Skill 目录
    skill_dir = BASE / "skill"
    skill_md = skill_dir / "SKILL.md"
    try:
        skill_ready = (
            skill_dir.is_dir()
            and not skill_dir.is_symlink()
            and skill_md.is_file()
            and not skill_md.is_symlink()
            and skill_md.stat().st_size > 0
        )
    except OSError:
        skill_ready = False
    checks.append(
        _item("shared_skill", "共享通知技能", "pass", "ok", "共享技能文件已就绪。")
        if skill_ready
        else _item("shared_skill", "共享通知技能", "warning", "missing_skill", "共享技能文件缺失或异常。")
    )

    # 3. 安装清单与登记的 Codex 环境（最多 16 个，严格只读）
    homes = None
    try:
        if not MANIFEST.exists() and not MANIFEST.is_symlink():
            checks.append(_item("manifest", "安装清单", "warning", "manifest_missing", "尚未登记 Codex 环境，请运行接入配置。"))
        elif MANIFEST.is_symlink() or not MANIFEST.is_file():
            checks.append(_item("manifest", "安装清单", "warning", "invalid_manifest", "安装清单文件异常，请重新运行接入配置。"))
        else:
            raw = json.loads(MANIFEST.read_text())
            if not isinstance(raw, dict) or not isinstance(raw.get("homes"), dict) or not raw["homes"]:
                checks.append(_item("manifest", "安装清单", "warning", "invalid_manifest", "安装清单格式异常或未包含有效环境。"))
            else:
                homes = list(raw["homes"].keys())[:MAX_HEALTH_HOMES]
                if len(raw["homes"]) > MAX_HEALTH_HOMES:
                    checks.append(_item("home_limit", "登记环境数量", "unknown", "home_limit", "登记环境较多，本次仅检查前 16 个。"))
                checks.append(_item("manifest", "安装清单", "pass", "ok", "安装清单已就绪。"))
    except Exception:
        checks.append(_item("manifest", "安装清单", "warning", "invalid_manifest", "安装清单无法解析，请重新运行接入配置。"))

    if homes:
        target_skill_real = None
        try:
            target_skill_real = skill_dir.resolve(strict=True) if skill_ready else None
        except OSError:
            target_skill_real = None

        for idx, home_str in enumerate(homes, start=1):
            label = f"Codex 环境 #{idx}"
            try:
                home_path = Path(home_str)
                home_exists = isinstance(home_str, str) and home_path.is_absolute() and not home_path.is_symlink() and home_path.is_dir() and home_path.stat().st_uid == uid
            except Exception:
                home_exists = False

            if not home_exists:
                checks.append(_item(f"home_{idx}", label, "warning", "home_missing", "登记的 Codex 目录不存在，已跳过 CLI 检查。"))
                continue

            # 校验该环境下的 codex-bark-wait 技能链接
            link_path = home_path / "skills/codex-bark-wait"
            try:
                link_ok = (
                    target_skill_real is not None
                    and link_path.exists()
                    and link_path.resolve(strict=True) == target_skill_real
                    and (link_path / "SKILL.md").is_file()
                )
            except OSError:
                link_ok = False
            checks.append(
                _item(f"home_{idx}_skill", f"{label} · 技能链接", "pass", "ok", "通知技能链接正常。")
                if link_ok
                else _item(f"home_{idx}_skill", f"{label} · 技能链接", "warning", "invalid_skill_link", "通知技能链接缺失或指向错误。")
            )

            # 校验该环境下的 4 条 Hook（遵守全局预算）
            remaining = deadline - time.monotonic()
            if remaining <= 4.5:
                checks.append(_item(f"home_{idx}_hooks", f"{label} · Hook 状态", "unknown", "check_timeout", "健康检查总耗时达到上限，剩余环境暂未查询。"))
                continue

            try:
                payload = _query_hooks(hooks_list, home_path, timeout=min(float(per_home_timeout), 3.0, max(0.5, (remaining - 3.5) / 2)))
                h_status, h_code, h_detail = _evaluate_hooks_payload(payload, CMD)
            except (TimeoutError, subprocess.TimeoutExpired):
                h_status, h_code, h_detail = ("unknown", "api_timeout", "本地 Hook 接口响应超时，稍后可重试。")
            except Exception:
                h_status, h_code, h_detail = ("unknown", "api_unavailable", "无法通过本地接口查询 Hook 状态。")
            checks.append(_item(f"home_{idx}_hooks", f"{label} · Hook 状态", h_status, h_code, h_detail))

    # 4. 登录启动项（关闭属于正常偏好，status="off" 不触发全局失败）
    try:
        installed_app = _app_for_status()
    except Exception:
        installed_app = None
    checks.append(_check_login_item(LOGIN_FILE, installed_app))

    statuses = {c["status"] for c in checks}
    if "warning" in statuses:
        summary = "attention"
    elif "unknown" in statuses:
        summary = "unknown"
    else:
        summary = "ready"

    return {
        "ok": True,
        "summary": summary,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checks": checks,
    }


def support_report(snapshot_provider=None, health_provider=None):
    """On-demand allowlisted diagnostics; never export paths, names or content."""
    if snapshot_provider is None:
        def snapshot_provider():
            p = subprocess.run([python_executable(), str(BASE / "bark.py"), "snapshot", "--view", "history"],
                               capture_output=True, timeout=5)
            return json.loads(p.stdout) if p.returncode == 0 else {}
    try: snap = snapshot_provider()
    except Exception: snap = {}
    if not isinstance(snap, dict): snap = {}
    try: health = (health_provider or health_report)()
    except Exception: health = {}
    if not isinstance(health, dict): health = {}
    codes = {"ok", "missing_runtime", "unsafe_permissions", "missing_skill", "manifest_missing", "invalid_manifest",
             "home_limit", "home_missing", "invalid_skill_link", "check_timeout", "api_timeout", "api_unavailable",
             "api_unknown", "missing_hook", "duplicate_hook", "hook_disabled", "hook_untrusted", "hook_matcher_mismatch",
             "login_off", "invalid_login_item", "health_failed"}
    statuses = {"pass", "warning", "off", "unknown"}
    raw_checks = health.get("checks", [])
    if not isinstance(raw_checks, list): raw_checks = []
    checks = [{"item": i + 1, "status": x.get("status") if x.get("status") in statuses else "unknown",
               "code": x.get("code") if x.get("code") in codes else "api_unknown"}
              for i, x in enumerate(raw_checks[:100]) if isinstance(x, dict)]
    kinds = {"complete", "battery", "test", "permission", "question", "application", "source_test", "control"}
    reasons = {"dns_error", "connect_error", "timeout", "tls_error", "transport_error", "http_auth", "endpoint_missing",
               "rate_limited", "server_error", "request_rejected", "api_rejected", "invalid_response", "sender_unavailable",
               "unknown_error", "partial", "multiple_failures", "local_config_error", "invalid_rules", "not_configured", "accepted"}
    counts = {"accepted": 0, "partial": 0, "send_failed": 0, "skipped": 0}
    failures = []
    rows = snap.get("events", [])
    for row in rows[-200:] if isinstance(rows, list) else []:
        if not isinstance(row, dict): continue
        state = row.get("status")
        if state in counts: counts[state] += 1
        elif row.get("category") == "skipped": counts["skipped"] += 1
        if state not in ("send_failed", "partial", "not_configured"): continue
        devices = row.get("device_results", [])
        devices = devices if isinstance(devices, list) else []
        failures.append({"kind": row.get("kind") if row.get("kind") in kinds else "application", "status": state,
                         "reason": row.get("reason") if row.get("reason") in reasons else "unknown_error",
                         "devices": [{"item": i + 1, "reason": x.get("reason") if x.get("reason") in reasons else "unknown_error"}
                                     for i, x in enumerate(devices[:16]) if isinstance(x, dict)]})
    version = "unknown"
    try:
        value = plistlib.loads((_app_for_status() / "Contents/Info.plist").read_bytes()).get("CFBundleShortVersionString")
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}", value): version = value
    except Exception: pass
    status = snap.get("status", {})
    status = status if isinstance(status, dict) else {}
    return {"ok": True, "report": {"schema": 1, "app_version": version,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "snapshot_available": snap.get("ok") is True,
            "configured": status.get("configured") is True, "enabled": status.get("enabled") is True,
            "health_summary": health.get("summary") if health.get("summary") in {"ready", "attention", "unknown"} else "unknown",
            "checks": checks, "recent_results": counts, "recent_failures": failures[-20:],
            "scope": "local_configuration_and_gateway_results_only"}}


def trust_block(records):
    rows = [START]
    for key, value in records.items():
        rows.extend(["[hooks.state." + json.dumps(key, ensure_ascii=False) + "]",
                     "enabled = true", "trusted_hash = " + json.dumps(value), ""])
    rows.append(END)
    return "\n".join(rows) + "\n"


def load_manifest():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"homes": {}, "backups": []}


def save_manifest(m):
    atomic(MANIFEST, json.dumps(m, ensure_ascii=False, indent=2) + "\n")


def update_common(block, backup_dir=None, remove=False):
    # Select/update ONLY the common TOML value, never account/provider rows or auth JSON.
    db = Path.home() / ".cc-switch/cc-switch.db"
    if not db.exists():
        return False
    c = sqlite3.connect(str(db), timeout=2)
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT value FROM settings WHERE key='common_config_codex'").fetchone()
        if not r:
            c.rollback()
            return False
        old = r[0]
        new = strip_block(old)
        if not remove:
            new = new.rstrip() + "\n\n" + block
        if old != new:
            if backup_dir:
                atomic(backup_dir / "cc-switch-common-config.toml", old)
            c.execute("UPDATE settings SET value=? WHERE key='common_config_codex' AND value=?", (new, old))
            if c.total_changes != 1:
                raise RuntimeError("CC Switch common config changed concurrently")
        c.commit()
        return True
    finally:
        c.close()


def attach(home, with_cc_switch=False):
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    b = BASE / "backups" / timestamp
    b.mkdir(parents=True, mode=0o700)
    config = home / "config.toml"
    hooks = home / "hooks.json"
    backup(config, b, "config.toml")
    backup(hooks, b, "hooks.json")
    config_existed = config.exists()
    old_config = config.read_text() if config_existed else ""
    old_hooks = hooks.read_text() if hooks.exists() else None
    h = json.loads(old_hooks) if old_hooks else {"hooks": {}}
    h.setdefault("hooks", {})
    entries = [
        ("PermissionRequest", None, True, "Codex Bark: waiting for permission"),
        ("PreToolUse", QUESTION_MATCHER, True, "Codex Bark: waiting for answer"),
        ("SessionStart", None, False, "Codex Bark: load question reminder"),
        ("Stop", None, True, "Codex Bark: turn finished"),
    ]
    for event, matcher, asynchronous, label in entries:
        groups = h["hooks"].setdefault(event, [])
        for group in groups:
            for hook in group.get("hooks", []):
                if is_our_command(hook.get("command")):
                    hook["command"] = CMD
        if any(x.get("command") == CMD for g in groups for x in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": CMD, "async": asynchronous,
                            "timeout": 3, "statusMessage": label}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    original_skill_present = (home / "skills/codex-bark-wait").exists() or (home / "skills/codex-bark-wait").is_symlink()
    atomic(hooks, json.dumps(h, ensure_ascii=False, indent=2) + "\n")
    try:
        loaded = hooks_list(home)
        ours = [x for x in loaded["hooks"] if x.get("command") == CMD]
        if loaded["errors"] or len(ours) != 4:
            raise RuntimeError("Codex did not load all 4 notification hooks")
        records = {x["key"]: x["currentHash"] for x in ours}
        block = trust_block(records)
        new_config = strip_block(old_config).rstrip() + "\n\n" + block
        atomic(config, new_config)
        loaded = hooks_list(home)
        ours = [x for x in loaded["hooks"] if x.get("command") == CMD]
        if loaded["errors"] or any(x["trustStatus"] != "trusted" for x in ours):
            raise RuntimeError("Notification hooks did not pass exact-definition trust verification")
        skill = home / "skills/codex-bark-wait"
        skill.parent.mkdir(exist_ok=True)
        if skill.exists() or skill.is_symlink():
            if not skill.is_symlink() or skill.resolve() != (BASE / "skill").resolve():
                raise RuntimeError("Existing unrelated codex-bark-wait skill; not replacing it")
        else:
            skill.symlink_to(BASE / "skill", target_is_directory=True)
        m = load_manifest()
        m.pop("uninstalled", None)
        original = m["homes"].get(str(home), {})
        m["homes"][str(home)] = {"trust": records, "skill_link": str(skill),
                                  "config_existed": original.get("config_existed", bool(old_config)),
                                  "hooks_existed": original.get("hooks_existed", old_hooks is not None)}
        m["backups"].append(str(b))
        # Combine all installed homes' entries so every account directory uses one key.
        all_records = {k: v for x in m["homes"].values() for k, v in x["trust"].items()}
        m["cc_switch_common_updated"] = (update_common(trust_block(all_records), b) if with_cc_switch else m.get("cc_switch_common_updated", False))
        save_manifest(m)
        print(json.dumps({"home": str(home), "trusted_hooks": len(ours),
                          "hook_errors": len(loaded["errors"]), "warnings": loaded["warnings"],
                          "cc_switch_common_updated": m["cc_switch_common_updated"],
                          "backup": str(b)}, ensure_ascii=False))
    except Exception:
        if config_existed: atomic(config, old_config)
        else: config.unlink(missing_ok=True)
        link = home / "skills/codex-bark-wait"
        if link.is_symlink() and link.resolve() == (BASE / "skill").resolve():
            if not original_skill_present: link.unlink()
        if old_hooks is None:
            hooks.unlink(missing_ok=True)
        else:
            atomic(hooks, old_hooks)
        raise


def remove_own_trust(text, records):
    text = strip_block(text)
    # Codex may reformat TOML and remove comments. Remove only our exact known trust keys.
    lines = text.splitlines(keepends=True)
    out = []
    skip = False
    for line in lines:
        if line.lstrip().startswith("["):
            skip = False
            match = re.match(r'^\s*\[hooks\.state\.("(?:[^"\\]|\\.)*")\]\s*$', line)
            if match and json.loads(match[1]) in records:
                skip = True
        if not skip:
            out.append(line)
    return "".join(out)


ANTIGRAVITY_HOOKS_PATH = Path.home() / ".gemini/config/hooks.json"
ANTIGRAVITY_HOOK_NAME = "bark-bridge-native"
ANTIGRAVITY_MATCHER = r"^(ask_question|ask_permission)$"


def _antigravity_file(path, limit=1048576):
    """Read only the selected Hook/public adapter metadata file, never settings/auth."""
    if path.is_symlink(): raise ValueError("unsafe_permissions")
    if not path.exists(): return None, None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > limit):
            raise ValueError("unsafe_permissions")
        raw = stream.read(limit + 1)
    if len(raw) > limit: raise ValueError("invalid_hooks")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("invalid_hooks")
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict): raise ValueError("invalid_hooks")
    return data, raw


def _antigravity_manifest():
    path = BASE / "antigravity_installation.json"
    data, _ = _antigravity_file(path, 262144)
    if data is not None and (data.get("schema") != 1
                            or data.get("hook_target") != str(ANTIGRAVITY_HOOKS_PATH)):
        raise ValueError("adapter_conflict")
    return data, path


def _antigravity_app(app=None):
    if app is None:
        manifest, _ = _antigravity_manifest()
        app = manifest.get("app_path") if manifest else None
    if not isinstance(app, (str, Path)): raise ValueError("app_not_found")
    path = Path(app)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir(): raise ValueError("app_not_found")
    info_path = path / "Contents/Info.plist"
    if info_path.is_symlink() or not info_path.is_file() or info_path.stat().st_size > 1048576:
        raise ValueError("app_not_found")
    info = plistlib.loads(info_path.read_bytes())
    executable = info.get("CFBundleExecutable")
    if (info.get("CFBundleIdentifier") != "com.google.antigravity"
            or not isinstance(executable, str) or Path(executable).name != executable):
        raise ValueError("app_not_found")
    binary = path / "Contents/MacOS" / executable
    if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
        raise ValueError("app_not_found")
    return path.resolve(), str(info.get("CFBundleShortVersionString", ""))[:40]


def _antigravity_command(event):
    return " ".join(shlex.quote(x) for x in [python_executable(), "-I", "-B", str(BASE / "bark.py"),
                                           "antigravity-hook", "--event", event])


def _antigravity_own_command(command):
    if not isinstance(command, str): return False
    try: tokens = shlex.split(command)
    except ValueError: return False
    return (len(tokens) == 7 and tokens[0] in ("/usr/bin/python3", str(BASE / "python/bin/python3"))
            and tokens[1:4] == ["-I", "-B", str(BASE / "bark.py")]
            and tokens[4:6] == ["antigravity-hook", "--event"] and tokens[6] in ("pre-tool", "stop"))


def _antigravity_strip(definition):
    """Remove only exact Bark commands, preserving user additions in our named group."""
    if not isinstance(definition, dict): raise ValueError("adapter_conflict")
    if "enabled" in definition and type(definition["enabled"]) is not bool:
        raise ValueError("adapter_conflict")
    clean, found = {}, False
    for name, value in definition.items():
        if name not in ("PreToolUse", "Stop"):
            clean[name] = value
            continue
        if not isinstance(value, list): raise ValueError("adapter_conflict")
        kept = []
        for item in value:
            if not isinstance(item, dict): raise ValueError("adapter_conflict")
            if _antigravity_own_command(item.get("command")):
                found = True
                continue
            if "hooks" not in item:
                kept.append(item)
                continue
            if not isinstance(item["hooks"], list): raise ValueError("adapter_conflict")
            hooks = [hook for hook in item["hooks"] if not (
                isinstance(hook, dict) and _antigravity_own_command(hook.get("command")))]
            removed = len(hooks) != len(item["hooks"])
            found = found or removed
            if not removed:
                kept.append(item)
            elif hooks or set(item) - {"matcher", "hooks"}:
                kept.append(dict(item, hooks=hooks))
        if kept or not value: clean[name] = kept
    return clean, found


def _antigravity_definition(definition=None):
    clean, _ = _antigravity_strip(definition or {})
    clean.setdefault("enabled", True)
    clean.setdefault("PreToolUse", []).append({"matcher": ANTIGRAVITY_MATCHER,
        "hooks": [{"type": "command", "command": _antigravity_command("pre-tool"), "timeout": 3}]})
    clean.setdefault("Stop", []).append({"type": "command", "command": _antigravity_command("stop"), "timeout": 3})
    return clean


@contextmanager
def _antigravity_lock():
    target = ANTIGRAVITY_HOOKS_PATH
    # Refuse user-controlled symlink components rather than following them to
    # unrelated settings. macOS's public /var alias is outside the home prefix.
    for parent in [target.parent] + list(target.parent.parents):
        if parent == Path.home(): break
        if parent.is_symlink(): raise ValueError("unsafe_permissions")
        if Path.home() in target.parents and parent.exists():
            info = parent.stat()
            if not parent.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("unsafe_permissions")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = target.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("unsafe_permissions")
    lock_path = target.parent / ".bark-native-hooks.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe_permissions")
        os.fchmod(fd, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError("adapter_busy") from None
        yield
    finally: os.close(fd)


def antigravity_status(app=None):
    available, version = False, None
    try:
        _, version = _antigravity_app(app)
        available = True
        data, _ = _antigravity_file(ANTIGRAVITY_HOOKS_PATH)
        definition = (data or {}).get(ANTIGRAVITY_HOOK_NAME)
        configured = False
        enabled = False
        if definition is not None:
            _, found = _antigravity_strip(definition)
            if not found: raise ValueError("adapter_conflict")
            expected = _antigravity_definition(definition)
            configured = definition == expected
            enabled = definition.get("enabled", True) is True
        return {"ok": True, "available": available, "version": version,
                "configured": configured, "enabled": configured and enabled,
                "reason": "attached" if configured and enabled else "disabled" if configured else "not_configured"}
    except Exception as error:
        reason = str(error) if str(error) in ("app_not_found", "unsafe_permissions", "adapter_conflict", "adapter_busy") else "invalid_hooks"
        return {"ok": False, "available": available, "version": version, "configured": False, "enabled": False, "reason": reason}


def antigravity_attach(app):
    try:
        app, _ = _antigravity_app(app)
        with _antigravity_lock():
            data, raw = _antigravity_file(ANTIGRAVITY_HOOKS_PATH)
            data = data or {}
            manifest, manifest_path = _antigravity_manifest()
            definition = data.get(ANTIGRAVITY_HOOK_NAME)
            if definition is not None and not _antigravity_strip(definition)[1]:
                raise ValueError("adapter_conflict")
            updated = dict(data, **{ANTIGRAVITY_HOOK_NAME: _antigravity_definition(definition)})
            if updated == data and manifest is not None and manifest.get("app_path") == str(app):
                return {"ok": True, "configured": True, "reason": "attached"}
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup_dir = BASE / "backups" / ("antigravity-" + timestamp)
            backup_dir.mkdir(parents=True, mode=0o700)
            if raw is not None: atomic(backup_dir / "hooks.json", raw.decode("utf-8"))
            record = {"schema": 1, "hook_target": str(ANTIGRAVITY_HOOKS_PATH), "app_path": str(app),
                      "hook_name": ANTIGRAVITY_HOOK_NAME, "hooks_existed": manifest.get("hooks_existed") if manifest else raw is not None,
                      "backups": (manifest.get("backups", []) if manifest else []) + [str(backup_dir)]}
            if _antigravity_file(ANTIGRAVITY_HOOKS_PATH)[1] != raw:
                raise ValueError("adapter_conflict")
            written = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
            try:
                atomic(ANTIGRAVITY_HOOKS_PATH, written)
                atomic(manifest_path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            except Exception:
                # Do not overwrite a third party's edit made after our write.
                if _antigravity_file(ANTIGRAVITY_HOOKS_PATH)[1] == written.encode("utf-8"):
                    if raw is None: ANTIGRAVITY_HOOKS_PATH.unlink(missing_ok=True)
                    else: atomic(ANTIGRAVITY_HOOKS_PATH, raw.decode("utf-8"))
                raise
        return {"ok": True, "configured": True, "reason": "attached"}
    except Exception as error:
        reason = str(error) if str(error) in ("app_not_found", "unsafe_permissions", "adapter_conflict", "adapter_busy") else "invalid_hooks"
        return {"ok": False, "configured": False, "reason": reason}


def antigravity_detach():
    try:
        with _antigravity_lock():
            data, raw = _antigravity_file(ANTIGRAVITY_HOOKS_PATH)
            manifest, manifest_path = _antigravity_manifest()
            if data is not None and ANTIGRAVITY_HOOK_NAME in data:
                clean, found = _antigravity_strip(data[ANTIGRAVITY_HOOK_NAME])
                if found:
                    updated = dict(data)
                    if set(clean) <= {"enabled"}: updated.pop(ANTIGRAVITY_HOOK_NAME)
                    else: updated[ANTIGRAVITY_HOOK_NAME] = clean
                    if _antigravity_file(ANTIGRAVITY_HOOKS_PATH)[1] != raw:
                        raise ValueError("adapter_conflict")
                    if not updated and manifest and manifest.get("hooks_existed") is False:
                        ANTIGRAVITY_HOOKS_PATH.unlink()
                    else: atomic(ANTIGRAVITY_HOOKS_PATH, json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
            manifest_path.unlink(missing_ok=True)
        return {"ok": True, "configured": False, "reason": "detached"}
    except Exception as error:
        reason = str(error) if str(error) in ("unsafe_permissions", "adapter_conflict", "adapter_busy") else "invalid_hooks"
        return {"ok": False, "configured": False, "reason": reason}


def uninstall():
    # Detach before any destructive removal so no native hook references a
    # deleted receiver. A conflict leaves installed programs/data untouched.
    if (BASE / "antigravity_installation.json").exists():
        detached = antigravity_detach()
        if detached.get("ok") is not True:
            print("Antigravity 通知入口未能移除，已停止卸载，请检查本地 Hook 配置后重试。")
            return
    adapter = BASE / "claude_bark.py"
    if adapter.is_file():
        result = subprocess.run([python_executable(), str(adapter), "detach"], capture_output=True, timeout=5)
        try: detached = json.loads(result.stdout)
        except ValueError: detached = {}
        if detached.get("ok") is not True:
            print("Claude Code 通知入口未能移除，已停止卸载，请检查其本地配置后重试。")
            return
    m = load_manifest()
    app_to_remove = None
    if m.get("menu_app"):
        try:
            app_to_remove = resolve_app()
        except Exception:
            print("应用位置暂不可用，已停止卸载，请先手动打开 Bark 后重试。")
            return
    for name, item in m["homes"].items():
        home = Path(name)
        p = home / "hooks.json"
        if p.exists():
            h = json.loads(p.read_text())
            for event in list(h.get("hooks", {})):
                groups = h["hooks"][event]
                for g in groups:
                    g["hooks"] = [x for x in g.get("hooks", []) if not is_our_command(x.get("command"))]
                h["hooks"][event] = [g for g in groups if g["hooks"]]
                if not h["hooks"][event]:
                    del h["hooks"][event]
            if not item["hooks_existed"] and not h.get("hooks") and len(h) == 1:
                p.unlink()
            else:
                atomic(p, json.dumps(h, ensure_ascii=False, indent=2) + "\n")
        p = home / "config.toml"
        if p.exists():
            atomic(p, remove_own_trust(p.read_text(), item["trust"]))
        link = Path(item["skill_link"])
        if link.is_symlink() and link.resolve() == (BASE / "skill").resolve():
            link.unlink()
    if m.get("cc_switch_common_updated") is True:
        update_common("", remove=True)
    # Delete the secret and local notification state. Keep code/backups for review/rollback.
    for pattern in ("config.json", "state.sqlite3*", "events*.jsonl", "menu-state.json", "sources.json", ".sources.lock", ".log.lock", ".config.lock", "rules.json", ".rules.lock"):
        for p in BASE.glob(pattern):
            p.unlink(missing_ok=True)
    gui = m.get("menu_app")
    if gui:
        import plistlib
        app = app_to_remove
        login = Path(gui["login_plist"])
        if login.exists():
            try:
                if plistlib.loads(login.read_bytes()).get("Label") == "local.codex.bark.menu":
                    login.unlink()
            except Exception:
                pass
        subprocess.run(["/bin/launchctl", "bootout", "gui/" + str(os.getuid()) + "/local.codex.bark.menu"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/pkill", "-x", "CodexBarkMenu"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/pkill", "-x", "CodexBarkPanel"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info = app / "Contents/Info.plist"
        if info.exists() and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") == "local.codex.bark.menu":
            shutil.rmtree(app)
            (BASE / "app-location.json").unlink(missing_ok=True)
    (BASE / ".menu-instance.lock").unlink(missing_ok=True)
    m["uninstalled"] = True
    save_manifest(m)
    print("已卸载通知入口、菜单栏应用和登录启动项，删除 Bark 密钥和通知状态。原配置及备份保留。请重启 Codex。")


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["attach", "check", "uninstall", "health", "support-report",
                                     "antigravity-attach", "antigravity-status", "antigravity-detach",
                                     "app-register", "app-locate", "launch-app", "login-set"])
    p.add_argument("home", nargs="?", default=str(Path.home() / ".codex"))
    p.add_argument("--app", type=Path)
    p.add_argument("--with-cc-switch", action="store_true", help="Explicitly merge Bark Hook trust into CC Switch common config")
    a = p.parse_args()
    if a.action in ("antigravity-attach", "antigravity-status", "antigravity-detach"):
        result = (antigravity_attach(a.app) if a.action == "antigravity-attach" else
                  antigravity_status(a.app) if a.action == "antigravity-status" else antigravity_detach())
        print(json.dumps(result, ensure_ascii=False))
        if result.get("ok") is not True: raise SystemExit(1)
    elif a.action in ("app-register", "app-locate", "launch-app", "login-set"):
        try:
            if a.action == "app-register":
                if a.app is None:
                    raise RuntimeError("app_location_invalid")
                result = {"ok": True, "path": str(register_app(a.app))}
            elif a.action == "app-locate":
                result = {"ok": True, "path": str(resolve_app())}
            elif a.action == "launch-app":
                result = {"ok": True, "path": str(launch_app())}
            else:
                if a.home not in ("on", "off"):
                    raise RuntimeError("invalid_login_item")
                set_login(a.home == "on")
                result = {"ok": True}
        except Exception:
            result = {"ok": False, "error": "app_location_unavailable"}
        print(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            raise SystemExit(1)
    elif a.action == "support-report":
        try: result = support_report()
        except Exception: result = {"ok": False, "error": "report_unavailable"}
        print(json.dumps(result, ensure_ascii=False))
    elif a.action == "health":
        try:
            result = health_report()
        except Exception:
            result = {"ok": True, "summary": "unknown", "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "checks": [_item("health", "接入自检", "unknown", "health_failed", "暂无法完成本地接入检查，请稍后重试。") ]}
        print(json.dumps(result, ensure_ascii=False))
    elif a.action == "attach":
        attach(a.home, with_cc_switch=a.with_cc_switch)
    elif a.action == "uninstall":
        uninstall()
    else:
        d = hooks_list(Path(a.home).expanduser().resolve())
        print(json.dumps({"hooks": [{k: x[k] for k in ("eventName", "enabled", "trustStatus", "async")} for x in d["hooks"] if x.get("command") == CMD],
                          "errors": d["errors"], "warnings": d["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
