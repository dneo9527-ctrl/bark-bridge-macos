#!/usr/bin/env python3
"""Package a clean, ad-hoc signed arm64 installer. Never reads installed data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tarfile
from release_metadata import read_metadata, verify_app_identity

ROOT = Path(__file__).resolve().parents[1]
MACHO = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def extract_python(archive, destination):
    lock = json.loads((ROOT / 'assets/python-runtime.json').read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock['sha256']:
        raise ValueError('Python archive checksum does not match the pinned runtime.')
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.isdev() or member.isfifo():
                raise ValueError('Unsafe Python archive path or type.')
            if member.issym() and not (target.parent / member.linkname).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe Python archive symlink.')
            if member.islnk() and not (destination / member.linkname).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe Python archive hardlink.')
        source.extractall(destination)


def sign(path):
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-archive', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.uname().machine != 'arm64':
        raise SystemExit('This binary distribution is built and verified on Apple Silicon only.')
    metadata = read_metadata()
    subprocess.run([sys.executable, str(ROOT / 'scripts/audit.py')], check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/build.py'), '--arch', 'arm64'], check=True)
    verify_app_identity(ROOT / 'build/Bark.app', metadata)
    version = metadata['version']
    suffix = '-preview' if metadata['channel'] == 'preview' else ''
    folder = ROOT / 'dist' / ('Bark-Bridge-' + version + '-macOS-arm64' + suffix)
    if folder.exists():
        raise SystemExit('Package folder exists; preserve or remove the previous build explicitly.')
    folder.mkdir(parents=True)
    installer = folder / 'Install Bark Bridge.app'
    resources = installer / 'Contents/Resources'
    binaries = installer / 'Contents/MacOS'
    binaries.mkdir(parents=True); resources.mkdir()
    payload = resources / 'Payload'; payload.mkdir()
    shutil.copytree(ROOT / 'build/Bark.app', payload / 'Bark.app', symlinks=True)
    (payload / 'runtime').mkdir()
    for name in ['bark.py', 'manage.py', 'claude_bark.py', 'bark-run', 'updater.py']:
        shutil.copy2(ROOT / 'src/runtime' / name, payload / 'runtime' / name)
    shutil.copy2(ROOT / 'assets/SKILL.md.in', payload / 'SKILL.md.in')
    shutil.copy2(ROOT / 'scripts/install.py', payload / 'install.py')
    extract_python(args.python_archive, payload)
    shutil.copytree(ROOT / 'assets/python-licenses', payload / 'python/LICENSES')
    shutil.copy2(ROOT / 'assets/python-runtime.json', payload / 'python/runtime-origin.json')
    # Sign only public runtime Mach-O files; never inspect user installations.
    for path in sorted((payload / 'python').rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream: magic = stream.read(4)
            if magic in MACHO: sign(path)
    subprocess.run([str(payload / 'python/bin/python3'), '-I', '-B', '-c', 'import ssl,sqlite3,json,platform; assert platform.machine()=="arm64"; print("Bundled Python smoke check passed")'], check=True)
    shutil.copy2(ROOT / 'assets/AppIcon.icns', resources / 'AppIcon.icns')
    info = dict(CFBundleName='Install Bark Bridge', CFBundleDisplayName='Install Bark Bridge',
                CFBundleExecutable='BarkBridgeInstaller', CFBundleIdentifier='local.codex.bark.installer',
                CFBundlePackageType='APPL', CFBundleIconFile='AppIcon', CFBundleShortVersionString=version,
                CFBundleVersion=str(metadata['build']), LSMinimumSystemVersion='13.0', NSHighResolutionCapable=True)
    (installer / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', '-target', 'arm64-apple-macos13.0', '-framework', 'AppKit', str(ROOT / 'src/BarkInstaller.swift'), '-o', str(binaries / 'BarkBridgeInstaller')], check=True)
    sign(installer)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(installer)], check=True)
    for name, target in [('LICENSE', 'LICENSE.txt'), ('docs/BINARY-INSTALL.html', 'Start Here.html')]:
        shutil.copy2(ROOT / name, folder / target)
    archive = folder.with_name(folder.name + '.zip')
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(folder), str(archive)], check=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ROOT / 'dist/SHA256SUMS').write_text(checksum + '  ' + archive.name + '\n')
    print(json.dumps({'archive': str(archive), 'sha256': checksum, 'bytes': archive.stat().st_size,
                      'signed': 'ad-hoc', 'notarized': False, 'installed_data_read': False}))


if __name__ == '__main__':
    main()
