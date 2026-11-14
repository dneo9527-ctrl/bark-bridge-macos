#!/usr/bin/env python3
"""Build locally for this Mac. Does not install or launch the app."""
import argparse
import platform
import plistlib
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=['arm64', 'x86_64'], default=platform.machine())
    args = parser.parse_args()
    if platform.system() != 'Darwin':
        raise SystemExit('macOS and Xcode Command Line Tools are required.')
    version = (ROOT / 'VERSION').read_text().strip()
    app = ROOT / 'build/Bark.app'
    panel = app / 'Contents/Helpers/Codex Bark Panel.app'
    for bundle, executable, identifier, accessory in [
        (app, 'CodexBarkMenu', 'local.codex.bark.menu', True),
        (panel, 'CodexBarkPanel', 'local.codex.bark.panel', False),
    ]:
        (bundle / 'Contents/MacOS').mkdir(parents=True, exist_ok=True)
        (bundle / 'Contents/Resources').mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'assets/AppIcon.icns', bundle / 'Contents/Resources/AppIcon.icns')
        info = dict(CFBundleName='Bark', CFBundleDisplayName='Bark', CFBundlePackageType='APPL',
                    CFBundleExecutable=executable, CFBundleIdentifier=identifier, CFBundleIconFile='AppIcon',
                    CFBundleShortVersionString=version, CFBundleVersion='44', LSMinimumSystemVersion='13.0',
                    LSUIElement=accessory, LSMultipleInstancesProhibited=True, NSHighResolutionCapable=True)
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', '-target', args.arch+'-apple-macos13.0',
                    '-framework', 'AppKit', '-framework', 'UniformTypeIdentifiers',
                    str(ROOT / 'src/CodexBark.swift'), '-o', str(panel / 'Contents/MacOS/CodexBarkPanel')], check=True)
    subprocess.run(['/usr/bin/xcrun', 'clang', '-O2', '-fobjc-arc', '-arch', args.arch,
                    '-mmacosx-version-min=13.0', '-framework', 'AppKit', '-framework', 'IOKit',
                    str(ROOT / 'src/CodexBarkMenu.m'), '-o', str(app / 'Contents/MacOS/CodexBarkMenu')], check=True)
    for bundle in [panel, app]:
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(bundle)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print('Built locally with ad-hoc signature: ' + str(app))


if __name__ == '__main__':
    main()
