#!/usr/bin/env python3
"""Signed program-only updates. Never opens Bark device/config/rule/account data.

No daemon: invoked by the panel or by the menu's optional twelve-hour timer.
Protocol 1 updates the app and five public scripts, never Python or Hook/Skill paths.
"""
import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPOSITORY = 'dneo9527-ctrl/bark-bridge-macos'
API = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=30'
RUNTIME = ('bark.py', 'manage.py', 'claude_bark.py', 'bark-run', 'updater.py')
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
INTERVAL = 12 * 3600


class UpdateError(Exception):
    pass


def require(condition, code='invalid_manifest'):
    if not condition:
        raise UpdateError(code)


def atomic(path, data, mode=0o600):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, data):
    atomic(path, (json.dumps(data, sort_keys=True) + '\n').encode())


def read_json(path, default=None):
    try:
        require(path.stat().st_size <= 262144, 'local_state_error')
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {} if default is None else default


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(131072), b''): value.update(block)
    return value.hexdigest()


def version_key(tag):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-preview\.(\d+))?', tag or '')
    require(match is not None, 'unsupported_version')
    a, b, c, preview = match.groups()
    return int(a), int(b), int(c), 1 if preview is None else 0, int(preview or 0)


def release_url(tag, asset):
    version_key(tag)
    require(re.fullmatch(r'[A-Za-z0-9._-]+', asset) is not None)
    return 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + asset


def fetch(url, limit, destination=None, expected_size=None):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == 'https' and parsed.hostname in ('api.github.com', 'github.com'), 'untrusted_source')
    request = urllib.request.Request(url, headers={'User-Agent': 'BarkBridge-Updater/1', 'Accept': 'application/vnd.github+json'})
    started = time.monotonic()
    data = bytearray()
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            # GitHub release CDN redirects are HTTPS; no credentials are sent.
            final = urllib.parse.urlsplit(response.url)
            require(final.scheme == 'https' and (final.hostname in ('api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')), 'untrusted_source')
            length = response.headers.get('Content-Length')
            if length: require(int(length) <= limit, 'download_too_large')
            while True:
                require(time.monotonic() - started < 150, 'network_timeout')
                block = response.read(131072)
                if not block: break
                data.extend(block)
                require(len(data) <= limit, 'download_too_large')
    except urllib.error.HTTPError as error:
        raise UpdateError('rate_limited' if error.code in (403, 429) else 'download_unavailable') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UpdateError('network_error') from None
    if expected_size is not None: require(len(data) == expected_size, 'size_mismatch')
    if destination: atomic(destination, data)
    return bytes(data)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def manifest_data(raw, tag, arch):
    try: item = json.loads(raw, object_pairs_hook=no_duplicates)
    except (ValueError, TypeError): raise UpdateError('invalid_manifest') from None
    require(isinstance(item, dict))
    require(item.get('schema') == 1 and item.get('repository') == REPOSITORY)
    require(item.get('tag') == tag and item.get('arch') == arch, 'incompatible_update')
    version_key(tag)
    require(item.get('version') == tag.removeprefix('v').split('-')[0])
    require(type(item.get('build')) is int and item['build'] > 0)
    require(item.get('min_os') == 13 and item.get('runtime_protocol') == 1, 'incompatible_update')
    require(type(item.get('size')) is int and 0 < item['size'] <= MAX_ARCHIVE)
    require(isinstance(item.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', item['sha256']))
    require(item.get('asset') == 'Bark-Bridge-' + tag.removeprefix('v') + '-macOS-' + arch + '-update.zip')
    files = item.get('files')
    require(isinstance(files, dict) and 5 < len(files) < 1000)
    require(all('runtime/' + name in files for name in RUNTIME))
    require('Bark.app/Contents/Info.plist' in files and 'Bark.app/Contents/Helpers/BarkUpdateVerifier' in files)
    for name, details in files.items():
        path = PurePosixPath(name)
        require(not path.is_absolute() and str(path) == name and all(part not in ('.', '..') for part in path.parts))
        require(name.startswith('Bark.app/Contents/') or name in ['runtime/' + entry for entry in RUNTIME])
        require(isinstance(details, dict) and details.get('mode') in (0o600, 0o700))
        require(isinstance(details.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', details['sha256']))
    return item


def unpack(archive, destination, manifest):
    require(not destination.exists(), 'stage_exists')
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        require(len(members) == len(manifest['files']), 'invalid_archive')
        require(len({member.filename for member in members}) == len(members), 'invalid_archive')
        require(sum(member.file_size for member in members) <= MAX_EXPANDED, 'download_too_large')
        for member in members:
            require(member.filename in manifest['files'] and not member.is_dir(), 'invalid_archive')
            mode = member.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG), 'unsafe_archive')
            require(not member.flag_bits & 1, 'invalid_archive')
        destination.mkdir(mode=0o700)
        for member in members:
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            details = manifest['files'][member.filename]
            value = source.read(member)
            require(hashlib.sha256(value).hexdigest() == details['sha256'], 'checksum_mismatch')
            atomic(target, value, details['mode'])


def swap_directories(left, right):
    # Both bundles stay present even if the process is killed mid-transaction.
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    result = lib.renameatx_np(-2, os.fsencode(left), -2, os.fsencode(right), 2)  # RENAME_SWAP
    if result != 0: raise UpdateError('app_swap_failed')


def process_path(pid):
    if sys.platform != 'darwin': return ''
    buffer = ctypes.create_string_buffer(4096)
    lib = ctypes.CDLL('/usr/lib/libproc.dylib')
    count = lib.proc_pidpath(int(pid), buffer, len(buffer))
    if count > 0: return os.fsdecode(buffer.value)
    # An executing Mach-O that was renamed/unlinked can return ENOENT here.
    # ps comm retains the launch executable path (never command arguments).
    try:
        return subprocess.check_output(['/bin/ps', '-p', str(int(pid)), '-o', 'comm='],
                                       text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
    except (subprocess.SubprocessError, ValueError): return ''


def location_manager(base):
    # The isolated worker is copied under updates/, so importing a sibling by
    # module name would either fail or use the wrong installation's manager.
    path = Path(base) / 'manage.py'
    require(path.is_file() and not path.is_symlink(), 'app_location_error')
    spec = importlib.util.spec_from_file_location('_bark_update_location', path)
    require(spec is not None and spec.loader is not None, 'app_location_error')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_app(base):
    try:
        return location_manager(base).resolve_app(base=base)
    except UpdateError:
        raise
    except Exception:
        raise UpdateError('app_location_error') from None


def register_app(app, base):
    try:
        # Launch-at-login is a user preference; location refresh changes neither
        # that preference nor the stable launcher already installed by manage.
        location_manager(base).register_app(app, base=base, migrate_login=False)
    except UpdateError:
        raise
    except Exception:
        raise UpdateError('app_location_error') from None


class Updater:
    def __init__(self, app=None, base=None):
        self.base = Path(base or Path.home() / 'Library/Application Support/CodexBark')
        self.root = self.base / 'updates'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not self.base.is_symlink() and not self.root.is_symlink(), 'unsafe_destination')
        self.root.chmod(0o700)
        self.state_path = self.root / 'state.json'
        self.prefs_path = self.root / 'preferences.json'
        self.transaction_path = self.root / 'transaction.json'
        self.arch = platform.machine()
        self.explicit_app = app is not None
        self.app = self.app_destination(app if app is not None else resolve_app(self.base))
        txn = read_json(self.transaction_path)
        if txn.get('phase') in ('applying', 'awaiting_start'):
            # A current process may supply its own bundle, but cannot redirect
            # an interrupted transaction to a different installation.
            require(self.transaction_app(txn) == self.app, 'app_location_error')

    def app_destination(self, value):
        require(isinstance(value, (str, Path)), 'unsafe_destination')
        path = Path(value)
        require(path.is_absolute() and str(path) == os.path.normpath(str(path)), 'unsafe_destination')
        require(path.suffix.lower() == '.app' and not path.name.startswith('.Bark-update-'), 'unsafe_destination')
        require(not path.is_symlink(), 'unsafe_destination')
        # macOS exposes the same real directory through /var and /private/var.
        # Pin the canonical parent once, while still rejecting a symlink bundle.
        path = path.resolve()
        base = self.base.resolve()
        require(base != path and base not in path.parents and path not in base.parents, 'unsafe_destination')
        return path

    def transaction_app(self, txn):
        require(isinstance(txn, dict), 'local_state_error')
        identifier = txn.get('id')
        require(isinstance(identifier, str) and re.fullmatch('backup-[0-9a-f]{32}', identifier) is not None, 'local_state_error')
        present = txn.get('runtime_present')
        require(isinstance(present, list) and all(isinstance(name, str) and name in RUNTIME for name in present)
                and len(present) == len(set(present)), 'local_state_error')
        if 'app_path' not in txn:
            return self.legacy_transaction_app()
        require(isinstance(txn['app_path'], str), 'local_state_error')
        try:
            return self.app_destination(txn['app_path'])
        except UpdateError:
            raise UpdateError('local_state_error') from None

    def legacy_transaction_app(self):
        # Version 49 transactions predate destination pinning. Only a current
        # native process explicitly identifying the historical installation may
        # recover them; a missing bookmark never grants a new fallback target.
        require(self.explicit_app, 'local_state_error')
        def metadata(path):
            if not path.exists() and not path.is_symlink(): return None
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not stat.S_IMODE(info.st_mode) & 0o022, 'local_state_error')
            value = read_json(path)
            require(isinstance(value, dict), 'local_state_error')
            return value
        manifest = metadata(self.base / 'installation.json')
        gui = manifest.get('menu_app') if manifest else None
        known = self.app == Path('/Applications/Bark.app').resolve()
        if isinstance(gui, dict) and isinstance(gui.get('path'), str):
            known = self.app_destination(gui['path']) == self.app
        require(known, 'local_state_error')
        record = metadata(self.base / 'app-location.json')
        if record is not None:
            require(record.get('schema') == 1 and record.get('bundle_id') == 'local.codex.bark.menu'
                    and isinstance(record.get('path'), str)
                    and self.app_destination(record['path']) == self.app, 'local_state_error')
        return self.app

    def refresh_location(self, strict=True):
        try:
            register_app(self.app, self.base)
        except Exception:
            if strict: raise UpdateError('app_location_error') from None

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(str(self.root / '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise UpdateError('busy') from None
            yield
        finally: os.close(fd)

    def installed(self):
        import plistlib
        info = plistlib.loads((self.app / 'Contents/Info.plist').read_bytes())
        release = read_json(self.app / 'Contents/Resources/release.json')
        return {'version': info['CFBundleShortVersionString'], 'build': str(info['CFBundleVersion']),
                'tag': release.get('tag', 'v' + info['CFBundleShortVersionString']),
                'preview': release.get('channel', 'preview') == 'preview'}

    def status(self):
        state = read_json(self.state_path)
        installed = self.installed()
        # An old staged package must never remain an install button after an upgrade.
        if state.get('tag') and version_key(state['tag']) <= version_key(installed['tag']):
            state = {k: v for k, v in state.items() if k in ('checked_at', 'last_attempt', 'error')}
            state['status'] = 'current'
        return {'ok': True, **state, 'installed': installed,
                'automatic': read_json(self.prefs_path).get('automatic') is True}

    def set_automatic(self, enabled):
        with self.lock(): write_json(self.prefs_path, {'automatic': bool(enabled)})
        return self.status()

    def verify_signature(self, folder):
        helper = self.app / 'Contents/Helpers/BarkUpdateVerifier'
        result = subprocess.run([str(helper), 'verify', str(folder / 'manifest.json'), str(folder / 'manifest.sig')],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        require(result.returncode == 0, 'signature_invalid')

    def signed_manifest(self, folder, tag):
        self.verify_signature(folder)
        return manifest_data((folder / 'manifest.json').read_bytes(), tag, self.arch)

    def validate_payload(self, folder, manifest):
        expected = manifest['files']
        actual = set()
        require(folder.is_dir() and not folder.is_symlink(), 'invalid_archive')
        for path in folder.rglob('*'):
            require(not path.is_symlink(), 'unsafe_archive')
            if path.is_dir(): continue
            name = str(path.relative_to(folder))
            require(path.is_file() and name in expected, 'invalid_archive')
            require(digest(path) == expected[name]['sha256'], 'checksum_mismatch')
            require(stat.S_IMODE(path.stat().st_mode) == expected[name]['mode'], 'invalid_archive')
            actual.add(name)
        require(actual == set(expected), 'invalid_archive')
        import plistlib
        info = plistlib.loads((folder / 'Bark.app/Contents/Info.plist').read_bytes())
        require(info.get('CFBundleIdentifier') == 'local.codex.bark.menu')
        require(info.get('CFBundleShortVersionString') == manifest['version'] and str(info.get('CFBundleVersion')) == str(manifest['build']))
        release = read_json(folder / 'Bark.app/Contents/Resources/release.json')
        require(release.get('tag') == manifest['tag'] and release.get('update_protocol') == 1)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(folder / 'Bark.app')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        for name in RUNTIME:
            if name.endswith('.py'): compile((folder / 'runtime' / name).read_bytes(), name, 'exec')
        result = subprocess.run([str(folder / 'Bark.app/Contents/MacOS/CodexBarkMenu'), '--update-smoke-check'],
                                capture_output=True, timeout=10, check=True)
        require(json.loads(result.stdout).get('version') == manifest['version'], 'app_health_failed')

    def check(self, due=False, download=False):
        with self.lock():
            state = read_json(self.state_path)
            if due:
                if read_json(self.prefs_path).get('automatic') is not True: return self.status()
                if time.time() - state.get('last_attempt', 0) < INTERVAL: return self.status()
            state['last_attempt'] = time.time()
            write_json(self.state_path, state)
            try:
                releases = json.loads(fetch(API, 1024 * 1024))
                require(isinstance(releases, list), 'invalid_response')
                current = self.installed()
                eligible = []
                for release in releases:
                    if not isinstance(release, dict) or release.get('draft'): continue
                    if release.get('prerelease') and not current['preview']: continue
                    try: key = version_key(release.get('tag_name'))
                    except UpdateError: continue
                    if not current['preview'] and key[3] == 0: continue
                    assets = {a.get('name') for a in release.get('assets', []) if isinstance(a, dict)}
                    if {'bark-update-' + self.arch + '.json', 'bark-update-' + self.arch + '.sig'} <= assets:
                        eligible.append((key, release['tag_name']))
                latest = max(eligible, default=None)
                if not latest or latest[0] <= version_key(current['tag']):
                    state.update(status='current' if latest else 'no_compatible_release', checked_at=time.time(), error=None)
                    state.pop('tag', None)
                else:
                    tag = latest[1]
                    candidate = self.root / ('candidate-' + uuid.uuid4().hex)
                    candidate.mkdir(mode=0o700)
                    try:
                        atomic(candidate / 'manifest.json', fetch(release_url(tag, 'bark-update-' + self.arch + '.json'), 262144))
                        atomic(candidate / 'manifest.sig', fetch(release_url(tag, 'bark-update-' + self.arch + '.sig'), 1024))
                        manifest = self.signed_manifest(candidate, tag)
                        ready = state.get('status') == 'ready' and state.get('tag') == tag and (self.root / 'stage/payload').is_dir()
                        if ready:
                            state.update(checked_at=time.time(), error=None)
                        else:
                            existing = self.root / 'candidate'
                            if existing.exists(): shutil.rmtree(existing)
                            candidate.rename(existing)
                            state = {'status': 'available', 'tag': tag, 'size': manifest['size'], 'checked_at': time.time(),
                                     'last_attempt': time.time(), 'error': None}
                    finally:
                        if candidate.exists(): shutil.rmtree(candidate)
                write_json(self.state_path, state)
            except Exception as error:
                state['error'] = error.args[0] if isinstance(error, UpdateError) else 'check_failed'
                write_json(self.state_path, state)
            if download and state.get('status') == 'available' and not state.get('error'):
                self._download(state)
        return self.status()

    def _download(self, state):
        try:
            candidate = self.root / 'candidate'
            manifest = self.signed_manifest(candidate, state['tag'])
            require(version_key(manifest['tag']) > version_key(self.installed()['tag']), 'not_newer')
            stage = self.root / 'stage'
            if stage.exists(): shutil.rmtree(stage)
            stage.mkdir(mode=0o700)
            for name in ('manifest.json', 'manifest.sig'): shutil.copy2(candidate / name, stage / name)
            fetch(release_url(manifest['tag'], manifest['asset']), MAX_ARCHIVE, stage / 'update.zip', manifest['size'])
            require(digest(stage / 'update.zip') == manifest['sha256'], 'checksum_mismatch')
            unpack(stage / 'update.zip', stage / 'payload', manifest)
            self.validate_payload(stage / 'payload', manifest)
            state.update(status='ready', error=None)
        except Exception as error:
            state.update(status='available', error=error.args[0] if isinstance(error, UpdateError) else 'download_failed')
        write_json(self.state_path, state)

    def download(self):
        with self.lock():
            state = read_json(self.state_path)
            require(state.get('status') == 'available', 'check_required')
            self._download(state)
        return self.status()

    def start(self, parent_pid):
        require(process_path(parent_pid) == str(self.app / 'Contents/Helpers/Codex Bark Panel.app/Contents/MacOS/CodexBarkPanel'), 'invalid_parent')
        with self.lock():
            require(self.status().get('status') == 'ready', 'check_required')
            require(not self.transaction_path.exists() or read_json(self.transaction_path).get('phase') in ('complete', 'rolled_back'), 'recovery_required')
            atomic(self.root / 'worker.py', Path(__file__).read_bytes(), 0o700)
            subprocess.Popen([sys.executable, '-I', '-B', str(self.root / 'worker.py'), 'apply',
                              '--app', str(self.app), '--base', str(self.base), '--parent-pid', str(parent_pid)],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return {'ok': True, 'started': True}

    def backup_runtime(self, destination):
        destination.mkdir(mode=0o700)
        present = []
        for name in RUNTIME:
            path = self.base / name
            require(not path.is_symlink(), 'unsafe_destination')
            if path.exists():
                require(path.is_file(), 'unsafe_destination')
                shutil.copy2(path, destination / name)
                present.append(name)
        return present

    def restore(self, txn):
        # Transaction paths are generated locally, never supplied by the release.
        destination = self.transaction_app(txn)
        require(destination == self.app, 'app_location_error')
        backup = self.root / txn['id']
        sibling = destination.with_name('.Bark-update-' + txn['id'] + '.app')
        require(backup.is_dir() and not backup.is_symlink() and (backup / 'Bark.app').is_dir()
                and not (backup / 'Bark.app').is_symlink() and (backup / 'runtime').is_dir()
                and not (backup / 'runtime').is_symlink(), 'local_state_error')
        require(not destination.is_symlink() and not sibling.is_symlink(), 'unsafe_destination')
        for name in txn['runtime_present']:
            source = backup / 'runtime' / name
            require(source.is_file() and not source.is_symlink(), 'local_state_error')
        if 'app_path' not in txn:
            txn = dict(txn, app_path=str(destination))
            write_json(self.transaction_path, txn)
        if (backup / 'Bark.app').is_dir():
            if sibling.exists(): shutil.rmtree(sibling)
            shutil.copytree(backup / 'Bark.app', sibling)
            swap_directories(destination, sibling)
            shutil.rmtree(sibling)
        for name in RUNTIME:
            source = backup / 'runtime' / name
            target = self.base / name
            require(not target.is_symlink(), 'unsafe_destination')
            if name in txn['runtime_present']: atomic(target, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))
            else: target.unlink(missing_ok=True)
        txn['phase'] = 'rolled_back'
        write_json(self.transaction_path, txn)
        state = read_json(self.state_path)
        state.update(status='available', error='update_rolled_back')
        write_json(self.state_path, state)
        # Restore the app and programs first. A broken bookmark must never
        # interrupt rollback or prevent the caller from restarting the old app.
        self.refresh_location(strict=False)

    def recover(self):
        recovered = False
        try:
            with self.lock():
                txn = read_json(self.transaction_path)
                if txn.get('phase') in ('applying', 'awaiting_start'):
                    self.restore(txn)
                    recovered = True
        except UpdateError as error:
            if str(error) != 'busy': raise
        return {'ok': True, 'recovered': recovered}

    def stop_menu(self):
        executable = str(self.app / 'Contents/MacOS/CodexBarkMenu')
        pids = subprocess.run(['/bin/ps', '-axo', 'pid='], capture_output=True, text=True, timeout=5, check=True).stdout.split()
        for pid in pids:
            if process_path(int(pid)) == executable:
                os.kill(int(pid), signal.SIGTERM)
                for _ in range(40):
                    if process_path(int(pid)) != executable: break
                    time.sleep(0.05)
                require(process_path(int(pid)) != executable, 'menu_still_running')

    def restart_menu(self):
        self.stop_menu()
        subprocess.run(['/usr/bin/open', '-g', str(self.app), '--args', '--background'], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def apply(self, parent_pid=0, restart=True, fault=None):
        if parent_pid:
            expected = str(self.app / 'Contents/Helpers/Codex Bark Panel.app/Contents/MacOS/CodexBarkPanel')
            for _ in range(150):
                if process_path(parent_pid) != expected: break
                time.sleep(0.1)
            require(process_path(parent_pid) != expected, 'panel_still_open')
        with self.lock():
            state = read_json(self.state_path)
            require(not self.transaction_path.exists() or read_json(self.transaction_path).get('phase') in ('complete', 'rolled_back'), 'recovery_required')
            require(state.get('status') == 'ready', 'check_required')
            stage = self.root / 'stage'
            manifest = self.signed_manifest(stage, state['tag'])
            require(version_key(manifest['tag']) > version_key(self.installed()['tag']), 'not_newer')
            require(self.arch == manifest['arch'], 'incompatible_update')
            self.validate_payload(stage / 'payload', manifest)
            require(self.app.is_dir() and not self.app.is_symlink() and os.access(self.app.parent, os.W_OK), 'install_permission')
            txn = {'id': 'backup-' + uuid.uuid4().hex, 'phase': 'preparing', 'tag': state['tag'],
                   'app_path': str(self.app)}
            backup = self.root / txn['id']; backup.mkdir(mode=0o700)
            txn['runtime_present'] = self.backup_runtime(backup / 'runtime')
            shutil.copytree(self.app, backup / 'Bark.app')
            sibling = self.app.with_name('.Bark-update-' + txn['id'] + '.app')
            shutil.copytree(stage / 'payload/Bark.app', sibling)
            txn['phase'] = 'applying'
            write_json(self.transaction_path, txn)
            try:
                if fault: fault('before_swap')
                if restart: self.stop_menu()
                swap_directories(self.app, sibling)
                if fault: fault('after_swap')
                for name in RUNTIME:
                    atomic(self.base / name, (stage / 'payload/runtime' / name).read_bytes(), 0o700)
                    if fault: fault('runtime:' + name)
                require(self.installed()['tag'] == manifest['tag'], 'app_health_failed')
                self.refresh_location()
                txn['phase'] = 'awaiting_start'
                write_json(self.transaction_path, txn)
                if restart:
                    (self.root / 'ready.json').unlink(missing_ok=True)
                    self.restart_menu()
                    for _ in range(200):
                        receipt = read_json(self.root / 'ready.json')
                        if receipt.get('tag') == manifest['tag'] and process_path(receipt.get('pid', 0)) == str(self.app / 'Contents/MacOS/CodexBarkMenu'): break
                        time.sleep(0.1)
                    else: raise UpdateError('app_start_failed')
                if fault: fault('before_commit')
                txn['phase'] = 'complete'
                write_json(self.transaction_path, txn)
                state.update(status='current', error=None)
                write_json(self.state_path, state)
                if sibling.exists(): shutil.rmtree(sibling)
            except Exception:
                self.restore(txn)
                if restart: self.restart_menu()
                raise
        return {'ok': True, 'updated': True}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'automatic', 'check', 'download', 'start', 'apply', 'recover'])
    parser.add_argument('value', nargs='?', choices=['on', 'off'])
    parser.add_argument('--due', action='store_true')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--parent-pid', type=int, default=0)
    parser.add_argument('--app')
    parser.add_argument('--base', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        updater = Updater(app=args.app, base=args.base)
        if args.action == 'status': result = updater.status()
        elif args.action == 'automatic':
            require(args.value in ('on', 'off'), 'invalid_request')
            result = updater.set_automatic(args.value == 'on')
        elif args.action == 'check': result = updater.check(args.due, args.download)
        elif args.action == 'download': result = updater.download()
        elif args.action == 'start': result = updater.start(args.parent_pid)
        elif args.action == 'apply': result = updater.apply(args.parent_pid)
        else: result = updater.recover()
    except Exception as error:
        # Never expose URLs, server response bodies, user paths or raw exceptions.
        result = {'ok': False, 'error': str(error) if isinstance(error, UpdateError) else 'update_failed'}
    print(json.dumps(result))


if __name__ == '__main__': main()
