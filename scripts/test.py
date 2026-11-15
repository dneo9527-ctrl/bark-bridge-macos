#!/usr/bin/env python3
"""Run isolated tests using this checkout, with fake device credentials."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = dict(os.environ, BARK_TEST_SOURCE=str(root/'src/runtime'), PYTHONPATH=str(root/'tests'), PYTHONDONTWRITEBYTECODE='1')
modules = ['test_codex_bark', 'test_abcd', 'test_battery', 'test_completion', 'test_snapshot',
           'test_health', 'test_event_preferences', 'test_chat_preview', 'test_transport_tolerance',
           'test_abd_backend', 'test_support_report', 'test_claude_adapter', 'test_builtins',
           'test_template_flow', 'test_chat_identity.IdentityTests', 'test_release', 'test_updater']
raise SystemExit(subprocess.call([sys.executable, '-B', '-m', 'unittest', *modules, '-v'], cwd=root, env=env))
