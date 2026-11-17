#!/usr/bin/python3
"""Local Codex -> Bark notifier. No Codex credentials or transcripts are read."""
import argparse
import concurrent.futures
from collections import deque
import datetime
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import unicodedata

BASE = Path(__file__).resolve().parent


def python_executable():
    bundled = BASE / "python/bin/python3"
    return str(bundled) if bundled.is_file() else "/usr/bin/python3"

CONFIG = BASE / "config.json"
STATE = BASE / "state.sqlite3"
LOG = BASE / "events.jsonl"
SOURCES = BASE / "sources.json"
RULES = BASE / "rules.json"
STATUSES = {"accepted", "partial", "send_failed", "not_configured", "disabled", "source_disabled", "unknown_source", "no_devices_enabled", "deduplicated", "enabled", "quiet_hours", "route_no_targets", "unknown_device"}
REASONS = {"accepted", "partial", "dns_error", "connect_error", "timeout", "tls_error", "transport_error", "http_auth", "endpoint_missing", "rate_limited", "server_error", "request_rejected", "api_rejected", "invalid_response", "sender_unavailable", "unknown_error", "multiple_failures", "local_config_error", "invalid_rules", "not_configured", "disabled", "source_disabled", "unknown_source", "no_devices_enabled", "quiet_hours", "route_no_targets", "unknown_device", "deduplicated"}
KINDS = {"complete", "battery", "test", "permission", "question", "application", "source_test", "control"}
STATUSES.add("event_disabled")
REASONS.add("event_disabled")
STATUSES.add("source_muted")
REASONS.add("source_muted")
CODEX_EVENT_DEFAULTS = {"permission": True, "question": True, "complete": True}
# Public, immutable artwork; never include device or chat information in this URL.
# Bark on iOS 15+ fetches it. The Mac only sends the optional API field.
SOURCE_ICONS = {
    "codex": "https://raw.githubusercontent.com/dneo9527-ctrl/bark-bridge-macos/c664b67c01a44e9ed698dfccc69d1c6b5d1763b4/docs/media/codex-source-icon.png",
}

QUESTION_TOOLS = re.compile(r"(?:^|__|\.)(?:request_user_input|request_user_input_async|ask_user|ask_user_question)$")
CONTEXT = f"""Local user preference: use the codex-bark-wait skill before asking a question or presenting choices that require a reply. Read {BASE / "skill/SKILL.md"}. The user requested chat names and short question/option previews on their existing Bark phone. Supply the minimal question and choice labels to wait --preview-json through stdin, never credentials, commands or transcripts. Failures must not block work. PermissionRequest and supported question tools have hooks. Do not change permissions or approve actions. Never read or print the Bark config or any account credential."""


def preview_text(value, limit):
    if not isinstance(value, str): return ""
    value = " ".join("".join(c if not unicodedata.category(c).startswith("C") else " " for c in value[:8192]).split())
    value = re.sub(r"https?://\S+", "[链接已隐藏]", value, flags=re.I)
    value = re.sub(r"\bBearer\s+\S+", "Bearer [已隐藏]", value, flags=re.I)
    value = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}", "[密钥已隐藏]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[令牌已隐藏]", value)
    value = re.sub(r"(?i)(api[_ -]?key|device[_ -]?key|token|password|secret|密钥|密码)\s*[:=：]\s*[^\s,，;；]+", r"\1=[已隐藏]", value)
    encoded = value.encode("utf-8")
    return value if len(encoded) <= limit else encoded[:max(0, limit-3)].decode("utf-8", "ignore") + "…"


def question_preview(data):
    if isinstance(data, str):
        try: data = json.loads(data[:32768])
        except (ValueError, TypeError): return []
    if not isinstance(data, dict): return []
    questions = data.get("questions")
    if not isinstance(questions, list): questions = [data]
    out = []
    for q in questions[:2]:
        if not isinstance(q, dict): continue
        if q.get("isSecret") is True or q.get("is_secret") is True:
            out.append({"question": "此问题涉及敏感输入，请返回 Mac 查看。", "options": []}); continue
        text = preview_text(q.get("question") or q.get("title"), 300)
        if not text: continue
        options = q.get("options", q.get("choices", []))
        clean = []
        if isinstance(options, list):
            for option in options[:5]:
                label = preview_text(option.get("label") if isinstance(option, dict) else option, 60)
                if label: clean.append(label)
        item = {"question": text, "options": clean}
        if q.get("more") is True or (isinstance(options,list) and len(options)>5): item["more"] = True
        out.append(item)
    if len(questions)>2 and out: out[-1]["more"] = True
    return out


def read_preview_input():
    # An interactive caller can write one compact JSON line without putting its
    # content in argv, shell commands, temporary files or terminal echo.
    if not sys.stdin.isatty(): return sys.stdin.buffer.read(32768)
    import termios
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    hidden = list(previous); hidden[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSANOW, hidden)
        return sys.stdin.buffer.readline(32768)
    finally: termios.tcsetattr(fd, termios.TCSANOW, previous)


def chat_id(value):
    try: return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError: return None


def hook_chat_id(data):
    # Hook session_id can identify a live session rather than a stored chat.
    # A transcript filename identifies its chat; never open the transcript.
    explicit = chat_id(data.get("thread_id"))
    transcript_ident = None
    transcript = data.get("transcript_path")
    if isinstance(transcript, str) and len(transcript) <= 4096:
        match = re.fullmatch(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-fA-F-]{36})\.jsonl", Path(transcript).name)
        if match:
            transcript_ident = chat_id(match[1])
    if explicit and transcript_ident and explicit != transcript_ident: return None
    if explicit or transcript_ident: return explicit or transcript_ident
    # A different inherited CODEX_THREAD_ID must not relabel another session.
    session = chat_id(data.get("session_id"))
    return session if data.get("session_id") else chat_id(os.environ.get("CODEX_THREAD_ID"))


def indexed_chat_name(home, ident):
    # Only read the dedicated name index, with a bounded tail and no writes.
    path = home / "session_index.jsonl"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022: return ""
            start = max(0, info.st_size - 1048576)
            stream.seek(start)
            if start: stream.readline(16384)
            raw = stream.read(1048576)
        deadline = time.monotonic() + .05
        for line in reversed(raw.splitlines()):
            if time.monotonic() > deadline: break
            if len(line) > 16384: continue
            try: row = json.loads(line)
            except (ValueError, UnicodeError): continue
            if not isinstance(row, dict) or row.get("id") != ident: continue
            value = row.get("thread_name")
            if isinstance(value, str) and 0 < len(value) <= 200 and "\n" not in value and "\r" not in value:
                return preview_text(value, 100)
            return ""
        return ""
    except (OSError, ValueError): return ""


def metadata_chat_name(home, ident):
    paths = [p for p in home.glob("state_*.sqlite") if re.fullmatch(r"state_[0-9]+\.sqlite", p.name)]
    if not paths: return ""
    path = max(paths, key=lambda p: int(p.stem.split("_")[1]))
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid(): return ""
    conn = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=.05)
    try:
        deadline = time.monotonic()+.05
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 100)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        # Some title columns contain the full initial prompt. Never retrieve those.
        name = "CASE WHEN length(name)<=200 AND instr(name,char(10))=0 AND instr(name,char(13))=0 THEN name ELSE NULL END" if "name" in columns else "NULL"
        title = "CASE WHEN length(title)<=80 AND instr(title,char(10))=0 AND instr(title,char(13))=0 THEN title ELSE NULL END" if "title" in columns else "NULL"
        row = conn.execute("SELECT "+name+", "+title+" FROM threads WHERE id=?", (ident,)).fetchone()
        if row and preview_text(row[0],100): return preview_text(row[0],100)
        return preview_text(row[1],100) if row else ""
    finally: conn.close()


def chat_name(event):
    supplied = preview_text(event.get("chat_title"), 100)
    if supplied: return supplied
    ident = chat_id(event.get("chat_id"))
    if not ident: return "Codex"
    fallback = "聊天 " + ident[:8]
    try:
        home = Path(os.environ.get("CODEX_HOME") or str(Path.home()/".codex")).expanduser()
        if not home.is_absolute() or home.is_symlink() or not home.is_dir() or home.stat().st_uid != os.getuid(): return fallback
        # Renamed chats can live in the name index while SQLite is still stale.
        indexed = indexed_chat_name(home, ident)
        if indexed: return indexed
        return metadata_chat_name(home, ident) or fallback
    except (OSError, sqlite3.Error, ValueError): return fallback


def codex_label(event, default_title, default_body):
    kind = event.get("kind")
    if kind not in ("permission", "question", "complete"): return default_title, default_body
    title = preview_text(chat_name(event) + " · " + {"permission":"等待授权", "question":"等待回答", "complete":"本轮已完成"}[kind], 180)
    if kind == "question":
        questions = question_preview({"questions":event.get("questions", [])})
        lines = []
        for index,q in enumerate(questions):
            lines.append((str(index+1)+"）" if len(questions)>1 else "")+q["question"])
            lines.extend(str(i+1)+". "+label for i,label in enumerate(q["options"]))
            if q.get("more"): lines.append("…更多内容请返回 Mac 查看。")
        if lines:
            body = "\n".join(lines)
            if len(body.encode('utf-8'))>1400: body=body.encode('utf-8')[:1397].decode('utf-8','ignore')+'…'
            return title,body
    if kind == "permission":
        tool = event.get("tool")
        if isinstance(tool,str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}",tool):
            return title,"工具："+tool+"\n请返回 Mac 查看授权请求并决定。"
    return title,default_body


def private_dir():
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BASE, 0o700)


def atomic_json(path, data):
    private_dir()
    fd, tmp = tempfile.mkstemp(prefix=".bark-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_id(value, fallback="unknown"):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value) else fallback


def safe_details(data):
    out = {}
    for key in ("http", "delivered", "total"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = max(0, min(999 if key == "http" else 16, value))
    if "source_id" in data: out["source_id"] = safe_id(data["source_id"])
    if data.get("reason") in REASONS: out["reason"] = data["reason"]
    if isinstance(data.get("reasons"), dict):
        out["reasons"] = {k: min(16, v) for k, v in data["reasons"].items() if k in REASONS and isinstance(v, int) and 0 < v <= 16}
    if isinstance(data.get("device_results"), list):
        out["device_results"] = [{"device_id": safe_id(x.get("device_id")), "status": x.get("status") if x.get("status") in ("accepted", "send_failed") else "send_failed", "reason": x.get("reason") if x.get("reason") in REASONS else "unknown_error", "http": max(0, min(999, int(x.get("http", 0))))} for x in data["device_results"][:16] if isinstance(x, dict)]
    if isinstance(data.get("battery_percent"), int) and not isinstance(data.get("battery_percent"), bool): out["battery_percent"] = max(0, min(100, data["battery_percent"]))
    if data.get("battery_level") in (10, 20): out["battery_level"] = data["battery_level"]
    return out


def log(status, kind="", **extra):
    # Only fixed status words / hashes / HTTP codes. Never log key, URL, commands or questions.
    try:
        private_dir()
        fd = os.open(str(BASE / ".log.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if LOG.exists() and LOG.stat().st_size > 262144:
                os.replace(LOG, BASE / "events.previous.jsonl")
            row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "status": status if status in STATUSES else "send_failed", "kind": kind if kind in KINDS else "application"}
            extra.setdefault("source_id", "system" if kind in ("test", "control") else "unknown")
            row.update(safe_details(extra))
            fd = os.open(str(LOG), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as out:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def validate_target(server, key):
    u = urllib.parse.urlsplit(server)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError("invalid_server")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key):
        raise ValueError("invalid_key")


def config():
    st = CONFIG.stat()
    if CONFIG.is_symlink() or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("unsafe_config_permissions")
    d = json.loads(CONFIG.read_text())
    if "devices" not in d:
        devices = []
        if d.get("device_key"):
            devices.append({"id": "primary", "name": "我的 iPhone", "enabled": True,
                            "server": d.get("server", ""), "device_key": d["device_key"]})
        d["devices"] = devices
    if not isinstance(d["devices"], list) or len(d["devices"]) > 16:
        raise ValueError("invalid_devices")
    seen = set()
    ids = set()
    for device in d["devices"]:
        validate_target(device.get("server", ""), device.get("device_key", ""))
        identity = (device["server"].rstrip("/"), device["device_key"])
        if identity in seen or device.get("id") in ids or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device.get("id", "")):
            raise ValueError("duplicate_or_invalid_device")
        seen.add(identity); ids.add(device["id"])
        if not isinstance(device.get("name"), str) or not 1 <= len(device["name"]) <= 40:
            raise ValueError("invalid_device_name")
    d.pop("server", None); d.pop("device_key", None)
    d["version"] = 2
    return d


def modify_config(operation, create=False):
    private_dir()
    fd = os.open(str(BASE / ".config.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not CONFIG.exists() and create:
            d = {"version": 2, "enabled": True, "devices": []}
        else:
            d = config()
        result = operation(d)
        atomic_json(CONFIG, d)
        return result


def migrate_devices():
    return modify_config(lambda d: {"ok": True, "count": len(d["devices"])})


def device_list():
    try:
        d = config()
    except FileNotFoundError:
        return []
    # Deliberately whitelist metadata. Never expose server addresses or keys to the UI.
    return [{"id": x["id"], "name": x["name"], "enabled": bool(x.get("enabled", True))} for x in d["devices"]]


def device_name(value):
    name = "".join(c for c in str(value) if c.isprintable()).strip()[:40]
    if not name:
        raise ValueError("invalid_device_name")
    return name


def parse_address(secret):
    import shlex
    secret = str(secret).strip()
    if secret.startswith("curl "):
        secret = next((x for x in shlex.split(secret) if x.startswith("https://")), "")
    u = urllib.parse.urlsplit(secret)
    parts = [p for p in u.path.split("/") if p]
    key = urllib.parse.unquote(parts[0]) if parts else ""
    if u.username or u.password:
        raise ValueError("invalid_address")
    server = urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", ""))
    validate_target(server, key)
    return server, key


def add_device(address, name="iPhone"):
    server, key = parse_address(address)
    name = device_name(name)
    def update(d):
        old = next((x for x in d["devices"] if x["server"].rstrip("/") == server and x["device_key"] == key), None)
        if old:
            old["name"] = name
            return {"ok": True, "id": old["id"], "existing": True}
        if len(d["devices"]) >= 16:
            raise ValueError("device_limit")
        entry = {"id": uuid.uuid4().hex, "name": name, "enabled": True, "server": server, "device_key": key}
        d["devices"].append(entry)
        return {"ok": True, "id": entry["id"], "existing": False}
    return modify_config(update, create=True)


def change_device(device_id, enabled=None, name=None, remove=False):
    if name is not None:
        name = device_name(name)
    def update(d):
        entry = next((x for x in d["devices"] if x["id"] == device_id), None)
        if entry is None:
            raise ValueError("missing_device")
        if remove:
            d["devices"].remove(entry)
        else:
            if enabled is not None:
                entry["enabled"] = (not bool(entry.get("enabled", True))) if enabled == "toggle" else bool(enabled)
            if name is not None: entry["name"] = name
        return {"ok": True}
    return modify_config(update)


def minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("invalid_time")
    h, m = map(int, value.split(":"))
    return h * 60 + m


def rules():
    if not RULES.exists():
        return {"version": 1, "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"}, "routes": {}, "codex_events": CODEX_EVENT_DEFAULTS.copy(), "codex_preview": True, "source_mute_until": {}}
    st = RULES.stat()
    if RULES.is_symlink() or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError("unsafe_rules_permissions")
    data = json.loads(RULES.read_text())
    quiet = data.get("quiet_hours", {})
    if not isinstance(quiet.get("enabled"), bool): raise ValueError("invalid_quiet_switch")
    start, end = minutes(quiet.get("start")), minutes(quiet.get("end"))
    if quiet["enabled"] and start == end: raise ValueError("equal_quiet_times")
    routes = data.get("routes", {})
    if not isinstance(routes, dict) or len(routes) > 256: raise ValueError("invalid_routes")
    clean = {}
    for sid, ids in routes.items():
        if safe_id(sid, None) is None or not isinstance(ids, list) or len(ids) > 16 or any(safe_id(x, None) is None for x in ids): raise ValueError("invalid_route")
        clean[sid] = list(dict.fromkeys(ids))
    events = data.get("codex_events", {})
    if not isinstance(events, dict) or any(k not in CODEX_EVENT_DEFAULTS or not isinstance(v, bool) for k, v in events.items()):
        raise ValueError("invalid_event_preferences")
    preview = data.get("codex_preview", True)
    if not isinstance(preview, bool): raise ValueError("invalid_preview_switch")
    mutes = data.get("source_mute_until", {})
    if not isinstance(mutes, dict) or len(mutes) > 256: raise ValueError("invalid_source_mutes")
    clean_mutes = {}
    for sid, until in mutes.items():
        if safe_id(sid, None) is None or isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(float(until)) or float(until) < 0:
            raise ValueError("invalid_source_mute")
        if float(until) > 0: clean_mutes[sid] = float(until)
    return {"version": 1, "quiet_hours": {k: quiet[k] for k in ("enabled", "start", "end")}, "routes": clean,
            "codex_events": {**CODEX_EVENT_DEFAULTS, **events}, "codex_preview": preview, "source_mute_until": clean_mutes}


def modify_rules(operation):
    private_dir()
    fd = os.open(str(BASE / ".rules.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = rules()
        result = operation(data)
        atomic_json(RULES, data)
        return result


def quiet_now(data=None, now=None):
    quiet = (data or rules())["quiet_hours"]
    if not quiet["enabled"]: return False
    now = now or datetime.datetime.now().astimezone()
    at = now.hour * 60 + now.minute
    start, end = minutes(quiet["start"]), minutes(quiet["end"])
    return start <= at < end if start < end else at >= start or at < end


def set_quiet(data):
    enabled = data.get("enabled")
    if not isinstance(enabled, bool): raise ValueError("invalid_quiet_switch")
    start, end = data.get("start"), data.get("end")
    if minutes(start) == minutes(end) and enabled: raise ValueError("equal_quiet_times")
    def update(current):
        current["quiet_hours"] = {"enabled": enabled, "start": start, "end": end}
        return {"ok": True, "quiet_hours": current["quiet_hours"]}
    return modify_rules(update)


def set_route(source_id, data):
    if not any(x["id"] == source_id for x in source_list()): raise ValueError("unknown_source")
    mode = data.get("mode")
    if mode not in ("all", "selected"): raise ValueError("invalid_route_mode")
    ids = [] if mode == "all" else data.get("device_ids", [])
    known = {x["id"] for x in config()["devices"]}
    if not isinstance(ids, list) or len(ids) > 16 or any(not isinstance(x, str) or x not in known for x in ids): raise ValueError("unknown_device")
    def update(current):
        if mode == "all": current["routes"].pop(source_id, None)
        else: current["routes"][source_id] = list(dict.fromkeys(ids))
        return {"ok": True}
    return modify_rules(update)


PRESET_MODES = {"need", "away", "focus"}
TEMPLATES = {"terminal": ("terminal-task", "终端长任务"), "shortcuts": ("shortcuts", "快捷指令"), "claude": ("claude-code", "Claude Code")}


def preset_state(settings):
    if source_muted_now("codex", settings): return "focus"
    events = settings["codex_events"]
    if all(events.values()): return "away"
    if events == {"permission": True, "question": True, "complete": False}: return "need"
    return "custom"


def apply_preset(mode, seconds=900):
    if mode not in PRESET_MODES: raise ValueError("invalid_preset")
    if mode == "focus" and (isinstance(seconds, bool) or seconds not in (900, 3600)): raise ValueError("invalid_duration")
    def update(current):
        if mode == "focus": current["source_mute_until"]["codex"] = time.time() + seconds
        else:
            current["codex_events"] = {"permission": True, "question": True, "complete": mode == "away"}
            current["source_mute_until"].pop("codex", None)
        return {"ok": True, "preset": preset_state(current)}
    return modify_rules(update)


def enable_template(name):
    if name not in TEMPLATES: raise ValueError("unknown_template")
    source_id, label = TEMPLATES[name]
    def update(entries):
        entry = next((x for x in entries if x["id"] == source_id), None)
        if entry is None:
            entries.append({"id": source_id, "name": label, "type": "application", "enabled": True, "status": "pending"})
        return {"ok": True, "source_id": source_id}
    return modify_sources(update)


def template_event(name, exit_code=0):
    if name not in ("terminal", "shortcuts") or isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise ValueError("invalid_template_event")
    source_id, _ = TEMPLATES[name]
    return {"kind": "application", "source_id": source_id, "origin": "integration", "session": digest([name, os.getpid()]),
            "fingerprint": digest(uuid.uuid4().hex), "task_success": exit_code == 0}


def claude_label(event):
    suffix = {"permission": "等待授权", "question": "等待回答", "complete": "本轮已完成"}.get(event["kind"], "提醒")
    title = "Claude Code · " + suffix
    body = {"permission": "Claude Code 请求操作授权，请返回 Mac 查看并决定。", "question": "Claude Code 正在等待你的回答或选择，请返回 Mac 查看。", "complete": "Claude Code 已结束本轮回复，请返回查看结果。"}[event["kind"]]
    project = preview_text(event.get("project_label"), 80)
    if project: title = preview_text(project + " · Claude · " + suffix, 180)
    if event["kind"] == "question":
        _, body = codex_label({**event, "chat_title": "Claude Code", "chat_id": None}, title, body)
    return title, body


def source_muted_now(source_id, data=None, now=None):
    if not isinstance(source_id, str) or not source_id: return False
    mutes = (data or rules()).get("source_mute_until", {})
    until = float(mutes.get(source_id, 0))
    return until > 0 and (time.time() if now is None else float(now)) < until


def set_source_mute(source_id, seconds):
    if not isinstance(source_id, str) or safe_id(source_id, None) is None:
        raise ValueError("unknown_source")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds not in (0, 900, 3600):
        raise ValueError("invalid_mute_duration")
    def update(current):
        if not any(x["id"] == source_id for x in source_list()): raise ValueError("unknown_source")
        until = float(time.time() + seconds) if seconds > 0 else 0
        if seconds == 0: current["source_mute_until"].pop(source_id, None)
        else: current["source_mute_until"][source_id] = until
        return {"ok": True, "source_id": source_id, "mute_until": until}
    return modify_rules(update)


def policy_status():
    data = rules()
    return {"ok": True, "quiet_hours": data["quiet_hours"], "quiet_now": quiet_now(data), "routes": data["routes"], "codex_events": data["codex_events"], "codex_preview": data["codex_preview"], "source_mute_until": data["source_mute_until"]}


def set_codex_preview(enabled):
    if not isinstance(enabled,bool): raise ValueError("invalid_preview_switch")
    def update(current):
        current["codex_preview"] = enabled
        return {"ok": True, "codex_preview": enabled}
    return modify_rules(update)


def set_codex_events(values):
    if not isinstance(values, dict) or not values or any(k not in CODEX_EVENT_DEFAULTS or not isinstance(v, bool) for k, v in values.items()):
        raise ValueError("invalid_event_preferences")
    def update(current):
        current["codex_events"].update(values)
        return {"ok": True, "codex_events": current["codex_events"]}
    return modify_rules(update)


def history_category(status):
    if status == "accepted": return "success"
    if status in ("partial", "send_failed", "not_configured"): return "failed"
    if status in ("disabled", "source_disabled", "event_disabled", "source_muted", "unknown_source", "no_devices_enabled", "deduplicated", "quiet_hours", "route_no_targets", "unknown_device"): return "skipped"
    return "info"


def history_lines():
    lines = []
    for path in (BASE / "events.previous.jsonl", LOG):
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END); size = file.tell()
                file.seek(max(0, size - 262144)); lines.extend(file.read(262144).splitlines())
        except FileNotFoundError: continue
    return lines


def history_row(line):
    try:
        raw = json.loads(line)
        status = raw.get("status")
        if status not in STATUSES or raw.get("kind") not in KINDS: return None
        at = raw.get("at", "")
        datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
        row = {"at": at, "status": status, "kind": raw["kind"], "source_id": safe_id(raw.get("source_id")), "category": history_category(status)}
        row.update(safe_details(raw))
        return row
    except (ValueError, TypeError, AttributeError): return None


def history(result="all", source_id="all", limit=200):
    if result not in ("all", "success", "failed", "skipped"): raise ValueError("invalid_filter")
    limit = max(1, min(200, int(limit)))
    rows = deque(maxlen=limit)
    for line in history_lines():
        row = history_row(line)
        if row is None: continue
        if result != "all" and row["category"] != result: continue
        if source_id != "all" and row["source_id"] != source_id: continue
        rows.append(row)
    return list(rows)


def latest_event():
    # Match the overview's previous last-200 semantics without decoding the whole log.
    valid = 0
    for line in reversed(history_lines()):
        row = history_row(line)
        if row is None: continue
        valid += 1
        if row["kind"] != "control" and row["status"] != "deduplicated": return row
        if valid >= 200: break
    return None


def digest(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def effective_enabled(data):
    return bool(data.get("enabled")) or (float(data.get("resume_at", 0)) > 0 and time.time() >= float(data["resume_at"]))


def source_list():
    if SOURCES.exists():
        data = json.loads(SOURCES.read_text())
        entries = data["sources"]
        if not isinstance(entries, list):
            raise ValueError("invalid_sources")
    else:
        entries = []
    if not any(x.get("id") == "codex" for x in entries):
        entries.insert(0, {"id": "codex", "name": "Codex", "type": "builtin", "enabled": True, "status": "connected"})
    if not any(x.get("id") == "mac-battery" for x in entries):
        entries.append({"id": "mac-battery", "name": "Mac 电池", "type": "builtin", "enabled": True, "status": "connected"})
    return entries


def modify_sources(operation):
    private_dir()
    fd = os.open(str(BASE / ".sources.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = source_list()
        result = operation(entries)
        atomic_json(SOURCES, {"version": 1, "sources": entries})
        return result


def add_sources(paths):
    apps = []
    for value in paths[:100]:
        p = Path(value).expanduser().resolve(strict=True)
        if not p.is_dir() or p.suffix.lower() != ".app":
            raise ValueError("not_application")
        info_path = p / "Contents/Info.plist"
        if info_path.stat().st_size > 2097152:
            raise ValueError("oversized_app_metadata")
        info = plistlib.loads(info_path.read_bytes())
        bundle = str(info.get("CFBundleIdentifier", ""))
        source_id = bundle if re.fullmatch(r"[A-Za-z0-9._-]{1,200}", bundle) else "app-" + digest(str(p))[:24]
        name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or p.stem)[:80]
        apps.append({"id": source_id, "name": name, "path": str(p), "type": "application", "enabled": True, "status": "pending"})

    def update(entries):
        for app in apps:
            existing = next((x for x in entries if x["id"] == app["id"]), None)
            if existing:
                existing.update({k: app[k] for k in ("name", "path")})
            else:
                entries.append(app)
        return [x["id"] for x in apps]
    return modify_sources(update)


def change_source(source_id, enabled=None, remove=False):
    if remove and source_id == "claude-code" and (BASE / "claude_bark.py").is_file():
        result = subprocess.run([python_executable(), str(BASE / "claude_bark.py"), "detach"], capture_output=True, timeout=8)
        try: detached = json.loads(result.stdout)
        except ValueError: detached = {}
        if detached.get("ok") is not True: raise ValueError("claude_detach_failed")
    rearm = [False]
    def update(entries):
        entry = next((x for x in entries if x["id"] == source_id), None)
        if not entry or (remove and entry.get("type") == "builtin"):
            raise ValueError("protected_or_missing_source")
        if remove:
            entries.remove(entry)
        else:
            was_enabled = bool(entry.get("enabled", True))
            new_enabled = (not was_enabled) if enabled == "toggle" else bool(enabled)
            rearm[0] = source_id == "mac-battery" and not was_enabled and new_enabled
            entry["enabled"] = new_enabled
        return True
    if remove:
        def clean_rules(current):
            result = modify_sources(update)
            current["routes"].pop(source_id, None)
            current["source_mute_until"].pop(source_id, None)
            return result
        result = modify_rules(clean_rules)
    else:
        result = modify_sources(update)
    if rearm[0]: reset_battery_cycle()
    return result


def source_policy(event):
    source_id = event.get("source_id")
    entries = source_list()
    if not source_id and event["kind"] == "battery": source_id = "mac-battery"
    if not source_id and event["kind"] in ("permission", "question", "complete"):
        source_id = "codex"
    if not source_id and event["kind"] in ("application", "source_test"):
        named = next((x for x in entries if x.get("name") == event.get("source")), None)
        source_id = named["id"] if named else None
    if not source_id:
        return "unknown_source" if event["kind"] == "source_test" else None
    source = next((x for x in entries if x["id"] == source_id), None)
    if not source:
        return "unknown_source"
    event["source_id"] = source_id
    event["source"] = source["name"]
    if event.get("origin") == "integration" and source.get("type") != "builtin":
        def observed(items):
            current = next((x for x in items if x["id"] == source_id), None)
            if current:
                current["status"] = "observed"
                current["last_received"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        modify_sources(observed)
    return None if source.get("enabled", True) else "source_disabled"


def event_from_hook(d):
    name = d.get("hook_event_name")
    if name == "PermissionRequest":
        kind = "permission"
        # Hash in memory only; never transmit or persist the actual tool input.
        fingerprint = digest([d.get("turn_id"), d.get("tool_name"), d.get("tool_input")])
    elif name == "Stop":
        # Observe the end of a main assistant turn. Never emit a continuation decision.
        turn = d.get("turn_id")
        if not isinstance(turn, str) or not turn: return None
        kind, fingerprint = "complete", digest(turn)
    elif name == "PreToolUse" and QUESTION_TOOLS.search(str(d.get("tool_name", ""))):
        kind = "question"
        fingerprint = digest([d.get("turn_id"), d.get("tool_input")])
    else:
        return None
    session = str(d.get("session_id") or os.environ.get("CODEX_THREAD_ID") or "local")
    ident = hook_chat_id(d)
    event = {"kind": kind, "session": digest(ident or session), "fingerprint": fingerprint}
    if ident: event["chat_id"] = ident
    if kind == "question": event["questions"] = question_preview(d.get("tool_input"))
    if kind == "permission":
        tool = d.get("tool_name")
        if isinstance(tool,str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}",tool): event["tool"] = tool
    return event


def claim(event):
    private_dir()
    c = sqlite3.connect(str(STATE), timeout=0.15)
    os.chmod(STATE, 0o600)
    c.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at REAL NOT NULL)")
    now = time.time()
    key = digest([event["kind"], event["session"], event["fingerprint"]])
    # Coalesce the skill fallback and tool hook for the same session for 15 seconds.
    question_key = digest(["question_window", event["session"]]) if event["kind"] == "question" else None
    completion_guard = digest(["question_window", event["session"]]) if event["kind"] == "complete" else None
    try:
        c.execute("BEGIN IMMEDIATE")
        for k, ttl in ((key, 120), (question_key, 15), (completion_guard, 15)):
            if k:
                r = c.execute("SELECT at FROM seen WHERE key=?", (k,)).fetchone()
                if r and now - r[0] < ttl:
                    c.rollback()
                    return False
        c.execute("DELETE FROM seen WHERE at < ?", (now - 86400,))
        for k in (key, question_key):
            if k:
                c.execute("INSERT OR REPLACE INTO seen VALUES (?,?)", (k, now))
        c.commit()
        return True
    finally:
        c.close()


def failure_reason(returncode, http, accepted=False):
    if returncode:
        return {5: "dns_error", 6: "dns_error", 7: "connect_error", 28: "timeout", 35: "tls_error", 51: "tls_error", 58: "tls_error", 59: "tls_error", 60: "tls_error", 77: "tls_error"}.get(returncode, "transport_error")
    if http in (401, 403): return "http_auth"
    if http == 404: return "endpoint_missing"
    if http == 429: return "rate_limited"
    if 500 <= http <= 599: return "server_error"
    if 400 <= http <= 499: return "request_rejected"
    return "accepted" if 200 <= http < 300 and accepted else "api_rejected"


def send_target(device, title, body, group, source_id=None):
    try:
        payload = {"device_key": device["device_key"], "title": title, "body": body, "group": group, "isArchive": "0"}
        # Codex reminders remain in phone Bark history, including generic previews.
        # Preserve the existing archive policy for every other source/legacy caller.
        if source_id == "codex": payload["isArchive"] = "1"
        icon = SOURCE_ICONS.get(source_id)
        if icon: payload["icon"] = icon
        result = subprocess.run([
            "/usr/bin/curl", "--silent", "--show-error", "--connect-timeout", "4",
            "--max-time", "8", "--max-filesize", "32768", "--retry", "0",
            "--proto", "=https", "--request", "POST", "--header", "Content-Type: application/json",
            "--data-binary", "@-", "--write-out", "\n%{http_code}",
            "--url", device["server"].rstrip("/") + "/push"
        ], input=json.dumps(payload, ensure_ascii=False).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=9)
        data, _, code = result.stdout.rpartition(b"\n")
        http = int(code) if code.isdigit() else 0
        try: parsed = json.loads(data)
        except ValueError: parsed = None
        accepted = isinstance(parsed, dict) and parsed.get("code") == 200
        reason = failure_reason(result.returncode, http, accepted)
        if result.returncode == 0 and 200 <= http < 300 and not isinstance(parsed, dict): reason = "invalid_response"
        return reason == "accepted", http, reason
    except subprocess.TimeoutExpired: return False, 0, "timeout"
    except FileNotFoundError: return False, 0, "sender_unavailable"
    except Exception: return False, 0, "unknown_error"


def deliver(event, force=False, device_id=None, report=None):
    kind = event["kind"]
    source_id = "system" if kind in ("test", "control") else "unknown"
    def finish(status, **detail):
        if kind == "battery":
            detail["battery_percent"] = event.get("battery_percent")
            detail["battery_level"] = event.get("battery_level")
        detail.setdefault("reason", status if status in REASONS else "unknown_error")
        detail["source_id"] = source_id
        clean = safe_details(detail)
        log(status, kind, **clean)
        if report is not None: report.update({"result": status, **clean})
        return status
    try:
        policy = source_policy(event)
        known = {x["id"] for x in source_list()}
        if event.get("source_id") in known: source_id = event["source_id"]
        if policy: return finish(policy)
        cfg = config()
        if not cfg["devices"]: return finish("not_configured")
        if not effective_enabled(cfg): return finish("disabled")
        try: settings = rules()
        except (ValueError, OSError): return finish("send_failed", reason="invalid_rules")
        if source_id == "codex" and kind in CODEX_EVENT_DEFAULTS and not settings["codex_events"][kind]:
            return finish("event_disabled")
        if kind in ("permission", "question", "complete", "application", "battery") and source_muted_now(source_id, settings):
            return finish("source_muted")
        if kind in ("permission", "question", "complete", "application", "battery") and quiet_now(settings): return finish("quiet_hours")
        devices = [x for x in cfg["devices"] if x.get("enabled", True) and (device_id is None or x["id"] == device_id)]
        if device_id is not None and not any(x["id"] == device_id for x in cfg["devices"]): return finish("unknown_device")
        route = settings["routes"].get(source_id)
        if device_id is None and route is not None:
            devices = [x for x in devices if x["id"] in route]
            if not devices: return finish("route_no_targets", total=0, delivered=0)
        if not devices: return finish("no_devices_enabled")
        if not force and not claim(event): return finish("deduplicated")
        labels = {
            "complete": ("Codex 本轮已完成", "Mac 上的 Codex 已结束本轮回复，请返回查看结果。"),
            "battery": ("Mac 电量偏低", "Mac 电量剩余 " + str(max(0, min(100, int(event.get("battery_percent", 0))))) + "% ，请接上电源，避免工作流中断。"),
            "permission": ("Codex 等待授权", "Mac 上的 Codex 请求操作授权，请返回查看并决定。"),
            "question": ("Codex 等待回答", "Mac 上的 Codex 正在等待你的回答或选择，请返回查看。"),
            "test": ("Bark 推送测试", "这台设备已接入 Mac 推送，请确认收到这条通知。"),
            "application": ("Mac 应用提醒", str(event.get("source", "Mac 应用")) + " 需要你处理，请返回 Mac 查看。"),
            "source_test": (str(event.get("source", "应用")) + " · 测试提醒", "此条为发送链路测试，不代表应用的自动事件已配置完成。"),
        }
        title, body = labels[kind]
        if source_id == "codex" and settings["codex_preview"]:
            title, body = codex_label(event, title, body)
        if source_id == "claude-code" and kind in ("permission", "question", "complete"):
            if settings["codex_preview"]: title, body = claude_label(event)
            else: title, body = claude_label({"kind": kind})
        if kind == "application" and source_id in ("terminal-task", "shortcuts"):
            success = event.get("task_success", True)
            title = ("终端任务" if source_id == "terminal-task" else "快捷指令") + (" · 已结束" if success else " · 执行失败")
            body = "你指定的任务已结束，请返回 Mac 查看结果。" if success else "你指定的任务以非零状态退出，请返回 Mac 检查。"
        group = str(event.get("source", "Mac 应用")) if kind in ("application", "source_test", "battery") else "Codex"
        if source_id == "claude-code": group = "Claude Code"
        if len(devices) == 1: results = [send_target(devices[0], title, body, group, source_id)]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(devices))) as pool:
                results = list(pool.map(lambda x: send_target(x, title, body, group, source_id), devices))
        # The two-field form remains accepted for existing local integrations/tests.
        results = [(x[0], x[1], x[2] if len(x) > 2 else failure_reason(0, x[1], x[0])) for x in results]
        accepted = sum(bool(x[0]) for x in results)
        status = "accepted" if accepted == len(devices) else ("partial" if accepted else "send_failed")
        reasons = {}
        for _, _, reason in results: reasons[reason] = reasons.get(reason, 0) + 1
        reason = "accepted" if status == "accepted" else (next(iter(reasons)) if len(reasons) == 1 else "multiple_failures")
        detail = {"delivered": accepted, "total": len(devices), "reason": reason, "reasons": reasons,
                  "device_results": [{"device_id": device["id"], "status": "accepted" if result[0] else "send_failed", "reason": result[2], "http": result[1]} for device, result in zip(devices, results)]}
        if kind == "battery":
            detail["battery_percent"] = int(event.get("battery_percent", 0))
            detail["battery_level"] = int(event.get("battery_level", 20))
        if len(results) == 1: detail["http"] = results[0][1]
        return finish(status, **detail)
    except FileNotFoundError: return finish("not_configured")
    except ValueError: return finish("send_failed", reason="local_config_error")
    except Exception: return finish("send_failed", reason="unknown_error")


def diagnose(device_id=None):
    report = {}
    status = deliver({"kind": "test", "session": "diagnostic", "fingerprint": "diagnostic"}, force=True, device_id=device_id, report=report)
    return {"ok": status == "accepted", **report}


def battery_db():
    private_dir()
    conn = sqlite3.connect(str(STATE), timeout=0.15)
    os.chmod(STATE, 0o600)
    conn.execute("CREATE TABLE IF NOT EXISTS battery_cycle (id INTEGER PRIMARY KEY, cycle TEXT NOT NULL, low INTEGER NOT NULL, critical INTEGER NOT NULL, on_ac INTEGER NOT NULL)")
    return conn


def reset_battery_cycle():
    conn = battery_db()
    try:
        conn.execute("DELETE FROM battery_cycle")
        conn.commit()
    finally: conn.close()


def battery_event(percent, on_ac):
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100 or not isinstance(on_ac, bool):
        raise ValueError("invalid_battery_reading")
    source = next(x for x in source_list() if x["id"] == "mac-battery")
    if not source.get("enabled", True): return None
    conn = battery_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT cycle, low, critical, on_ac FROM battery_cycle WHERE id=1").fetchone()
        cycle, low, critical, was_ac = row or (uuid.uuid4().hex, 0, 0, 1)
        level = None
        if on_ac or percent > 25:
            if low or critical or not was_ac: cycle = uuid.uuid4().hex
            low, critical = 0, 0
        else:
            if was_ac: cycle, low, critical = uuid.uuid4().hex, 0, 0
            if percent <= 10 and not critical: level, low, critical = 10, 1, 1
            elif percent <= 20 and not low: level, low = 20, 1
        conn.execute("INSERT OR REPLACE INTO battery_cycle VALUES (1,?,?,?,?)", (cycle, low, critical, int(on_ac)))
        conn.commit()
        if level is None: return None
        return {"kind": "battery", "source_id": "mac-battery", "session": digest(cycle), "fingerprint": digest([cycle, level]), "battery_percent": percent, "battery_level": level}
    finally: conn.close()


def dispatch(event):
    if event is None:
        return
    # Detach network work even for a direct skill invocation. No network wait in hooks.
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    p.stdin.write(json.dumps(event, ensure_ascii=False).encode())
    p.stdin.close()


def configure():
    private_dir()
    print("只在此 Mac 的终端填写；不要把 Bark 地址或密钥发到聊天。")
    print("在 iPhone 的 Bark 首页复制推送地址（例如 https://api.day.app/你的密钥/）。")
    secret = getpass.getpass("粘贴完整 Bark 地址，或只粘贴 device key（输入隐藏）：").strip()
    if not secret:
        print("未填写，配置未改动。")
        return
    if "://" in secret:
        u = urllib.parse.urlsplit(secret)
        parts = [p for p in u.path.split("/") if p]
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or not parts:
            print("格式无效：请使用 Bark 首页的 HTTPS 推送地址，不要带查询参数。")
            return
        # Standard Bark URL: the first path segment is the device key.
        key = urllib.parse.unquote(parts[0])
        server = urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", ""))
    else:
        key = secret
        server = input("Bark 服务器（回车使用 https://api.day.app）：").strip() or "https://api.day.app"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", key):
        print("密钥格式无效，未保存。")
        return
    u = urllib.parse.urlsplit(server)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        print("服务器必须是 HTTPS 地址，未保存。")
        return
    # No backup containing the key; replace atomically with mode 0600.
    saved = add_device(server.rstrip("/") + "/" + key, "iPhone")
    print("已保存到本机受限权限文件；不会显示密钥。正在发送测试通知……")
    print_test(deliver({"kind": "test", "session": "test", "fingerprint": "test"}, force=True, device_id=saved["id"]))


def print_test(result):
    messages = {
        "accepted": "Bark 服务器已接受测试推送；请在 iPhone 上确认收到。",
        "not_configured": "尚未填写 Bark 密钥。请先运行 Configure Bark.command。",
        "send_failed": "测试推送失败。配置仍已保存在本机；检查网络、服务器和密钥后重试。",
        "partial": "部分设备已被 Bark 服务器接受，其他设备发送失败，请查看记录。",
        "no_devices_enabled": "尚未启用接收设备，请在接收设备列表开启。",
        "disabled": "推送已暂停，请在 Codex Bark 菜单栏中开启后重试。",
    }
    print(messages.get(result, result))


def source_metadata(entries, mutes=None):
    mutes = mutes if isinstance(mutes, dict) else {}
    rows = []
    for entry in entries:
        if not isinstance(entry, dict) or safe_id(entry.get("id"), None) is None or not isinstance(entry.get("name"), str):
            raise ValueError("invalid_source_metadata")
        raw_mute = mutes.get(entry["id"], 0)
        mute_until = float(raw_mute) if isinstance(raw_mute, (int, float)) and not isinstance(raw_mute, bool) and math.isfinite(float(raw_mute)) and float(raw_mute) > 0 else 0
        row = {"id": entry["id"], "name": entry["name"][:80], "type": "builtin" if entry.get("type") == "builtin" else "application",
               "enabled": bool(entry.get("enabled", True)), "status": entry.get("status") if entry.get("status") in ("connected", "observed", "pending") else "pending",
               "mute_until": mute_until}
        if isinstance(entry.get("path"), str): row["path"] = entry["path"]
        rows.append(row)
    return rows


def snapshot(view="panel"):
    if view not in ("menu", "panel", "history"): raise ValueError("invalid_snapshot_view")
    errors = {}
    cfg, raw_sources, sources, policy, mutes = None, None, [], {"ok": False}, {}
    enabled, resume_at = False, 0
    try:
        cfg = config()
        deadline = float(cfg.get("resume_at", 0))
        if not math.isfinite(deadline): raise ValueError("invalid_resume_time")
        enabled = effective_enabled(cfg)
        resume_at = deadline if not enabled else 0
    except FileNotFoundError: errors["config"] = "not_configured"
    except Exception:
        errors["config"] = "local_config_error"; cfg = None; enabled = False; resume_at = 0
    try: raw_sources = source_list()
    except Exception: errors["sources"] = "local_sources_error"
    try:
        data = rules()
        mutes = data["source_mute_until"]
        policy = {"ok": True, "preset": preset_state(data), "quiet_hours": data["quiet_hours"], "quiet_now": quiet_now(data), "routes": data["routes"], "codex_events": data["codex_events"], "codex_preview": data["codex_preview"], "source_mute_until": mutes}
    except Exception: errors["policy"] = "invalid_rules"
    if raw_sources is not None:
        try: sources = source_metadata(raw_sources, mutes)
        except Exception: errors["sources"] = "local_sources_error"; sources = []
    devices = [] if cfg is None else [{"id": x["id"], "name": x["name"], "enabled": bool(x.get("enabled", True))} for x in cfg["devices"]]
    battery_enabled = next((x["enabled"] for x in sources if x["id"] == "mac-battery"), False)
    status_data = {"preset": policy.get("preset", "custom"), "configured": bool(devices), "enabled": enabled, "battery_enabled": battery_enabled,
                   "quiet_now": policy.get("quiet_now", False), "resume_at": resume_at,
                   "device_count": len(devices), "active_device_count": sum(x["enabled"] for x in devices),
                   "config_mode": oct(stat.S_IMODE(CONFIG.stat().st_mode)) if CONFIG.exists() else None,
                   "base_mode": oct(stat.S_IMODE(BASE.stat().st_mode)), "config_file": str(CONFIG)}
    result = {"ok": True, "version": 1, "status": status_data, "devices": devices, "sources": sources, "errors": errors}
    if view != "menu":
        result["policy"] = policy
        try:
            if view == "history":
                result["events"] = history()
                result["latest_event"] = next((x for x in reversed(result["events"]) if x["kind"] != "control" and x["status"] != "deduplicated"), None)
            else: result["latest_event"] = latest_event()
        except Exception:
            errors["history"] = "history_unavailable"
            result["latest_event"] = None
            if view == "history": result["events"] = []
    return result


def status():
    print(json.dumps(snapshot("menu")["status"], ensure_ascii=False))


def set_enabled(value, emit=True):
    private_dir()
    fd = os.open(str(BASE / ".config.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = config()
        enabled = (not effective_enabled(d)) if value == "toggle" else bool(value)
        d["enabled"] = enabled
        d.pop("resume_at", None)
        atomic_json(CONFIG, d)
    log("enabled" if enabled else "disabled", "control")
    result = {"ok": True, "enabled": enabled}
    if emit: print(json.dumps(result))
    return result


def pause_for(seconds):
    if not 1 <= seconds <= 604800:
        raise ValueError("invalid_pause_duration")
    private_dir()
    fd = os.open(str(BASE / ".config.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = config()
        d["enabled"] = False
        d["resume_at"] = time.time() + seconds
        atomic_json(CONFIG, d)
    log("disabled", "control")
    return {"ok": True, "resume_at": d["resume_at"]}


def configure_json():
    data = json.loads(sys.stdin.buffer.read(8192))
    saved = add_device(data.get("address", ""), data.get("name", "iPhone"))
    result = deliver({"kind": "test", "session": "test", "fingerprint": "test"}, force=True, device_id=saved["id"])
    print(json.dumps({"ok": True, "result": result}))


def main():
    parser = argparse.ArgumentParser(description="Codex Bark local notifier (never prints secrets)")
    parser.add_argument("action", choices=["hook", "wait", "notify", "test", "configure", "status", "snapshot", "set-enabled", "pause-for", "configure-json", "sources-list", "source-add", "source-toggle", "source-remove", "source-test", "source-mute", "devices-migrate", "devices-list", "device-add", "device-toggle", "device-rename", "device-remove", "device-test", "test-json", "policy-get", "quiet-set", "route-set", "events-set", "preview-set", "diagnose", "history", "battery-event", "preset-set", "template-enable", "template-notify", "_worker"])
    parser.add_argument("value", nargs="?", choices=["on", "off", "toggle"])
    parser.add_argument("--session", default="")
    parser.add_argument("--preview-json", action="store_true")
    parser.add_argument("--id", default="")
    parser.add_argument("--source", default="Mac 应用")
    parser.add_argument("--source-id", default="")
    parser.add_argument("--source-enabled", choices=["on", "off", "toggle"])
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("--device-id", default="")
    parser.add_argument("--device-enabled", choices=["on", "off", "toggle"])
    parser.add_argument("--history-result", choices=["all", "success", "failed", "skipped"], default="all")
    parser.add_argument("--history-source", default="all")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--view", choices=["menu", "panel", "history"], default="panel")
    parser.add_argument("--with-snapshot", action="store_true")
    parser.add_argument("--preset", choices=["need", "away", "focus"])
    parser.add_argument("--template", choices=["terminal", "shortcuts", "claude"])
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args()

    def respond(payload):
        if args.with_snapshot:
            payload = {**payload, "snapshot": snapshot(args.view)}
        print(json.dumps(payload, ensure_ascii=False))

    if args.action == "preset-set":
        respond(apply_preset(args.preset, args.seconds or 900))
    elif args.action == "template-enable":
        respond(enable_template(args.template))
    elif args.action == "template-notify":
        dispatch(template_event(args.template, args.exit_code))
    elif args.action == "snapshot":
        print(json.dumps(snapshot(args.view), ensure_ascii=False))
    elif args.action == "battery-event":
        data = json.loads(sys.stdin.buffer.read(1024))
        event = battery_event(data.get("percent"), data.get("on_ac"))
        if event: dispatch(event)
    elif args.action == "policy-get":
        print(json.dumps(policy_status()))
    elif args.action == "quiet-set":
        respond(set_quiet(json.loads(sys.stdin.buffer.read(4096))))
    elif args.action == "events-set":
        respond(set_codex_events(json.loads(sys.stdin.buffer.read(4096))))
    elif args.action == "preview-set":
        if args.value not in ("on", "off"): raise ValueError("missing_switch_value")
        respond(set_codex_preview(args.value == "on"))
    elif args.action == "route-set":
        respond(set_route(args.source_id, json.loads(sys.stdin.buffer.read(4096))))
    elif args.action == "source-mute":
        respond(set_source_mute(args.source_id, args.seconds))
    elif args.action == "diagnose":
        print(json.dumps(diagnose(args.device_id or None)))
    elif args.action == "history":
        print(json.dumps({"ok": True, "events": history(args.history_result, args.history_source, args.limit)}, ensure_ascii=False))
    elif args.action == "devices-migrate":
        print(json.dumps(migrate_devices()))
    elif args.action == "devices-list":
        print(json.dumps({"ok": True, "devices": device_list()}, ensure_ascii=False))
    elif args.action == "device-add":
        data = json.loads(sys.stdin.buffer.read(8192))
        respond(add_device(data.get("address", ""), data.get("name", "iPhone")))
    elif args.action in ("device-toggle", "device-rename", "device-remove"):
        if args.action == "device-toggle":
            if args.device_enabled is None: raise ValueError("missing_device_switch")
            mode = "toggle" if args.device_enabled == "toggle" else (args.device_enabled == "on")
            result = change_device(args.device_id, enabled=mode)
        elif args.action == "device-rename":
            data = json.loads(sys.stdin.buffer.read(4096))
            result = change_device(args.device_id, name=data.get("name", ""))
        else:
            result = change_device(args.device_id, remove=True)
        respond(result)
    elif args.action in ("device-test", "test-json"):
        report = {}
        result = deliver({"kind": "test", "session": "test", "fingerprint": "test"}, force=True,
                         device_id=args.device_id if args.action == "device-test" else None, report=report)
        print(json.dumps({"ok": result == "accepted", **report}))
    elif args.action == "pause-for":
        respond(pause_for(args.seconds))
    elif args.action == "sources-list":
        print(json.dumps({"ok": True, "sources": source_list()}, ensure_ascii=False))
    elif args.action == "source-add":
        data = json.loads(sys.stdin.buffer.read(65536))
        added = add_sources(data.get("paths", []))
        respond({"ok": True, "added": added})
    elif args.action in ("source-toggle", "source-remove"):
        if args.action == "source-toggle" and args.source_enabled is None:
            raise ValueError("missing_source_switch")
        mode = "toggle" if args.source_enabled == "toggle" else (args.source_enabled == "on")
        change_source(args.source_id, enabled=mode, remove=args.action == "source-remove")
        respond({"ok": True})
    elif args.action == "source-test":
        report = {}
        result = deliver({"kind": "source_test", "source_id": args.source_id, "origin": "manual_test", "session": "test", "fingerprint": "test"}, force=True, report=report)
        print(json.dumps({"ok": result == "accepted", **report}))
    elif args.action == "configure":
        configure()
    elif args.action == "configure-json":
        configure_json()
    elif args.action == "set-enabled":
        if args.value not in ("on", "off", "toggle"):
            raise ValueError("missing_switch_value")
        mode = "toggle" if args.value == "toggle" else (args.value == "on")
        respond(set_enabled(mode, emit=False))
    elif args.action == "status":
        status()
    elif args.action == "test":
        print_test(deliver({"kind": "test", "session": "test", "fingerprint": "test"}, force=True))
    elif args.action == "wait":
        session = args.session or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or "local"
        event = {"kind": "question", "session": digest(session), "fingerprint": digest(args.id or str(time.time_ns()))}
        ident = chat_id(session)
        if ident: event["chat_id"] = ident
        if args.preview_json:
            try:
                data = json.loads(read_preview_input())
                event["questions"] = question_preview(data)
                if isinstance(data, dict):
                    title = preview_text(data.get("chat_title"),100)
                    if title: event["chat_title"] = title
            except (ValueError, TypeError): pass
        dispatch(event)
    elif args.action == "notify":
        source = "".join(c for c in args.source if c.isprintable()).strip()[:40] or "Mac 应用"
        event = {"kind": "application", "source": source, "origin": "integration", "session": digest(args.source_id or source),
                 "fingerprint": digest(args.id or str(time.time_ns()))}
        if args.source_id:
            event["source_id"] = args.source_id
        dispatch(event)
    elif args.action == "_worker":
        deliver(json.loads(sys.stdin.buffer.read(4096)))
    elif args.action == "hook":
        data = json.loads(sys.stdin.buffer.read(1048576))
        if data.get("hook_event_name") == "SessionStart":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CONTEXT}}))
        else:
            dispatch(event_from_hook(data))
            if data.get("hook_event_name") == "Stop": print("{}")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (Exception, KeyboardInterrupt):
        # Notifications are advisory. Never emit a hook policy decision or exit 2.
        if len(sys.argv) > 1 and sys.argv[1] in ("preset-set", "template-enable", "snapshot", "policy-get", "quiet-set", "route-set", "events-set", "preview-set", "source-mute", "diagnose", "history", "set-enabled", "pause-for", "sources-list", "source-add", "source-toggle", "source-remove", "source-test", "configure-json", "devices-migrate", "devices-list", "device-add", "device-toggle", "device-rename", "device-remove", "device-test", "test-json"):
            print(json.dumps({"ok": False, "error": "operation_failed"}))
