#!/usr/bin/env python3
"""Create a program-only update and signed manifest from clean public sources.

Build first. --signing-key is a local maintainer path, never an uploaded asset.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import zipfile
from release_metadata import read_metadata, verify_app_identity

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signing-key', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    metadata = read_metadata()
    os.umask(0o077)
    subprocess.run([os.sys.executable, str(ROOT/'scripts/audit.py')], check=True)
    spec = importlib.util.spec_from_file_location('updater', ROOT/'src/runtime/updater.py')
    updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
    app = ROOT/'build/Bark.app'
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)], check=True)
    info, release = verify_app_identity(app, metadata)
    arch = os.uname().machine
    tag = release['tag']
    version = info['CFBundleShortVersionString']
    args.out.mkdir(parents=True, exist_ok=True)
    name = 'Bark-Bridge-'+tag.removeprefix('v')+'-macOS-'+arch+'-update.zip'
    archive = args.out/name
    if archive.exists(): raise SystemExit('Output already exists; do not overwrite a release artifact.')
    files = {}
    sources = [(str(path.relative_to(app.parent)),path) for path in sorted(app.rglob('*')) if path.is_file()]
    sources += [('runtime/'+name,ROOT/'src/runtime'/name) for name in updater.RUNTIME]
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for relative, path in sources:
            if path.is_symlink(): raise SystemExit('Symlink is not permitted in program update.')
            data = path.read_bytes()
            mode = 0o700 if path.stat().st_mode & 0o111 or relative.startswith('runtime/') else 0o600
            files[relative] = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode}
            entry = zipfile.ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3; entry.external_attr = (0o100000|mode)<<16
            entry.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(entry, data)
    manifest = dict(schema=1, repository=updater.REPOSITORY, tag=tag, version=version,
                    build=int(info['CFBundleVersion']), arch=arch, min_os=13, runtime_protocol=1,
                    asset=name, size=archive.stat().st_size, sha256=updater.digest(archive), files=files)
    updater.manifest_data(json.dumps(manifest).encode(), tag, arch)
    metadata = args.out/('bark-update-'+arch+'.json')
    signature = args.out/('bark-update-'+arch+'.sig')
    metadata.write_text(json.dumps(manifest, sort_keys=True, separators=(',',':'))+'\n')
    subprocess.run(['/usr/bin/xcrun','swift',str(ROOT/'scripts/sign-update.swift'),'sign',str(args.signing_key),str(metadata),str(signature)],check=True)
    # Verify immediately using the public key embedded in the built app.
    subprocess.run([str(app/'Contents/Helpers/BarkUpdateVerifier'),'verify',str(metadata),str(signature)],check=True)
    checksums = [updater.digest(path)+'  '+path.name for path in (archive,metadata,signature)]
    (args.out/'SHA256SUMS').write_text('\n'.join(checksums)+'\n')
    print(json.dumps({'archive':str(archive),'sha256':manifest['sha256'],'bytes':manifest['size'],
                      'manifest':str(metadata),'signature':str(signature),'private_state_read':False}))


if __name__ == '__main__': main()
