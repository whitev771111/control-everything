"""Coordinator-only: owned synthetic Node fixtures, real pipes, and cleanup receipts."""

import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from session_panel.sessions.observer_client import ObserverClient
from session_panel.sessions.observer_pipe import IoGate, WindowsPipe


def wait_for(callback, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('fixture_deadline')


def raw_boundary(directory, payload):
    client = SimpleNamespace(directory=directory, closed=threading.Event())
    gate = IoGate()
    pipe = WindowsPipe(client, gate)
    try:
        pipe.connect()
        gate.operation(client, pipe.pipe, data=payload, deadline=time.monotonic() + 1)
        deadline = time.monotonic() + 3
        def read(size):
            data = bytearray()
            while len(data) < size:
                data.extend(gate.operation(client, pipe.pipe, size=size - len(data), deadline=deadline))
            return data
        count = struct.unpack('<I', read(4))[0]
        if count > 524288:
            raise AssertionError('unbounded_response')
        return json.loads(read(count))
    finally:
        pipe.close(retired=gate.retired.pipe if gate.retired else None)
        if gate.arena:
            gate.arena.dispose()
        gate.reap_retired()
        if gate.retired:
            raise AssertionError('cleanup_pending')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', action='store_true', required=True)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32' or not args.node.is_file():
        raise SystemExit('Windows and an explicit Node executable are required')
    work = ROOT / 'work' / 'native-observer-fixture' / uuid.uuid4().hex
    work.mkdir(parents=True)
    binary = ROOT / 'work/native-observer/install/session-panel-observer.exe'
    profile = work / 'profile'
    profile.mkdir()
    prefix = work / 'bin'
    entry = prefix / 'node_modules/@openai/codex/bin/codex.js'
    entry.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'tests/fixtures/native_observer/idle.js', entry)
    (entry.parent.parent / 'package.json').write_text(json.dumps({
        'name': '@openai/codex', 'version': '0.0.0-fixture', 'bin': {'codex': 'bin/codex.js'}}), encoding='utf-8')
    (prefix / 'codex.cmd').write_text('@echo off\r\n', encoding='utf-8')
    node = prefix / 'node.exe'
    try:
        os.link(args.node.resolve(), node)
    except OSError:
        shutil.copyfile(args.node, node)
    environment = os.environ.copy()
    # Isolate PATH discovery. Normal OS/AppX enumeration still runs; only owned PIDs are recorded.
    environment['PATH'] = str(prefix)
    environment['PATHEXT'] = '.EXE;.CMD'
    roots = []
    for index in range(16):
        directory = work / ('project-' + str(index))
        directory.mkdir()
        roots.append({'directory_id': 'directory-' + str(index), 'path': str(directory)})
    children, daemon, client = [], None, None
    evidence = {'synthetic_load_only': True, 'os_enumeration': True, 'build_mode': 'debug',
                'model_calls': 0, 'phases': [], 'cleanup': [], 'errors': []}
    try:
        daemon = subprocess.Popen([str(binary), 'daemon', '--data-dir', str(profile)], env=environment,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=0x08000000)
        def ready():
            result = subprocess.run([str(binary), 'status', '--data-dir', str(profile)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=5,
                                    creationflags=0x08000000)
            assert daemon.poll() is None, 'owned_daemon_exited_before_ready'
            return result.returncode == 0
        wait_for(ready)
        evidence['daemon_pid'] = daemon.pid
        client = ObserverClient(profile, scope_supplier=lambda: roots)
        client.start()
        for count in (1, 8, 16):
            while len(children) < count:
                index = len(children)
                children.append(subprocess.Popen([str(node), str(entry)], cwd=roots[index]['path'], env=environment,
                                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                 creationflags=0x08000000))
            expected = {child.pid for child in children}
            def observed():
                cached = client.cached()
                sample = cached['snapshot']
                if cached['status'] != 'available' or not sample or sample['status'] != 'available':
                    return None
                rows = [row for row in sample['instances'] if row['pid'] in expected]
                return (sample, rows) if {row['pid'] for row in rows} == expected else None
            sample, rows = wait_for(observed)
            previous = sample['seq']
            begin = time.monotonic()
            print(json.dumps({'event': 'phase_begin', 'daemon_pid': daemon.pid, 'count': count,
                              'monotonic': begin}), flush=True)
            # Cover at least two five-second discovery periods, beyond resource-only sampling.
            time.sleep(10.2)
            sample, rows = wait_for(lambda: (value if (value := observed()) and value[0]['seq'] > previous else None))
            elapsed = time.monotonic() - begin
            print(json.dumps({'event': 'phase_end', 'daemon_pid': daemon.pid, 'count': count,
                              'monotonic': time.monotonic(), 'window_seconds': elapsed}), flush=True)
            assert len(rows) == count and all(len(row['directory_ids']) == 1 for row in rows)
            resources = {row['resource_id'] for row in rows}
            assert len(resources) == count
            measured = [row for row in sample['resources'] if row['resource_id'] in resources]
            assert len(measured) == count and all(row['rss_bytes'] is not None and row['cpu_percent_one_core'] is not None for row in measured)
            evidence['phases'].append({'count': count, 'seq': sample['seq'], 'instances': len(rows),
                                       'distinct_resources': len(resources), 'window_seconds': elapsed,
                                       'rss_sum_bytes': sum(row['rss_bytes'] for row in measured),
                                       'cpu_sum_one_core_percent': sum(row['cpu_percent_one_core'] for row in measured)})
        client.close_client()
        assert client.gate.worker_done.wait(2), 'worker_not_done'
        client.gate.reap_retired()
        assert client.gate.retired is None and client.detached.is_set(), 'cleanup_pending'
        evidence['client_cleanup'] = {'worker_done': True, 'retired': False, 'detached': True}
        assert daemon.poll() is None and all(child.poll() is None for child in children)
        duplicate = b'{"method":"status","method":"status"}'
        evidence['invalid_duplicate'] = raw_boundary(profile, struct.pack('<I', len(duplicate)) + duplicate)['reason']
        evidence['oversize'] = raw_boundary(profile, struct.pack('<I', 65537))['reason']
        evidence['slow_partial'] = raw_boundary(profile, struct.pack('<I', 50) + b'{')['reason']
        assert evidence['oversize'] == 'frame_size' and evidence['slow_partial'] == 'pipe_timeout'
        assert evidence['invalid_duplicate'] == 'invalid_message'
    except Exception as error:
        evidence['errors'].append(type(error).__name__ + ':' + str(error))
    finally:
        if client:
            client.close_client()
        if daemon:
            try:
                stopped = subprocess.run([str(binary), 'stop', '--data-dir', str(profile)], capture_output=True,
                                         text=True, encoding='utf-8', timeout=5, creationflags=0x08000000)
                evidence['stop_exit'] = stopped.returncode
            except subprocess.TimeoutExpired:
                evidence['errors'].append('owned_stop_deadline')
            try:
                daemon.wait(timeout=4)
            except subprocess.TimeoutExpired:
                evidence['errors'].append('owned_daemon_forced_cleanup')
                daemon.kill()
                daemon.wait(timeout=3)
            evidence['daemon_exit'] = daemon.returncode
        for child in children:
            try:
                child.stdin.write(b'fixture-stop\n')
                child.stdin.flush()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                evidence['errors'].append('owned_fixture_forced_cleanup')
                child.kill()
                child.wait(timeout=3)
            finally:
                child.stdin.close()
            evidence['cleanup'].append({'pid': child.pid, 'exit_code': child.returncode})
            if child.returncode != 0:
                evidence['errors'].append('owned_fixture_nonzero_exit')
        if client:
            if not client.gate.worker_done.wait(2):
                evidence['errors'].append('worker_not_done')
            client.gate.reap_retired()
            if client.gate.retired is not None:
                evidence['errors'].append('cleanup_pending')
        evidence['work_directory'] = str(work.relative_to(ROOT))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(evidence))
    return 1 if evidence['errors'] or evidence.get('daemon_exit') != 0 or evidence.get('stop_exit') != 0 else 0


if __name__ == '__main__':
    raise SystemExit(main())
