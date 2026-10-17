"""Check file ownership for explicit or Git publication candidates."""

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitattributes', '.gitignore', 'AGENTS.md', 'CONTRIBUTING.md', 'README.md',
              'requirements.txt', 'rust-toolchain.toml', 'pyproject.toml', 'MANIFEST.in', 'setup.py'}
PACKAGE_FILES = {'__init__.py', 'paths.py', 'input_validation.py', 'gateway.py',
                 'profile.py', 'native_processes.py', 'mcp_stdio.py'}
PACKAGES = {'http', 'sessions', 'workspace', 'execution', 'profiles', 'integrations', 'platform', 'installation'}


def ownership_error(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        return 'invalid_relative_path'
    path = PurePosixPath(value)
    parts = path.parts
    if not parts or path.is_absolute() or '..' in parts or path.as_posix() != value or any(
            not part or part.endswith((' ', '.')) or any(ord(char) < 32 for char in part) for part in parts):
        return 'invalid_relative_path'
    if len(parts) == 1:
        return None if value in ROOT_FILES else 'root_file_has_no_owner'
    top, section = parts[:2]
    if top == 'src':
        if section != 'session_panel' or len(parts) < 3:
            return 'unknown_backend_owner'
        if len(parts) == 3:
            return None if parts[2] in PACKAGE_FILES else 'package_root_is_for_entries_and_foundations'
        return None if parts[2] in PACKAGES and path.suffix == '.py' else 'unknown_backend_owner'
    if top == 'web':
        return None if section in {'sessions', 'progress', 'workspace'} and path.suffix in {
            '.html', '.css', '.js', '.ts'} else 'unknown_frontend_owner'
    if top == 'tests':
        return None if value == 'tests/__init__.py' or section in {
            'backend', 'frontend', 'e2e', 'fixtures'} else 'unknown_test_type'
    if top == 'tools':
        return None if section in {'typescript', 'remote-reader', 'architecture', 'packaging'} else 'unknown_development_tool'
    if top == 'examples':
        return None if section in {'integrations', 'legacy'} and path.suffix == '.json' else 'unknown_example_owner'
    if top == 'native':
        return None if section == 'observer' and path.suffix in {'.rs', '.toml', '.lock', '.json'} else 'unknown_native_owner'
    if top == 'scripts':
        return None if len(parts) == 2 and path.suffix in {'.py', '.cjs', '.ps1'} else 'scripts_are_development_entries'
    if top == '.github':
        return None if value in {'.github/CODEOWNERS', '.github/pull_request_template.md'} or (
            section == 'workflows' and len(parts) == 3 and path.suffix in {'.yml', '.yaml'}) else 'unknown_repository_automation'
    if value in {'.cargo/config.toml', '.codex/config.toml.example'}:
        return None
    return 'unregistered_top_level_directory'


def git_candidates(root=ROOT):
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=root, capture_output=True, check=True)
    # Deleted tracked paths do not belong to the resulting publication tree.
    return sorted({name for name in result.stdout.decode('utf-8').split('\0')
                   if name and root.joinpath(*PurePosixPath(name).parts).is_file()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['layout'])
    parser.add_argument('--files', type=Path, help='JSON array of explicit repository-relative candidate paths')
    args = parser.parse_args()
    try:
        files = json.loads(args.files.read_text(encoding='utf-8')) if args.files else git_candidates()
        if not isinstance(files, list) or not files:
            raise ValueError('nonempty_candidate_array_required')
        failures = [{'path': value, 'reason': reason} for value in files if (reason := ownership_error(value))]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'blocked', 'reason': type(error).__name__}))
        return 2
    print(json.dumps({'status': 'blocked' if failures else 'passed', 'candidates': len(files),
                      'failures': failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
