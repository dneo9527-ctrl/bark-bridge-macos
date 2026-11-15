#!/usr/bin/env python3
"""Explicit, clean first install. Dry run unless --apply is supplied."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = Path('/Applications/Bark.app')
BASE = Path.home() / 'Library/Application Support/CodexBark'


def install(app, base, codex_home=None, with_cc_switch=False, payload=None):
    if app.exists() or app.is_symlink() or base.exists() or base.is_symlink():
        raise RuntimeError('An app or runtime already exists. This prerelease installer supports clean first installs only; nothing was overwritten.')
    payload = Path(payload) if payload else None
    built = payload / 'Bark.app' if payload else ROOT / 'build/Bark.app'
    runtime = payload / 'runtime' if payload else ROOT / 'src/runtime'
    template = payload / 'SKILL.md.in' if payload else ROOT / 'assets/SKILL.md.in'
    info = plistlib.loads((built / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'local.codex.bark.menu':
        raise RuntimeError('Build output identity mismatch.')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(built)], check=True)
    if not os.access(app.parent, os.W_OK):
        raise RuntimeError('No write access to Applications. Run from a Mac user that can install apps; no sudo is invoked automatically.')
    # Claim this first-install directory exclusively. Never remove an existing app.
    base.mkdir(parents=True, mode=0o700)
    base.chmod(0o700)
    app_created = False
    app_identity = None
    try:
        for name in ['bark.py', 'manage.py', 'claude_bark.py', 'bark-run']:
            shutil.copy2(runtime / name, base / name)
            (base / name).chmod(0o700)
        if payload:
            shutil.copytree(payload / 'python', base / 'python', symlinks=True)
        python = base / 'python/bin/python3' if payload else Path('/usr/bin/python3')
        (base / 'skill').mkdir(mode=0o700)
        skill = template.read_text().replace('@BARK_BASE@', str(base)).replace('@BARK_PYTHON@', shlex.quote(str(python)))
        (base / 'skill/SKILL.md').write_text(skill)
        (base / 'skill/SKILL.md').chmod(0o600)
        # mkdir without exist_ok closes the normal concurrent-install overwrite case.
        app.mkdir()
        app_created = True
        app_identity = (app.stat().st_dev, app.stat().st_ino)
        subprocess.run(['/usr/bin/ditto', str(built), str(app)], check=True)
        manifest = {'homes': {}, 'backups': [], 'menu_app': {
            'path': str(app), 'login_plist': str(Path.home() / 'Library/LaunchAgents/local.codex.bark.menu.plist'),
            'version': info['CFBundleShortVersionString']}}
        (base / 'installation.json').write_text(json.dumps(manifest, indent=2)+'\n')
        (base / 'installation.json').chmod(0o600)
        if codex_home:
            command = [str(python), str(base / 'manage.py'), 'attach', str(codex_home)]
            if with_cc_switch: command.append('--with-cc-switch')
            subprocess.run(command, check=True)
    except Exception:
        # manage.attach restores original hook/config files on failure.
        # Preserve failed-install backups for review instead of deleting them.
        if app_created and app.is_dir() and not app.is_symlink():
            current = app.stat()
            if (current.st_dev, current.st_ino) == app_identity: shutil.rmtree(app)
        raise
    return {'app': str(app), 'runtime': str(base), 'codex_attached': bool(codex_home)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--with-cc-switch', action='store_true')
    parser.add_argument('--payload', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--json-result', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.with_cc_switch and not args.codex_home:
        parser.error('--with-cc-switch requires --codex-home')
    if not args.apply:
        print(json.dumps({'dry_run': True, 'app': str(APP), 'runtime': str(BASE),
                          'codex_home': str(args.codex_home) if args.codex_home else None,
                          'cc_switch_opt_in': args.with_cc_switch}, ensure_ascii=False))
        return
    try:
        print(json.dumps({'ok': True, **install(APP, BASE, args.codex_home, args.with_cc_switch, args.payload)}, ensure_ascii=False))
    except Exception:
        if not args.json_result:
            raise
        # GUI output never includes user config, raw exceptions or Hook contents.
        print(json.dumps({'ok': False, 'error': 'installation_stopped'}))
        raise SystemExit(1)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('Install stopped: '+str(error), file=sys.stderr)
        raise SystemExit(1)
