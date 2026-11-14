#!/usr/bin/env python3
"""Explicit, clean first install. Dry run unless --apply is supplied."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APP = Path('/Applications/Bark.app')
BASE = Path.home() / 'Library/Application Support/CodexBark'


def install(app, base, codex_home=None, with_cc_switch=False):
    if app.exists() or app.is_symlink() or base.exists() or base.is_symlink():
        raise RuntimeError('An app or runtime already exists. This prerelease installer supports clean first installs only; nothing was overwritten.')
    built = ROOT / 'build/Bark.app'
    info = plistlib.loads((built / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'local.codex.bark.menu':
        raise RuntimeError('Build output identity mismatch.')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(built)], check=True)
    if not os.access(app.parent, os.W_OK):
        raise RuntimeError('No write access to Applications. Run from a Mac user that can install apps; no sudo is invoked automatically.')
    try:
        base.mkdir(parents=True, mode=0o700)
        base.chmod(0o700)
        for name in ['bark.py', 'manage.py', 'claude_bark.py', 'bark-run']:
            shutil.copy2(ROOT / 'src/runtime' / name, base / name)
            (base / name).chmod(0o700)
        (base / 'skill').mkdir(mode=0o700)
        skill = (ROOT / 'assets/SKILL.md.in').read_text().replace('@BARK_BASE@', str(base))
        (base / 'skill/SKILL.md').write_text(skill)
        (base / 'skill/SKILL.md').chmod(0o600)
        subprocess.run(['/usr/bin/ditto', str(built), str(app)], check=True)
        manifest = {'homes': {}, 'backups': [], 'menu_app': {
            'path': str(app), 'login_plist': str(Path.home() / 'Library/LaunchAgents/local.codex.bark.menu.plist'),
            'version': info['CFBundleShortVersionString']}}
        (base / 'installation.json').write_text(json.dumps(manifest, indent=2)+'\n')
        (base / 'installation.json').chmod(0o600)
        if codex_home:
            command = ['/usr/bin/python3', str(base / 'manage.py'), 'attach', str(codex_home)]
            if with_cc_switch: command.append('--with-cc-switch')
            subprocess.run(command, check=True)
    except Exception:
        # manage.attach restores original hook/config files on failure.
        # Preserve failed-install backups for review instead of deleting them.
        if app.is_dir() and (app / 'Contents/Info.plist').is_file():
            installed = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            if installed.get('CFBundleIdentifier') == 'local.codex.bark.menu': shutil.rmtree(app)
        raise
    return {'app': str(app), 'runtime': str(base), 'codex_attached': bool(codex_home)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--with-cc-switch', action='store_true')
    args = parser.parse_args()
    if args.with_cc_switch and not args.codex_home:
        parser.error('--with-cc-switch requires --codex-home')
    if not args.apply:
        print(json.dumps({'dry_run': True, 'app': str(APP), 'runtime': str(BASE),
                          'codex_home': str(args.codex_home) if args.codex_home else None,
                          'cc_switch_opt_in': args.with_cc_switch}, ensure_ascii=False))
        return
    print(json.dumps(install(APP, BASE, args.codex_home, args.with_cc_switch), ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('Install stopped: '+str(error), file=sys.stderr)
        raise SystemExit(1)
