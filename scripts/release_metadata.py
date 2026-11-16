"""Validate one release identity before compiling or packaging any artifact."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read_metadata(root=ROOT):
    value = json.loads((root / 'assets/release.json').read_text())
    version = (root / 'VERSION').read_text().strip()
    if (not isinstance(value, dict) or not re.fullmatch(r'\d+\.\d+\.\d+', version)
            or value.get('version') != version or type(value.get('build')) is not int
            or value['build'] <= 0):
        raise ValueError('Release version or build does not match the source identity.')
    channel, tag = value.get('channel'), value.get('tag')
    valid = (channel == 'stable' and tag == 'v' + version)
    valid |= (channel == 'preview' and isinstance(tag, str)
              and re.fullmatch(re.escape('v' + version) + r'-preview\.[1-9]\d*', tag) is not None)
    if not valid:
        raise ValueError('Release tag and update channel do not match.')
    return value


def verify_app_identity(app, metadata):
    import plistlib
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    release = json.loads((app / 'Contents/Resources/release.json').read_bytes())
    if (info.get('CFBundleIdentifier') != 'local.codex.bark.menu'
            or info.get('CFBundleShortVersionString') != metadata['version']
            or info.get('CFBundleVersion') != str(metadata['build'])
            or release.get('tag') != metadata['tag']
            or release.get('channel') != metadata['channel']
            or release.get('update_protocol') != 1):
        raise ValueError('Built App identity differs from the release metadata; rebuild first.')
    return info, release
