#!/usr/bin/env python3
from __future__ import annotations

import copy
import datetime
import tempfile
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import bark  # noqa: E402

SOURCE_ID = "claude-code"
SOURCE_NAME = "Claude Code"
MAX_SETTINGS_BYTES = 1048576
MAX_STDIN_BYTES = 1048576
ASK_USER_QUESTION_MATCHER = "^AskUserQuestion$"
MANAGED_EVENTS = ("PermissionRequest", "PreToolUse", "Stop")

VERSION_RE = re.compile(r"\b([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._-]+)?)\b")
SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

# Injectable overrides for isolated testing
CLAUDE_BIN: str | Path | None = None
SETTINGS_PATH: Path | None = None
MANIFEST_FILE = BASE / "claude_installation.json"

STATUS_KEYS = ("ok", "available", "version", "configured", "reason")
ACTION_KEYS = ("ok", "configured", "reason")


def script_path() -> Path:
    return (BASE / "claude_bark.py").resolve()


def hook_command() -> str:
    return f"{shlex.quote(bark.python_executable())} {shlex.quote(str(script_path()))} hook"


def is_our_command(cmd: Any) -> bool:
    if not isinstance(cmd, str): return False
    try: tokens = shlex.split(cmd.strip())
    except ValueError: return False
    return len(tokens) == 3 and tokens[0] in {"/usr/bin/python3", str(BASE / "python/bin/python3")} and tokens[1:] == [str(script_path()), "hook"]


def settings_path() -> Path:
    if SETTINGS_PATH is not None:
        return Path(SETTINGS_PATH)
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env_dir:
        directory = Path(env_dir).expanduser()
        if not directory.is_absolute(): raise ValueError("invalid_settings")
        return directory / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def manifest_path() -> Path:
    return Path(MANIFEST_FILE)


def candidate_claude_binaries() -> list[Path]:
    if CLAUDE_BIN is not None:
        return [Path(CLAUDE_BIN)] if str(CLAUDE_BIN).strip() else []
    candidates: list[Path] = []
    which_hit = shutil.which("claude")
    if which_hit:
        candidates.append(Path(which_hit))
    home = Path.home()
    for p in (
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global" / "bin" / "claude",
    ):
        if p not in candidates:
            candidates.append(p)
    return candidates


def detect_claude_cli() -> tuple[bool, str | None, str]:
    uid = os.getuid()
    for candidate in candidate_claude_binaries():
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if not os.access(candidate, os.X_OK):
            continue
        if st.st_uid not in (0, uid):
            continue
        try:
            proc = subprocess.run(
                [str(candidate), "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        match = VERSION_RE.search(proc.stdout or "")
        if match:
            return True, match.group(1), "ok"
    return False, None, "cli_missing"


def validate_safe_dir(path: Path) -> tuple[bool, str]:
    if not path.is_absolute(): return False, "unsafe_permissions"
    # macOS /var and /tmp are system symlinks. Resolve only that shared system prefix;
    # user-controlled components below home must not redirect settings writes.
    home = Path.home()
    if path == home or home in path.parents:
        for parent in [path] + list(path.parents):
            if parent == home: break
            if parent.is_symlink(): return False, "unsafe_permissions"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True, "ok"
    except OSError:
        return False, "io_error"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False, "unsafe_permissions"
    if st.st_uid != os.getuid():
        return False, "unsafe_permissions"
    if stat.S_IMODE(st.st_mode) & 0o022:
        return False, "unsafe_permissions"
    return True, "ok"


def validate_safe_file(path: Path, max_bytes: int = MAX_SETTINGS_BYTES) -> tuple[bool, str]:
    ok_dir, reason_dir = validate_safe_dir(path.parent)
    if not ok_dir:
        return False, reason_dir
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True, "missing"
    except OSError:
        return False, "io_error"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False, "unsafe_permissions"
    if st.st_uid != os.getuid():
        return False, "unsafe_permissions"
    if stat.S_IMODE(st.st_mode) & 0o022:
        return False, "unsafe_permissions"
    if st.st_size > max_bytes:
        return False, "invalid_settings"
    return True, "ok"


def ensure_safe_parent_dir(path: Path) -> tuple[bool, str]:
    parent = path.parent
    try:
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(parent, 0o700)
    except OSError:
        return False, "io_error"
    return validate_safe_dir(parent)


@contextmanager
def settings_lock(target_settings: Path) -> Iterator[None]:
    ok_dir, reason = ensure_safe_parent_dir(target_settings)
    if not ok_dir:
        raise PermissionError(reason)
    lock_file = target_settings.parent / ".claude-bark.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_file, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError("unsafe_permissions") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError("unsafe_permissions")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_safe_json_dict(path: Path) -> tuple[bool, dict[str, Any] | None, bool, str]:
    """Returns (ok, data, existed, reason)."""
    safe, reason = validate_safe_file(path)
    if not safe:
        return False, None, False, reason
    if reason == "missing":
        return True, {}, False, "missing"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return False, None, True, "unsafe_permissions"
        return False, None, True, "io_error"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (stat.S_IMODE(st.st_mode) & 0o022):
            return False, None, True, "unsafe_permissions"
        if st.st_size > MAX_SETTINGS_BYTES:
            return False, None, True, "invalid_settings"
        raw = os.read(fd, MAX_SETTINGS_BYTES + 1)
    except OSError:
        return False, None, True, "io_error"
    finally:
        os.close(fd)
    if len(raw) > MAX_SETTINGS_BYTES:
        return False, None, True, "invalid_settings"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None, True, "invalid_settings"
    if not isinstance(parsed, dict):
        return False, None, True, "invalid_settings"
    return True, parsed, True, "ok"


def validate_hooks_structure(data: dict[str, Any]) -> bool:
    if "hooks" not in data:
        return True
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event_name in MANAGED_EVENTS:
        if event_name not in hooks:
            continue
        groups = hooks.get(event_name)
        if not isinstance(groups, list):
            return False
        for group in groups:
            if not isinstance(group, dict):
                return False
            inner = group.get("hooks")
            if inner is not None and not isinstance(inner, list):
                return False
    return True


def atomic_write_json(path: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    ok_dir, reason = ensure_safe_parent_dir(path)
    if not ok_dir:
        return False, reason
    safe_existing, reason_existing = validate_safe_file(path)
    if not safe_existing:
        return False, reason_existing
    tmp_path = None
    try:
        fd, name = tempfile.mkstemp(prefix=".claude-bark-", dir=path.parent)
        tmp_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n"); file.flush(); os.fsync(file.fileno())
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path is not None: tmp_path.unlink(missing_ok=True)
        return False, "io_error"
    return True, "ok"


def _group_has_our_hook(group: dict[str, Any], expected_matcher: str | None) -> bool:
    matcher = group.get("matcher")
    if expected_matcher is None:
        if matcher not in (None, ""):
            return False
    else:
        if matcher != expected_matcher:
            return False
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return False
    for item in inner:
        if isinstance(item, dict) and item.get("type") == "command" and is_our_command(item.get("command")):
            return True
    return False


def is_configured(data: dict[str, Any]) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    expected = (
        ("PermissionRequest", None),
        ("PreToolUse", ASK_USER_QUESTION_MATCHER),
        ("Stop", None),
    )
    for event_name, matcher in expected:
        groups = hooks.get(event_name)
        if not isinstance(groups, list):
            return False
        if not any(isinstance(g, dict) and _group_has_our_hook(g, matcher) for g in groups):
            return False
    return True


def has_any_our_hook(data: dict[str, Any]) -> bool:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for item in inner:
                if isinstance(item, dict) and is_our_command(item.get("command")):
                    return True
    return False


def strip_our_hooks(hooks_dict: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for event_name, groups in hooks_dict.items():
        if not isinstance(groups, list):
            cleaned[event_name] = copy.deepcopy(groups)
            continue
        new_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(copy.deepcopy(group))
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                new_groups.append(copy.deepcopy(group))
                continue
            kept_inner = [
                copy.deepcopy(item)
                for item in inner
                if not (isinstance(item, dict) and is_our_command(item.get("command")))
            ]
            removed = len(kept_inner) != len(inner)
            if not removed:
                new_groups.append(copy.deepcopy(group))
            elif kept_inner:
                updated_group = copy.deepcopy(group)
                updated_group["hooks"] = kept_inner
                new_groups.append(updated_group)
        if new_groups:
            cleaned[event_name] = new_groups
    return cleaned


def merge_hooks_into_settings(data: dict[str, Any], cmd: str | None = None) -> dict[str, Any]:
    command_str = cmd or hook_command()
    merged = copy.deepcopy(data)
    existing_hooks = merged.get("hooks")
    hooks_obj = strip_our_hooks(existing_hooks if isinstance(existing_hooks, dict) else {})

    specs: tuple[tuple[str, dict[str, Any]], ...] = (
        ("PermissionRequest", {"hooks": [{"type": "command", "command": command_str, "timeout": 3}]}),
        (
            "PreToolUse",
            {
                "matcher": ASK_USER_QUESTION_MATCHER,
                "hooks": [{"type": "command", "command": command_str, "timeout": 3}],
            },
        ),
        ("Stop", {"hooks": [{"type": "command", "command": command_str, "timeout": 3}]}),
    )
    for event_name, group_entry in specs:
        event_list = hooks_obj.get(event_name)
        if not isinstance(event_list, list):
            event_list = []
        event_list.append(group_entry)
        hooks_obj[event_name] = event_list

    merged["hooks"] = hooks_obj
    return merged


def remove_hooks_from_settings(data: dict[str, Any], manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    existing_hooks = updated.get("hooks")
    if not isinstance(existing_hooks, dict):
        return updated

    cleaned_hooks = strip_our_hooks(existing_hooks)
    orig_hooks = manifest.get("original_hooks") if isinstance(manifest, dict) else None
    if isinstance(orig_hooks, dict):
        for k, v in orig_hooks.items():
            if k not in cleaned_hooks and isinstance(v, list) and len(v) == 0:
                cleaned_hooks[k] = []

    hooks_existed = bool(manifest.get("hooks_existed")) if isinstance(manifest, dict) else False
    if cleaned_hooks or hooks_existed:
        updated["hooks"] = cleaned_hooks
    else:
        updated.pop("hooks", None)
    return updated


def load_manifest() -> tuple[bool, dict[str, Any] | None]:
    mpath = manifest_path()
    ok, data, existed, _ = read_safe_json_dict(mpath)
    if not ok:
        return False, None
    if not existed:
        return True, None
    return True, data


def save_manifest(
    settings_existed: bool,
    hooks_existed: bool,
    original_hooks: Any,
) -> tuple[bool, str]:
    payload = {
        "version": 1,
        "source_id": SOURCE_ID,
        "settings_target": str(settings_path()),
        "settings_existed": bool(settings_existed),
        "hooks_existed": bool(hooks_existed),
        "original_hooks": copy.deepcopy(original_hooks) if hooks_existed and isinstance(original_hooks, dict) else None,
        "attached_events": list(MANAGED_EVENTS),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return atomic_write_json(manifest_path(), payload)


def whitelist_status(
    ok: bool,
    available: bool,
    version: str | None,
    configured: bool,
    reason: str,
) -> dict[str, Any]:
    out = {
        "ok": bool(ok),
        "available": bool(available),
        "version": str(version) if isinstance(version, str) and version else None,
        "configured": bool(configured),
        "reason": str(reason),
    }
    return {k: out[k] for k in STATUS_KEYS}


def whitelist_action(ok: bool, configured: bool, reason: str) -> dict[str, Any]:
    out = {
        "ok": bool(ok),
        "configured": bool(configured),
        "reason": str(reason),
    }
    return {k: out[k] for k in ACTION_KEYS}


def status() -> dict[str, Any]:
    available, version, _ = detect_claude_cli()
    try: spath = settings_path()
    except ValueError: return whitelist_status(False, available, version, False, "invalid_settings")
    if not available and not manifest_path().exists() and not spath.exists() and not spath.is_symlink():
        return whitelist_status(True, False, None, False, "cli_missing")
    ok, data, existed, reason = read_safe_json_dict(spath)
    if not ok or data is None:
        return whitelist_status(False, available, version, False, reason)
    if existed and not validate_hooks_structure(data):
        return whitelist_status(False, available, version, False, "invalid_settings")
    configured = is_configured(data) if existed else False
    if configured and available:
        final_reason = "ready"
    elif configured and not available:
        final_reason = "cli_missing"
    elif not configured and available:
        final_reason = "not_attached"
    else:
        final_reason = "cli_missing"
    return whitelist_status(True, available, version, configured, final_reason)


def attach() -> dict[str, Any]:
    available, _, _ = detect_claude_cli()
    if not available:
        return whitelist_action(False, False, "cli_missing")

    try: spath = settings_path()
    except ValueError: return whitelist_action(False, False, "invalid_settings")
    safe_pre, reason_pre = validate_safe_file(spath)
    if not safe_pre:
        return whitelist_action(False, False, reason_pre)

    try:
        with settings_lock(spath):
            ok, data, existed, reason = read_safe_json_dict(spath)
            if not ok or data is None:
                return whitelist_action(False, False, reason)
            if existed and not validate_hooks_structure(data):
                return whitelist_action(False, False, "invalid_settings")

            m_ok, existing_manifest = load_manifest()
            if not m_ok:
                return whitelist_action(False, False, "unsafe_permissions")

            if existing_manifest and existing_manifest.get("settings_target", str(spath)) != str(spath):
                return whitelist_action(False, False, "other_configuration_registered")
            if existing_manifest is None or not has_any_our_hook(data):
                hooks_existed = existed and ("hooks" in data) and isinstance(data.get("hooks"), dict)
                orig_hooks = copy.deepcopy(data.get("hooks")) if hooks_existed else None
                saved_ok, saved_reason = save_manifest(
                    settings_existed=existed,
                    hooks_existed=hooks_existed,
                    original_hooks=orig_hooks,
                )
                if not saved_ok:
                    return whitelist_action(False, False, saved_reason)

            merged = merge_hooks_into_settings(data, hook_command())
            write_ok, write_reason = atomic_write_json(spath, merged)
            if not write_ok:
                return whitelist_action(False, False, write_reason)
    except PermissionError as exc:
        err_reason = str(exc) if str(exc) in ("unsafe_permissions", "io_error") else "unsafe_permissions"
        return whitelist_action(False, False, err_reason)
    except OSError:
        return whitelist_action(False, False, "io_error")

    return whitelist_action(True, True, "attached")


def detach() -> dict[str, Any]:
    m_ok, initial_manifest = load_manifest()
    if not m_ok: return whitelist_action(False, False, "unsafe_permissions")
    target = initial_manifest.get("settings_target") if initial_manifest else None
    try: spath = Path(target) if isinstance(target, str) and Path(target).is_absolute() else settings_path()
    except ValueError: return whitelist_action(False, False, "invalid_settings")
    if initial_manifest is None and not detect_claude_cli()[0] and not spath.exists() and not spath.is_symlink():
        return whitelist_action(True, False, "detached")
    safe_pre, reason_pre = validate_safe_file(spath)
    if not safe_pre:
        return whitelist_action(False, False, reason_pre)

    if reason_pre == "missing":
        mpath = manifest_path()
        m_safe, m_reason = validate_safe_file(mpath)
        if not m_safe:
            return whitelist_action(False, False, m_reason)
        if m_reason != "missing":
            try:
                mpath.unlink(missing_ok=True)
            except OSError:
                pass
        return whitelist_action(True, False, "detached")

    try:
        with settings_lock(spath):
            ok, data, existed, reason = read_safe_json_dict(spath)
            if not ok or data is None:
                return whitelist_action(False, False, reason)
            if existed and not validate_hooks_structure(data):
                return whitelist_action(False, False, "invalid_settings")

            m_ok, manifest = load_manifest()
            if not m_ok:
                return whitelist_action(False, False, "unsafe_permissions")

            if existed and has_any_our_hook(data):
                cleaned = remove_hooks_from_settings(data, manifest)
                if not cleaned and manifest and manifest.get("settings_existed") is False:
                    spath.unlink()
                else:
                    write_ok, write_reason = atomic_write_json(spath, cleaned)
                    if not write_ok:
                        return whitelist_action(False, False, write_reason)

            mpath = manifest_path()
            if mpath.exists():
                try:
                    mpath.unlink(missing_ok=True)
                except OSError:
                    pass
    except PermissionError as exc:
        err_reason = str(exc) if str(exc) in ("unsafe_permissions", "io_error") else "unsafe_permissions"
        return whitelist_action(False, False, err_reason)
    except OSError:
        return whitelist_action(False, False, "io_error")

    return whitelist_action(True, False, "detached")


def extract_project_label(raw_cwd: Any) -> str:
    if not isinstance(raw_cwd, str):
        return ""
    trimmed = raw_cwd.strip().rstrip("/\\")
    if not trimmed:
        return ""
    base = os.path.basename(trimmed)
    if not base or base in (".", ".."):
        return ""
    return bark.preview_text(base, 40)


def event_from_claude_hook(raw: bytes) -> dict[str, Any] | None:
    if not raw or len(raw) > MAX_STDIN_BYTES:
        return None
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None

    hook_event = str(d.get("hook_event_name") or "").strip()
    if hook_event not in MANAGED_EVENTS:
        return None

    raw_session = str(d.get("session_id") or "local").strip() or "local"
    session_hash = bark.digest(["claude", raw_session])
    project_label = extract_project_label(d.get("cwd"))

    event: dict[str, Any] = {
        "origin": "integration",
        "source_id": SOURCE_ID,
        "source": SOURCE_NAME,
        "session": session_hash,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if project_label:
        event["project_label"] = project_label

    if hook_event == "PermissionRequest":
        tool_name = d.get("tool_name")
        tool_input = d.get("tool_input")
        event["kind"] = "permission"
        event["fingerprint"] = bark.digest(["claude", raw_session, tool_name, tool_input])
        if isinstance(tool_name, str) and SAFE_TOOL_RE.match(tool_name):
            event["tool"] = tool_name
        return event

    if hook_event == "PreToolUse":
        tool_name = str(d.get("tool_name") or "").strip()
        if tool_name != "AskUserQuestion":
            return None
        tool_input = d.get("tool_input")
        event["kind"] = "question"
        event["fingerprint"] = bark.digest(["claude", raw_session, tool_input])
        questions = bark.question_preview(tool_input)
        if questions:
            event["questions"] = questions
        return event

    if hook_event == "Stop":
        if d.get("stop_hook_active") is True:
            return None
        event["kind"] = "complete"
        last_msg = d.get("last_assistant_message") or d.get("message") or d.get("stop_reason")
        if isinstance(last_msg, str) and last_msg.strip():
            dedup_token: Any = last_msg.strip()
        else:
            dedup_token = int(time.time() // 120)
        event["fingerprint"] = bark.digest(["claude_stop", raw_session, dedup_token])
        return event

    return None


def run_hook() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        event = event_from_claude_hook(raw)
        if event is not None:
            bark.dispatch(event)
    except BaseException:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "status"

    if action == "hook":
        return run_hook()

    try:
        if action == "status":
            res = status()
        elif action == "attach":
            res = attach()
        elif action == "detach":
            res = detach()
        else:
            res = whitelist_action(False, False, "unknown_action")
        sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
        return 0
    except BaseException:
        fallback = (
            whitelist_status(False, False, None, False, "internal_error")
            if action == "status"
            else whitelist_action(False, False, "internal_error")
        )
        try:
            sys.stdout.write(json.dumps(fallback, ensure_ascii=False) + "\n")
        except BaseException:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
