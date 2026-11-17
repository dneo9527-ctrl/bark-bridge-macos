#!/usr/bin/env python3
"""Audit only the allowlisted release sources. Never opens installed data."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md', 'VERSION', 'LICENSE', '.gitignore', 'release-check.json'}
DIRS = {'src', 'assets', 'scripts', 'tests', 'docs', '.github'}
DENIED_NAMES = {'config.json', 'rules.json', 'sources.json', 'installation.json', 'claude_installation.json'}


def source_files():
    for path in sorted(ROOT.rglob('*')):
        rel = path.relative_to(ROOT)
        if not path.is_file() and not path.is_symlink(): continue
        if any(part in {'.git', 'build', 'dist', '__pycache__'} for part in rel.parts): continue
        if rel.parts[0] not in DIRS and str(rel) not in TOP: raise ValueError('Unexpected release file: '+str(rel))
        if path.is_symlink(): raise ValueError('Release symlink: '+str(rel))
        if path.name in DENIED_NAMES or path.suffix in {'.sqlite', '.jsonl', '.pem', '.key'} or '.sqlite-' in path.name:
            raise ValueError('Private-state filename: '+str(rel))
        yield path


def audit():
    checked = []
    for path in source_files():
        rel = str(path.relative_to(ROOT))
        if path.suffix == '.png':
            if rel not in {'docs/media/app-icon.png', 'docs/media/codex-source-icon.png'} or path.stat().st_size > 524288 or not path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('Unapproved binary image: '+rel)
            if rel == 'docs/media/codex-source-icon.png' and hashlib.sha256(path.read_bytes()).hexdigest() != 'de7d43f3386105ab20952958c2c25beb0d903e2aeb6e1aef57c49a648c0d1c07':
                raise ValueError('Codex artwork digest mismatch: '+rel)
        elif path.suffix != '.icns':
            text = path.read_text()
            if '/Users/'+'m/' in text or '/Applications/'+'学习/' in text:
                raise ValueError('Machine-specific path: '+rel)
            for key in re.findall(r'https://api[.]day[.]app/([A-Za-z0-9_-]{12,})', text):
                if not key.startswith(('FAKE_', 'LOCAL_TEST_')): raise ValueError('Possible live device URL: '+rel)
            if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}', text): raise ValueError('Possible credential: '+rel)
        checked.append(rel)
    return {'ok': True, 'allowlisted_files': len(checked), 'installed_config_read': False,
            'scope': 'source allowlist, private-state filenames, known personal paths, device URLs and common GitHub credentials; not a proof against every possible secret'}


if __name__ == '__main__':
    print(json.dumps(audit(), ensure_ascii=False))
