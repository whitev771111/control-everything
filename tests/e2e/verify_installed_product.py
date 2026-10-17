"""Coordinator-only Windows installer acceptance in a new, retained evidence directory.

No developer packages are imported by the driver. The private runtime executes the
probe with -I from a foreign cwd. This is PATH isolation on the current machine,
not a claim that every clean Windows 10/11 machine has been tested.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


APP_ID = '{A68E7F38-54DB-4A76-9DD2-89B3554F617D}'
UNINSTALL_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID + '_is1'
ASSETS = {
    '/': 'sessions/index.html', '/app.js': 'sessions/app.js', '/style.css': 'sessions/style.css',
    '/progress': 'progress/progress.html', '/progress.js': 'progress/progress.js',
    '/progress.css': 'progress/progress.css', '/workspace': 'workspace/workspace.html',
    '/workspace.js': 'workspace/workspace.js', '/workspace.css': 'workspace/workspace.css',
    '/task-center.js': 'workspace/task-center.js', '/execution.js': 'workspace/execution.js',
    '/observation.js': 'workspace/observation.js', '/news.js': 'workspace/news.js',
    '/quota.js': 'workspace/quota.js',
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def unlinked(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute_path_required')
    require('..' not in path.parts, 'canonical_path_required')
    for entry in (path, *path.parents):
        require(not entry.is_symlink() and not entry.is_junction(), 'linked_input')
    return path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def document(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def isolated_environment():
    environment = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'NODE_OPTIONS', 'NODE_PATH', 'SESSION_PANEL_DATA_DIR'):
        environment.pop(name, None)
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    require(all(shutil.which(tool, path=environment['PATH']) is None
                for tool in ('python.exe', 'node.exe', 'rustc.exe', 'cargo.exe')), 'developer_tool_on_test_path')
    return environment


def guard_registration():
    import winreg
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | view):
                raise ValueError('existing_app_id_registration')
        except FileNotFoundError:
            pass
    shortcut = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Session Panel.lnk'
    require(not shortcut.exists() and not shortcut.is_symlink(), 'existing_start_menu_shortcut')


class Acceptance:
    def __init__(self, evidence, environment):
        self.root, self.env = evidence, environment
        self.rows = []
        self.sequence = 0

    def run(self, argv, *, expected=0, timeout=600, stdin=None):
        self.sequence += 1
        stem = self.root / ('command-%03d' % self.sequence)
        # Popen avoids subprocess.run's implicit kill on a timeout. Preserve failures.
        with stem.with_suffix('.stdout').open('xb') as out, stem.with_suffix('.stderr').open('xb') as err:
            process = subprocess.Popen(list(map(str, argv)), cwd=self.root / 'foreign', env=self.env,
                                       stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, shell=False)
            try:
                process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.rows.append({'argv': list(map(str, argv)), 'pid': process.pid, 'exit': None,
                                  'reason': 'deadline_process_retained', 'log': stem.name})
                raise ValueError('deadline_process_retained:' + str(process.pid)) from None
        row = {'argv': list(map(str, argv)), 'pid': process.pid, 'exit': process.returncode, 'log': stem.name}
        self.rows.append(row)
        require(process.returncode == expected if expected is not None else process.returncode != 0,
                'unexpected_exit:' + stem.name)
        return stem.with_suffix('.stdout').read_text(encoding='utf-8-sig')

    def install(self, installer, install_root, label, *, blocked=False):
        before = sha(install_root / 'session-panel.cmd') if blocked else None
        self.run([installer, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                  '/DIR=' + str(install_root), '/LOG=' + str(self.root / (label + '.log'))],
                 expected=None if blocked else 0)
        if blocked:
            require(sha(install_root / 'session-panel.cmd') == before, 'blocked_install_changed_launcher')
            raw = (self.root / (label + '.log')).read_bytes()
            marker = 'Session Panel maintenance preflight exit=20'
            require(marker.encode('ascii') in raw or marker.encode('utf-16-le') in raw,
                    'http_block_reason_not_proven')

    def launcher(self, install_root, version_id):
        expected = ('@echo off\r\n"%~dp0versions\\' + version_id
                    + '\\runtime\\python.exe" -I -B -X utf8 -m session_panel.gateway %*\r\n').encode('ascii')
        require((install_root / 'session-panel.cmd').read_bytes() == expected, 'active_launcher_target')

    def probe(self, version, profile, label, *, while_http=None):
        ready, stop = self.root / (label + '-ready.json'), self.root / (label + '-stop')
        argv = [version / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', Path(__file__).resolve(),
                '--probe', '--payload', version, '--profile', profile, '--ready', ready, '--stop', stop]
        with (self.root / (label + '.stdout')).open('xb') as out, (self.root / (label + '.stderr')).open('xb') as err:
            process = subprocess.Popen(list(map(str, argv)), cwd=self.root / 'foreign', env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False)
            try:
                deadline = time.monotonic() + 180
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                require(ready.exists(), 'probe_not_ready:' + label)
                if while_http:
                    while_http()
            finally:
                stop.touch(exist_ok=False)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.rows.append({'probe': label, 'pid': process.pid, 'exit': None, 'reason': 'retained'})
                    raise ValueError('probe_cleanup_unverified:' + str(process.pid)) from None
            self.rows.append({'probe': label, 'pid': process.pid, 'exit': process.returncode})
            require(process.returncode == 0, 'probe_failed:' + label)
        return json.loads(ready.read_text(encoding='utf-8'))


def probe(args):
    # Only the explicitly supplied product interpreter can import these packages.
    import importlib.metadata
    import sqlite3
    import ssl
    import secrets
    import threading
    from urllib.error import HTTPError
    from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
    from session_panel.http.gateway import build_profile_server, make_server
    from session_panel.installation.manifest import verify_payload
    from session_panel.paths import observer_install_dir, source_root, web_root
    from session_panel.workspace.news_source import Collector, NewsSource
    from session_panel.workspace import news_worker
    from session_panel.sessions import source as remote_source
    import session_panel

    payload, profile = unlinked(args.payload), unlinked(args.profile)
    result = verify_payload(payload, strict_native=True)
    require(Path(sys.executable) == payload / 'runtime/python.exe', 'wrong_interpreter')
    require(Path(session_panel.__file__).is_relative_to(payload / 'runtime/Lib/site-packages'), 'source_import')
    require(source_root() is None, 'checkout_resource_fallback')
    require(observer_install_dir() == payload / 'native/observer', 'native_location')
    require(importlib.metadata.version('session-panel') == result['version'], 'metadata_version')
    ssl.create_default_context()
    require(sqlite3.connect(':memory:').execute('select 1').fetchone() == (1,), 'sqlite_unavailable')
    require(Path(news_worker.__file__).is_file() and Path(remote_source.__file__).is_file(), 'worker_source_missing')
    stopped = threading.Event()
    stopped.set()
    batch = news_worker.OwnedBatch('ai', secrets.token_hex(16), time.monotonic() + 5, stopped)
    frames, reason, cleaned = batch.run()
    require(batch.assigned and batch.pid is not None and cleaned and reason == 'timeout'
            and batch.process is None and batch.job is None and not frames, 'owned_job_cleanup')
    # No public feed is fetched in this HTTP/resource contract.
    collector = Collector(launch=lambda slot: collector.finish(slot, [], 'worker_failed', True))
    server = build_profile_server(str(profile), port=0, news_source=NewsSource(collector))
    legacy, thread, legacy_thread = None, None, None
    try:
        server.start_observer()
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *unused):
                return None
        try:
            build_opener(ProxyHandler({}), NoRedirect()).open(
                'http://127.0.0.1:%s/progress' % server.server_port, timeout=5)
            raise ValueError('profile_redirect_missing')
        except HTTPError as response:
            require(response.code == 302 and response.headers['Location'] == '/workspace#projects', 'profile_redirect')
            response.close()
        # Legacy resource URLs retain their original bytes outside profile routing.
        legacy = make_server(None, ('127.0.0.1', 0), workspace=server.workspace)
        legacy_thread = threading.Thread(target=legacy.serve_forever)
        legacy_thread.start()
        assets = []
        for route, relative in ASSETS.items():
            with opener.open('http://127.0.0.1:%s%s' % (legacy.server_port, route), timeout=5) as response:
                body = response.read()
                require(body == web_root().joinpath(relative).read_bytes(), 'resource_bytes:' + route)
                mime = 'text/html' if relative.endswith('.html') else 'text/css' if relative.endswith('.css') else 'text/javascript'
                require(response.headers['Content-Type'] == mime + '; charset=utf-8', 'resource_mime:' + route)
                require(response.headers['X-Content-Type-Options'] == 'nosniff'
                        and response.headers['Cache-Control'] == 'no-store'
                        and response.headers['Content-Security-Policy'], 'resource_headers:' + route)
                assets.append({'url': route, 'sha256': hashlib.sha256(body).hexdigest(), 'mime': mime})
        require(server.capabilities['execution'] is False, 'paused_execution_enabled')
        store = server.profile.store
        projects = store.list_projects()
        if not projects:
            for label, percent in (('null', None), ('zero', 0)):
                directory = profile / ('synthetic-project-' + label)
                directory.mkdir()
                project = store.save_project({'id': None, 'name': label, 'archived': False,
                    'directories': [{'id': 'main', 'path': str(directory)}],
                    'source': {'kind': 'none', 'directory_id': None, 'relative_path': None}})
                store.save_manual({'project_id': project['id'], 'state': '未开始', 'percent': percent, 'note': 'synthetic'})
            projects = store.list_projects()
        manual = store.manual_rows()
        percentages = {item['name']: manual[item['id']]['percent'] for item in projects}
        require(percentages == {'null': None, 'zero': 0}, 'null_zero_changed')
        deadline = time.monotonic() + 20
        while server.observer.cached()['status'] != 'available' and time.monotonic() < deadline:
            time.sleep(.1)
        require(server.observer.cached()['status'] == 'available', 'native_not_available')
        document(args.ready, {'version': result['version'], 'closure': result['closure'], 'assets': assets,
                             'executable': sys.executable, 'package': session_panel.__file__,
                             'ssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version,
                             'native': 'available', 'news': 'offline fixture; no collection claim',
                             'news_job': {'pid': batch.pid, 'assigned_before_resume': True, 'cleanup': True,
                                          'scope': 'cancelled while suspended; no network or successful collection claim'},
                             'manual_percent': percentages,
                             'controlled_path': os.environ['PATH'], 'http_port': server.server_port})
        deadline = time.monotonic() + 600
        while not args.stop.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        require(args.stop.exists(), 'probe_owner_disappeared')
    finally:
        if legacy_thread is not None:
            legacy.shutdown()
            legacy_thread.join(5)
        if legacy is not None:
            legacy.server_close()
        if thread is not None:
            server.shutdown()
            thread.join(5)
        server.server_close()
    return 0


def payload_document(payload):
    payload = unlinked(payload)
    value = json.loads((payload / 'product.json').read_text(encoding='utf-8'))
    require(value['product'] == 'session-panel' and len(value['closure']) == 64, 'payload_header')
    return value, value['version'] + '-' + value['closure'][:16]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--second-payload', type=Path)
    parser.add_argument('--second-installer', type=Path)
    parser.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--profile', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--ready', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--stop', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require(sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64'), 'windows_x64_required')
    if args.probe:
        return probe(args)
    require(args.installer is not None and args.evidence is not None, 'installer_and_evidence_required')
    require(bool(args.second_payload) == bool(args.second_installer), 'second_pair_required')
    evidence, installer = unlinked(args.evidence), unlinked(args.installer)
    require(not evidence.exists() and evidence.parent.is_dir(), 'new_evidence_directory_required')
    require(installer.is_file(), 'installer_missing')
    first, first_id = payload_document(args.payload)
    second = payload_document(args.second_payload) if args.second_payload else None
    if second:
        require(second[1] != first_id and unlinked(args.second_installer).is_file(), 'different_second_payload_required')
    guard_registration()
    environment = isolated_environment()
    evidence.mkdir()
    (evidence / 'foreign').mkdir()
    install_root = evidence / 'installation'
    acceptance = Acceptance(evidence, environment)
    report = {'schema': 1, 'status': 'failed', 'machine': platform.platform(),
              'scope': 'current Windows x64 machine; controlled PATH; synthetic profiles only',
              'installer_sha256': sha(installer), 'source': first['source'], 'commands': acceptance.rows,
              'errors': [], 'not_covered': ['clean Windows 10 and 11 matrix', 'real SSH/Agent/desktop focus',
                                           'live public news success', 'normal-running news worker collection']}
    profiles = [evidence / 'profile-a', evidence / 'profile-b']
    active = None
    try:
        acceptance.install(installer, install_root, 'install')
        acceptance.launcher(install_root, first_id)
        active = install_root / 'versions' / first_id
        for module in ('gateway', 'profile'):
            acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.' + module, '--help'])
        acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.native_processes', '--help'], expected=1)
        acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.mcp_stdio'], stdin=b'')
        acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8',
                        active / 'runtime/Lib/site-packages/session_panel/workspace/news_worker.py'], expected=2)
        report['probes'] = []
        for index, profile in enumerate(profiles):
            acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.profile', 'init', '--data-dir', profile])
            report['probes'].append(acceptance.probe(active, profile, 'profile-' + str(index),
                while_http=(lambda: acceptance.install(installer, install_root, 'http-blocked', blocked=True)) if index == 0 else None))
            preserved = acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.profile',
                                        'preserve-tasks', '--data-dir', profile])
            backup = json.loads(preserved)['backup']
            acceptance.run([active / 'runtime/python.exe', '-I', '-B', '-X', 'utf8', '-m', 'session_panel.profile', 'restore',
                            '--backup', backup, '--data-dir', evidence / ('restored-' + str(index))])
        saved = {str(path): sha(path) for profile in profiles for path in profile.rglob('*')
                 if path.is_file() and (path.name in ('profile.json', 'workspace-state.sqlite3') or 'backups' in path.parts)}
        if second:
            acceptance.install(args.second_installer, install_root, 'upgrade')
            acceptance.launcher(install_root, second[1])
            active = install_root / 'versions' / second[1]
            require((install_root / 'versions' / first_id).is_dir(), 'old_version_removed')
            report['probes'].append(acceptance.probe(active, profiles[0], 'upgraded'))
            # Running the original installer selects its exact preserved payload.
            acceptance.install(installer, install_root, 'rollback')
            acceptance.launcher(install_root, first_id)
            active = install_root / 'versions' / first_id
            report['probes'].append(acceptance.probe(active, profiles[1], 'rolled-back'))
        acceptance.install(installer, install_root, 'same-version-reinstall')
        acceptance.launcher(install_root, first_id)
        sentinel = install_root / 'unknown-user-file.txt'
        sentinel.write_bytes(b'owned acceptance sentinel; installer must preserve unknown files\n')
        uninstaller = install_root / 'unins000.exe'
        require(uninstaller.is_file(), 'uninstaller_missing')
        acceptance.run([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                        '/LOG=' + str(evidence / 'uninstall.log')])
        active = None
        require(sentinel.read_bytes().startswith(b'owned acceptance'), 'unknown_file_deleted')
        require(not (install_root / 'session-panel.cmd').exists(), 'launcher_not_removed')
        require(all(Path(path).is_file() and sha(path) == digest for path, digest in saved.items()), 'profile_or_backup_changed')
        guard_registration()
        report['status'] = 'passed'
        report['two_versions'] = bool(second)
        if not second:
            report['not_covered'].append('two distinct versions upgrade/rollback; supply --second-payload/--second-installer')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report['errors'].append(type(error).__name__ + ':' + str(error))
    finally:
        # Never force-kill or uninstall after a failure. Only exact owned profile peers may be stopped.
        if active is not None:
            for profile in profiles:
                if profile.is_dir():
                    try:
                        acceptance.run([active / 'native/observer/session-panel-observer.exe', 'stop', '--data-dir', profile], expected=0, timeout=8)
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        report['errors'].append('cleanup:' + str(error))
        document(evidence / 'report.json', report)
    print(json.dumps({'status': report['status'], 'evidence': str(evidence), 'errors': report['errors']}))
    return 0 if report['status'] == 'passed' and not report['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
