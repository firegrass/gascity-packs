"""Versioned, lossless PROOF records carried by ordinary durable Beads issues."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import hashlib
import json
import os
import re

import proof_work_policy as policy
from execution_contract import digest, legacy_semantic, semantic

SCHEMA = 'proof.work.v1'
TERMINAL = policy.TERMINAL_STATUSES

class Refused(ValueError):
    pass


def bead_id(legacy_id):
    if not isinstance(legacy_id, str) or not legacy_id:
        raise Refused('missing legacy identity')
    return 'pw-' + hashlib.sha256(legacy_id.encode()).hexdigest()[:32]


@dataclass
class Record:
    path: str
    fm: dict
    body: str

    @property
    def filename(self):
        return PurePosixPath(self.path).name

    @property
    def status(self):
        return self.fm.get('status')

    @property
    def doc_type(self):
        return self.fm.get('doc_type')


class Corpus:
    """Storage-free policy input with the same selectors as the Git adapter."""
    def __init__(self, records, root='/work'):
        self.root = root
        self.by_id = {}
        self.records = {}
        for row in records:
            path = row['path']
            if not path.startswith('docs/') or '..' in PurePosixPath(path).parts:
                raise Refused('unsafe legacy path')
            record = Record(os.path.join(root, path), row['fields'], row['body'])
            rid = policy.record_id(record)
            if rid != row['id'] or rid in self.by_id:
                raise Refused('duplicate or inconsistent identity: ' + row['id'])
            self.by_id[rid] = record
            self.records[record.path] = record

    def of_type(self, kind):
        return [r for r in self.records.values() if r.doc_type == kind]

    items = property(lambda self: self.of_type('backlog'))
    epics = property(lambda self: self.of_type('epic'))
    questions = property(lambda self: self.of_type('question'))
    milestones = property(lambda self: sorted(self.of_type('milestone'), key=policy.milestone_sequence))

    def epic(self, rid):
        r = self.by_id.get(rid)
        return r if r is not None and r.doc_type == 'epic' else None

    def milestone(self, rid):
        r = self.by_id.get(rid)
        return r if r is not None and r.doc_type == 'milestone' else None

    def children(self, rid, active_only=True):
        return [r for r in self.items if r.fm.get('parent_epic') == rid
                and (not active_only or r.status in policy.ACTIVE_STATUSES)]


def timestamp(value):
    if not isinstance(value, str):
        raise Refused('invalid source timestamp')
    result = value + 'T00:00:00Z' if policy.DATE_RE.fullmatch(value) else value
    if policy.parse_timestamp(result) is None:
        raise Refused('invalid source timestamp: ' + value)
    return result


def validate_row(row):
    required = {'schema', 'id', 'path', 'fields', 'body', 'authored_revision',
                'operational_revision', 'semantic_digest', 'source_sha', 'source_text'}
    if not required <= row.keys() or row['schema'] != SCHEMA:
        raise Refused('unsupported work metadata schema')
    fm = row['fields']
    if not isinstance(fm, dict) or any(not isinstance(k, str) or
            not (isinstance(v, str) or isinstance(v, list) and all(isinstance(x, str) for x in v))
            for k, v in fm.items()):
        raise Refused('unsupported legacy metadata shape')
    list_fields = {'source_documents', 'related_repos', 'depends_on', 'related', 'reads',
                   'write_scope', 'validation', 'evidence', 'lanes', 'related_backlog',
                   'v5_evidence_ids', 'related_pull_requests'}
    for key in list_fields & fm.keys():
        if not isinstance(fm[key], list):
            raise Refused('field must be a list: ' + key)
    for key in ('title', 'status', 'created', 'last_reviewed', 'doc_type'):
        if not isinstance(fm.get(key), str) or not fm[key]:
            raise Refused('missing scalar field: ' + key)
    if fm.get('doc_type') not in ('backlog', 'epic', 'milestone', 'question'):
        raise Refused('invalid work record type')
    if fm.get('status') not in policy.ACTIVE_STATUSES | TERMINAL:
        raise Refused('invalid domain resolution')
    if not isinstance(row['body'], str) or not isinstance(row['source_text'], str):
        raise Refused('invalid record body')
    for name in ('authored_revision', 'operational_revision'):
        if type(row[name]) is not int or row[name] < (1 if name == 'authored_revision' else 0):
            raise Refused('invalid ' + name)
    if row['semantic_digest'] not in (digest(semantic(fm, row['body'])), digest(legacy_semantic(fm, row['body']))):
        raise Refused('semantic digest mismatch: ' + row['id'])
    scopes = row.get('reserved_scopes', [])
    if not isinstance(scopes, list) or any(not isinstance(scope, list) or len(scope) != 2
            or not all(isinstance(value, str) for value in scope) for scope in scopes):
        raise Refused('invalid retained reservation')
    Corpus([row])


def encode(row):
    validate_row(row)
    fm = row['fields']
    status = 'closed' if fm['status'] in TERMINAL else 'in_progress' if fm['status'] == 'working' else 'open'
    issue = dict(id=bead_id(row['id']), title=fm['title'], description=row['body'],
                 issue_type='epic' if fm['doc_type'] in ('epic', 'milestone') else 'task',
                 status=status, priority=policy.PRIORITY_RANK.get(fm.get('priority'), 2),
                 created_at=timestamp(fm['created']), updated_at=timestamp(fm['last_reviewed']),
                 assignee=fm.get('claimed_by', ''), external_ref=row['id'],
                 metadata={'proof': row}, ephemeral=False, no_history=False, storage_class='versioned')
    if status == 'closed':
        issue['closed_at'] = timestamp(fm.get('done_at', fm['last_reviewed']))
        issue['close_reason'] = fm['status']
    return issue


def decode(issue):
    row = (issue.get('metadata') or {}).get('proof')
    if not isinstance(row, dict) or row.get('schema') != SCHEMA:
        if issue.get('id', '').startswith('pw-') and not issue.get('id', '').startswith(('pw-op-', 'pw-event-', 'pw-coordination')):
            raise Refused('missing or unsupported project metadata')
        return None
    expected = encode(row)
    for key in ('id', 'title', 'description', 'status', 'priority', 'issue_type', 'external_ref', 'assignee'):
        if issue.get(key, '') != expected.get(key, ''):
            raise Refused('native/PROOF drift: ' + issue['id'] + ':' + key)
    if (issue.get('ephemeral') or issue.get('no_history') or issue.get('compaction_level', 0)
            or issue.get('storage_class', 'versioned') != 'versioned'):
        raise Refused('project history retention drift: ' + issue['id'])
    # Every reader sees the current contract: a row stamped under the legacy
    # formula is served, compared and (on its next write) stored under this one.
    row['semantic_digest'] = digest(semantic(row['fields'], row['body']))
    return row


def relationships(rows):
    """Only native blocks edges affect prerequisite readiness.

    Beads parent-child has scheduling meaning, so membership stays in validated
    metadata; milestones and membership are nonblocking related edges. A pair
    can carry one native edge, with blocks taking precedence over related.
    """
    known = {r['id'] for r in rows}
    edges = {}
    for row in rows:
        fm = row['fields']
        for target in (fm.get('parent_epic'), fm.get('milestone'), *fm.get('related', [])):
            if target in known and target != row['id']:
                edges[(bead_id(row['id']), bead_id(target))] = 'relates-to'
        for target in fm.get('depends_on', []):
            if target not in known:
                raise Refused('unknown prerequisite ' + target)
            edges[(bead_id(row['id']), bead_id(target))] = 'blocks'
    return [dict(issue_id=a, depends_on_id=b, type=kind) for (a, b), kind in sorted(edges.items())]


def validate_corpus(rows, now):
    corpus = Corpus(rows)
    if policy.dependency_cycles(corpus.by_id):
        raise Refused('dependency cycle')
    identities = {}
    for row in rows:
        validate_row(row)
        bid = bead_id(row['id'])
        if bid in identities:
            raise Refused('native identity collision')
        identities[bid] = row['id']
        r = corpus.by_id[row['id']]
        problems = []
        if r.doc_type == 'backlog':
            problems = policy.claim_lifecycle_problems(r, now) + policy.membership_problems(corpus, r)
        elif r.doc_type == 'epic':
            problems = policy.epic_lifecycle_problems(corpus, r)
        if problems:
            raise Refused(row['id'] + ': ' + '; '.join(problems))
        if r.doc_type in ('backlog', 'epic') and r.status in policy.ACTIVE_STATUSES:
            milestone = corpus.milestone(r.fm.get('milestone'))
            if milestone is None or r.fm.get('lane') not in milestone.fm['lanes']:
                raise Refused('invalid active milestone lane')
    relationships(rows)
    return corpus
