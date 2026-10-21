"""Sandbox preparation transport; gateway retains browser authorization."""
from urllib.parse import parse_qs
from pathlib import Path

from control_everything.execution import sandbox_preparations
from control_everything.input_validation import ProfileError


def get(workspace, profile, project_id, query):
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    if set(values) - {'directory_id'} or any(len(value) != 1 for value in values.values()):
        raise ProfileError('invalid_query')
    directory_id = values.get('directory_id', ['main'])[0]
    path = workspace.project_directory(project_id, directory_id)
    return sandbox_preparations.status(profile.data_dir, project_id, path, directory_id=directory_id)


def post(workspace, profile, project_id, body):
    if not isinstance(body, dict) or body.get('confirmed') is not True:
        raise ProfileError('invalid_request')
    required = {'confirmed', 'path', 'directory_id', 'confirmation_hash'}
    if set(body) != required or any(not isinstance(body.get(key), str) for key in required - {'confirmed'}):
        raise ProfileError('directory_changed', status=409)
    if workspace.project_has_active_execution(project_id):
        raise ProfileError('project_busy', status=409)
    directory_id = body['directory_id']
    path = workspace.project_directory(project_id, directory_id)
    if body['path'] != str(Path(path).resolve()):
        raise ProfileError('directory_changed', status=409)
    # prepare rechecks the fingerprint after acquiring the shared execution lease.
    return sandbox_preparations.prepare(profile.data_dir, project_id, path,
        directory_id=directory_id, confirmation_hash=body['confirmation_hash'])
