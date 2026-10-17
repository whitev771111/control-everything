"""Exercise actual import contracts on isolated copies of publication sources."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def source_files():
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=ROOT, capture_output=True, check=True)
    names = []
    for name in sorted(set(result.stdout.decode('utf-8').split('\0'))):
        if not name.startswith('src/session_panel/') or not name.endswith('.py'):
            continue
        path = ROOT.joinpath(*PurePosixPath(name).parts)
        if not path.is_file():
            continue
        cursor = path
        while cursor != ROOT:
            if cursor.is_symlink() or cursor.is_junction():
                raise ValueError('linked_publication_source')
            cursor = cursor.parent
        names.append(name)
    if not names:
        raise ValueError('publication_sources_missing')
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--linter', default=shutil.which('lint-imports'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.linter:
        parser.error('Prepare import-linter==2.15 explicitly; no implicit installation is performed')
    linter = str(Path(args.linter).resolve(strict=True))
    version = subprocess.run([linter, '--version'], capture_output=True, text=True, encoding='utf-8', check=True)
    if version.stdout.strip() != 'import-linter 2.15':
        raise ValueError('import_linter_2_15_required')
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'work'):
        raise ValueError('evidence_must_stay_in_work')
    output.mkdir(parents=True, exist_ok=False)
    config = ROOT / 'tools/architecture/importlinter.ini'
    names = source_files()
    cases = [
        ('legal', {}, 0),
        ('direct', {'src/session_panel/workspace/service.py': '\nfrom session_panel.profiles import profile\n'}, 1),
        ('function_local', {'src/session_panel/workspace/service.py': '\ndef _architecture_probe():\n    from session_panel.profiles import profile\n'}, 1),
        ('descendant', {'src/session_panel/workspace/architecture_probe.py': 'from session_panel.profiles import profile\n'}, 1),
        ('indirect', {'src/session_panel/workspace/service.py': '\nfrom session_panel.sessions import architecture_probe\n',
                      'src/session_panel/sessions/architecture_probe.py': 'from session_panel.profiles import profile\n'}, 1),
        ('legal_cross_module', {'src/session_panel/profiles/profile_import.py': '\nfrom session_panel.workspace.validation import finite_number as _architecture_probe\n'}, 0),
        ('installation_direct', {'src/session_panel/installation/registry.py': '\nfrom session_panel.profiles import profile\n'}, 1),
        ('installation_function_local', {'src/session_panel/sessions/observer_client.py': '\ndef _installation_probe():\n    from session_panel.installation import registry\n'}, 1),
        ('installation_descendant', {'src/session_panel/platform/architecture_probe.py': 'from session_panel.installation import registry\n'}, 1),
        ('installation_indirect', {'src/session_panel/installation/registry.py': '\nfrom session_panel.sessions import architecture_probe\n',
                                   'src/session_panel/sessions/architecture_probe.py': 'from session_panel.profiles import profile\n'}, 1),
        ('manifest_business', {'src/session_panel/installation/manifest.py': '\nfrom session_panel.sessions import observer_protocol\n'}, 1),
        ('registry_orchestration', {'src/session_panel/installation/registry.py': '\nfrom session_panel.installation import maintenance\n'}, 1),
        ('legal_installation_identity', {'src/session_panel/installation/registry.py': '\nfrom session_panel.sessions.observer_protocol import profile_key as _architecture_probe\n'}, 0),
    ]
    rows = []
    for label, changes, expected in cases:
        case = output / label
        case.mkdir()
        for name in names:
            target = case / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for name, text in changes.items():
            with (case / name).open('a', encoding='utf-8') as stream:
                stream.write(text)
        environment = os.environ.copy()
        environment.pop('PYTHONHOME', None)
        environment['PYTHONPATH'] = str(case / 'src')
        result = subprocess.run([linter, '--config', str(config), '--no-cache', '--no-logo'],
                                cwd=case, env=environment, capture_output=True, encoding='utf-8')
        log = result.stdout + result.stderr
        (output / (label + '.log')).write_bytes(log.encode('utf-8'))
        rows.append({'case': label, 'expected_exit': expected, 'exit': result.returncode,
                     'log_sha256': hashlib.sha256(log.encode('utf-8')).hexdigest()})
        print(f'{label}: expected={expected} actual={result.returncode}\n{log}')
        if result.returncode != expected or (expected == 1 and (
                'BROKEN' not in log or ('session_panel.installation' if label.startswith(('installation_', 'manifest_', 'registry_'))
                                       else 'session_panel.workspace') not in log)):
            (output / 'receipt.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
            return 1
    (output / 'receipt.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
