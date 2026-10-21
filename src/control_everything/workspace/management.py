"""Local decision records and explicit human reviews in the management transaction."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from control_everything.input_validation import ProfileError
from control_everything.sessions.source import canonical_uuid
from control_everything.workspace import agent_runs
from control_everything.workspace import dispatch as dispatch_module
from control_everything.workspace.store import WorkspaceError, now_utc
from control_everything.workspace.task_schema import MAX_REVISION
from control_everything.workspace.validation import project_id_valid


DECISION_FIELDS = {'title', 'decision', 'rationale', 'alternatives', 'status', 'links'}
DECISION_STATES = ('open', 'accepted', 'superseded')
REVIEW_STATES = {'approved': 'done', 'changes_requested': 'doing', 'rejected': 'todo'}


def _text(value, field, reason, maximum=2000, *, required=False):
    if (not isinstance(value, str) or len(value) > maximum or '\x00' in value
            or required and not value.strip()):
        raise ProfileError(reason, field)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ProfileError(reason, field) from None
    return value


def _identity(value, field, reason):
    try:
        return canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise ProfileError(reason, field) from None


def _strings(value, field, reason):
    if not isinstance(value, list) or len(value) > 20:
        raise ProfileError(reason, field)
    return [_text(item, field, reason, 2048, required=True) for item in value]


def _page(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ProfileError('invalid_query')


def _project_filter(project_id):
    if project_id is not None and not project_id_valid(project_id):
        raise ProfileError('invalid_query', 'project_id')


def validate_item_flags(fields):
    for key, allowed in (('priority', (0, 1, 2)), ('focus', (0, 1))):
        if key not in fields or key == 'priority' and fields[key] is None:
            continue
        if type(fields[key]) is not int or fields[key] not in allowed:
            raise ProfileError('invalid_item', key)


def validate_decision_body(body):
    if (not isinstance(body, dict) or not {'project_id', 'title', 'confirmed'} <= set(body)
            or set(body) - (DECISION_FIELDS | {'project_id', 'confirmed'})
            or body['confirmed'] is not True):
        raise ProfileError('invalid_decision')
    if not project_id_valid(body['project_id']):
        raise ProfileError('invalid_decision', 'project_id')


def _decision_fields(fields):
    for key in ('title', 'decision', 'rationale', 'alternatives'):
        if key in fields:
            _text(fields[key], key, 'invalid_decision', 160 if key == 'title' else 4000,
                  required=key == 'title')
    if 'links' in fields:
        _strings(fields['links'], 'links', 'invalid_decision')
    if 'status' in fields and fields['status'] not in DECISION_STATES:
        raise ProfileError('invalid_decision', 'status')


def _decision(row):
    result = dict(row)
    result['links'] = json.loads(result.pop('links_json'))
    return result


def save_decision(store, body, *, update=False):
    if update:
        if (not isinstance(body, dict) or set(body) != {'id', 'expected_updated_at', 'changes', 'confirmed'}
                or body['confirmed'] is not True):
            raise ProfileError('invalid_decision')
        identity = _identity(body['id'], 'id', 'invalid_decision')
        _text(body['expected_updated_at'], 'expected_updated_at', 'invalid_decision', 80, required=True)
        fields = body['changes']
        if not isinstance(fields, dict) or not fields or set(fields) - DECISION_FIELDS:
            raise ProfileError('invalid_decision', 'changes')
    else:
        validate_decision_body(body)
        identity = str(uuid4())
        fields = {key: body[key] for key in DECISION_FIELDS if key in body}
        fields = {'decision': '', 'rationale': '', 'alternatives': '', 'status': 'open', 'links': [], **fields}
        if fields['status'] == 'superseded':
            raise ProfileError('invalid_decision', 'status')
    _decision_fields(fields)
    with store.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        stamp = now_utc()
        if update:
            row = connection.execute('SELECT * FROM project_decisions WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ProfileError('decision_not_found', status=404)
            old = _decision(row)
            if old['updated_at'] != body['expected_updated_at']:
                raise ProfileError('decision_changed', status=409)
            changed = {key: value for key, value in fields.items() if old[key] != value}
            # Accepted decisions keep their wording; a replacement is a new record.
            if old['status'] != 'open' and (set(changed) - {'status'} or
                    changed.get('status', old['status']) not in (old['status'], 'superseded')):
                raise ProfileError('decision_closed', status=409)
            if not changed:
                return {'decision': old}
            merged = {**old, **changed}
            # A strictly newer timestamp also works as a CAS token after clock rollback.
            try:
                previous = datetime.fromisoformat(old['updated_at'])
            except ValueError:
                previous = None
            try:
                if previous is not None and previous.tzinfo is not None and previous >= datetime.fromisoformat(stamp):
                    stamp = (previous + timedelta(microseconds=1)).isoformat()
            except OverflowError:
                raise ProfileError('decision_changed', status=409) from None
        else:
            # Recheck project ownership on the same connection as the write.
            if connection.execute('SELECT 1 FROM projects WHERE id=? AND archived=0',
                                  (body['project_id'],)).fetchone() is None:
                raise ProfileError('invalid_decision', 'project_id')
            merged = {'id': identity, 'project_id': body['project_id'], 'created_at': stamp, **fields}
        if merged['status'] == 'accepted' and not merged['decision'].strip():
            raise ProfileError('invalid_decision', 'decision')
        merged['updated_at'] = stamp
        values = (merged['title'], merged['decision'], merged['rationale'], merged['alternatives'],
                  merged['status'], json.dumps(merged['links'], ensure_ascii=False), stamp)
        if update:
            connection.execute('''UPDATE project_decisions SET title=?,decision=?,rationale=?,alternatives=?,
                                  status=?,links_json=?,updated_at=? WHERE id=?''', (*values, identity))
        else:
            connection.execute('''INSERT INTO project_decisions(id,project_id,title,decision,rationale,
                alternatives,status,links_json,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                               (identity, merged['project_id'], *values, stamp))
        return {'decision': merged}


def list_decisions(store, *, offset=0, limit=50, project_id=None, status=None):
    _page(offset, limit)
    _project_filter(project_id)
    if status is not None and status not in DECISION_STATES:
        raise ProfileError('invalid_query', 'status')
    where, args = [], []
    for key, value in (('project_id', project_id), ('status', status)):
        if value is not None:
            where.append(key + '=?')
            args.append(value)
    source = ' FROM project_decisions' + (' WHERE ' + ' AND '.join(where) if where else '')
    with store.connection() as connection:
        connection.execute('BEGIN')
        total = connection.execute('SELECT COUNT(*)' + source, args).fetchone()[0]
        rows = connection.execute('SELECT *' + source + ' ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                                  (*args, limit, offset)).fetchall()
        return {'items': [_decision(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


def save_review(store, body):
    required = {'task_id', 'expected_revision', 'verdict', 'reason', 'confirmed'}
    if (not isinstance(body, dict) or not required <= set(body)
            or set(body) - required - {'evidence', 'reviewer', 'run_id'} or body['confirmed'] is not True):
        raise ProfileError('invalid_review')
    identity = _identity(body['task_id'], 'task_id', 'invalid_review')
    revision = body['expected_revision']
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise ProfileError('invalid_review', 'expected_revision')
    if not isinstance(body['verdict'], str) or body['verdict'] not in REVIEW_STATES:
        raise ProfileError('invalid_review', 'verdict')
    _text(body['reason'], 'reason', 'invalid_review', required=True)
    evidence = _strings(body.get('evidence', []), 'evidence', 'invalid_review')
    for key, maximum in (('reviewer', 80), ('run_id', 160)):
        if body.get(key) is not None:
            _text(body[key], key, 'invalid_review', maximum, required=True)
    with store.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM task_items WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise WorkspaceError('item_not_found')
        old = dict(row)
        if old['revision'] != revision:
            raise WorkspaceError('item_changed')
        if old['state'] != 'review':
            raise ProfileError('review_state_conflict', status=409)
        managed = connection.execute(
            "SELECT ar.id,ar.status,d.id AS dispatch_id,d.external_ref,d.delivery_mode FROM agent_runs ar "
            "JOIN task_dispatches d ON d.id=ar.dispatch_id WHERE ar.task_id=? "
            "AND ar.status IN ('claimed','running','waiting_review') LIMIT 1", (identity,)).fetchone()
        effective_run_id = body.get('run_id')
        if managed is not None and managed['delivery_mode'] == 'auto' and managed['external_ref']:
            binding = dispatch_module.execution_binding(
                connection, dispatch_id=managed['dispatch_id'],
                execution_run_id=managed['external_ref'], task_id=identity)
            if binding['agent_run_id'] != managed['id']:
                raise ProfileError('execution_review_conflict', 'binding', status=409)
            execution = connection.execute("SELECT state,cleanup_state,task_id,request_id FROM execution_runs WHERE id=?",
                                           (managed['external_ref'],)).fetchone()
            if (managed['status'] != 'waiting_review' or execution is None
                    or execution['state'] != 'completed' or execution['cleanup_state'] != 'verified'
                    or execution['task_id'] != identity or execution['request_id'] != managed['dispatch_id']):
                raise ProfileError('execution_review_conflict', status=409)
            if body.get('run_id') is not None and body['run_id'] != managed['id']:
                raise ProfileError('execution_review_conflict', 'run_id', status=409)
            effective_run_id = managed['id']
        item = store._update_item(connection, old, {'state': REVIEW_STATES[body['verdict']]}, revision)
        review = dict(id=str(uuid4()), task_id=identity, run_id=effective_run_id, verdict=body['verdict'],
                      reason=body['reason'], evidence=evidence, reviewer=body.get('reviewer'), created_at=now_utc())
        connection.execute('''INSERT INTO task_reviews(id,task_id,run_id,verdict,reason,evidence_json,reviewer,
                              created_at) VALUES(?,?,?,?,?,?,?,?)''',
                           (review['id'], identity, review['run_id'], review['verdict'], review['reason'],
                            json.dumps(evidence, ensure_ascii=False), review['reviewer'], review['created_at']))
        agent_runs.close_run(connection, identity, body['verdict'])
        return {'review': review, 'item': item}


def list_reviews(store, *, offset=0, limit=50, task_id=None, project_id=None, verdict=None):
    _page(offset, limit)
    _project_filter(project_id)
    if task_id is not None:
        task_id = _identity(task_id, 'task_id', 'invalid_query')
    if verdict is not None and (not isinstance(verdict, str) or verdict not in REVIEW_STATES):
        raise ProfileError('invalid_query', 'verdict')
    where, args = [], []
    for key, value in (('r.task_id', task_id), ('t.project_id', project_id), ('r.verdict', verdict)):
        if value is not None:
            where.append(key + '=?')
            args.append(value)
    source = (' FROM task_reviews r JOIN task_items t ON r.task_id=t.id'
              + (' WHERE ' + ' AND '.join(where) if where else ''))
    with store.connection() as connection:
        connection.execute('BEGIN')
        total = connection.execute('SELECT COUNT(*)' + source, args).fetchone()[0]
        rows = connection.execute('SELECT r.*' + source + ' ORDER BY r.created_at DESC,r.id LIMIT ? OFFSET ?',
                                  (*args, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['evidence'] = json.loads(item.pop('evidence_json') or '[]')
            items.append(item)
        return {'items': items, 'total': total, 'offset': offset, 'limit': limit}
