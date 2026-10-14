#!/usr/bin/env python3
"""Bounded, GET-only production evidence for the on-demand PROOF SRE role.

No arbitrary command, URL, query, mutation, retry loop, or credential output.
Existing operator credentials are used; this is not credential-level isolation.
"""
import argparse
import configparser
import re
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.liskov.proof.computer/readyz'
CONSOLE = 'https://console.liskov.proof.computer'
SENTRY = 'https://de.sentry.io/api/0/organizations/moose-labs-ltd/issues/'
PROJECT = '4511647545098320'
QUERY = 'lastSeen:-24h'
LIMIT = 20
MAX_BYTES = 4 * 1024 * 1024


class ReadbackError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReadbackError('redirect refused')


def now():
    return datetime.now(timezone.utc).isoformat()


def get_json(url, token=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'proof-sre-readback/1'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ReadbackError('response size limit exceeded')
            return json.loads(body), response.headers.get('Link', '')
    except urllib.error.HTTPError as exc:
        # Never copy a server error body or credential into evidence.
        raise ReadbackError(f'HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise ReadbackError('network failure or timeout') from None


def proof_json(command):
    # Only these two commands exist on this surface, even for Python callers.
    if command not in (('whoami',), ('application', 'list')):
        raise ReadbackError('Proof command is not allowlisted')
    binary = shutil.which('proof')
    if binary is None or shutil.which('node') is None:
        raise ReadbackError('proof and node must both be on PATH')
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'XDG_CONFIG_HOME') if key in os.environ}
    argv = [binary, 'liskov', *command, '--no-analytics', '--json']
    try:
        result = subprocess.run(argv, env=env, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise ReadbackError('Proof command timed out') from None
    if result.returncode != 0:
        raise ReadbackError(f'Proof command exited {result.returncode}; stderr withheld')
    if len(result.stdout) > MAX_BYTES:
        raise ReadbackError('Proof response size limit exceeded')
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise ReadbackError('Proof returned an unsuccessful response')
    return value


def health():
    value, _ = get_json(API)
    if not isinstance(value, dict):
        raise ReadbackError('unexpected readiness response')
    return {'url': API, 'http_status': 200, 'readiness': value}


def applications():
    identity = proof_json(('whoami',))
    if identity.get('slipwayUrl', '').rstrip('/') != CONSOLE:
        raise ReadbackError('Proof session is not bound to the production console')
    value = proof_json(('application', 'list'))
    rows = value.get('applications')
    if not isinstance(rows, list):
        raise ReadbackError('missing applications array')
    # Exclude credentials, session contents, manifests, source and user identity.
    selected = [{key: row.get(key) for key in ('applicationUid', 'applicationName',
                  'status', 'applicationPosture')} for row in rows]
    org = identity.get('organization') or {}
    return {'source': 'proof liskov application list --no-analytics --json',
            'organization': {k: org.get(k) for k in ('id', 'slug', 'role')},
            'scope': 'applications visible to the current CLI organization, not the fleet',
            'count': value.get('count'), 'applications': selected}


def sentry(start=None, end=None):
    config = configparser.ConfigParser(interpolation=None)
    config.read(Path.home() / '.sentryclirc')
    token = config.get('auth', 'token', fallback=None)
    if not token:
        raise ReadbackError('Sentry token missing from ~/.sentryclirc [auth]')
    params = {'project': PROJECT, 'query': QUERY, 'statsPeriod': '24h',
              'sort': 'date', 'per_page': str(LIMIT)}
    if start is not None:
        # Use event timestamps for historical windows, not current issue lastSeen.
        params = {'project': PROJECT, 'query': '', 'start': start, 'end': end,
                  'sort': 'date', 'per_page': str(LIMIT)}
    rows, links = get_json(SENTRY + '?' + urllib.parse.urlencode(params), token)
    if not isinstance(rows, list):
        raise ReadbackError('unexpected Sentry issue response')
    selected = [{k: row.get(k) for k in ('shortId', 'title', 'level', 'status',
                  'firstSeen', 'lastSeen', 'count', 'permalink')} for row in rows]
    return {'project': PROJECT, 'query': params['query'],
            'window': {'start': start, 'end': end} if start is not None else '24h',
            'environment': 'all environments; environment attribution not established',
            'limit': LIMIT, 'more_results': any('rel="next"' in link and 'results="true"' in link
                                             for link in links.split(',')),
            'count_note': 'issue count is lifetime, not occurrences in this window',
            'issues': selected}


def collect(start=None, end=None, packet=None, expected_build=None):
    result = {'schema': 'proof.sre.readback.v1', 'started_at': now(),
              'credential_boundary': 'existing operator credentials; allowlisted reads, not OS or credential isolation',
              'purpose': 'packet-closeout' if packet else 'investigation',
              'packet': packet, 'expected_build': expected_build,
              'historical_scope': 'Sentry window only; API and CLI are current observations',
              'observations': {}}
    for name, function in (('api', health), ('proof', applications), ('sentry', lambda: sentry(start, end))):
        observation = {'observed_at': now()}
        try:
            observation.update(ok=True, data=function())
        except (ReadbackError, ValueError, OSError, KeyError, TypeError, configparser.Error) as exc:
            # Only deliberately constructed ReadbackError messages are printable.
            observation.update(ok=False, error=str(exc) if isinstance(exc, ReadbackError)
                               else 'invalid response or local configuration: ' + type(exc).__name__)
        result['observations'][name] = observation
    if packet is not None:
        api = result['observations']['api']
        observed_build = api.get('data', {}).get('readiness', {}).get('build')
        result['closeout'] = {
            'observed_api_build': observed_build,
            'api_build_matches': observed_build == expected_build if api['ok'] else None,
            'acceptance': 'not evaluated; packet-specific assertions require owner review',
            'authority': 'evidence only; no item completion or gate closure',
        }
    result['finished_at'] = now()
    result['complete'] = all(v['ok'] for v in result['observations'].values())
    return result


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', help='Sentry historical interval start, timezone-qualified ISO 8601')
    parser.add_argument('--end', help='Sentry historical interval end, timezone-qualified ISO 8601')
    parser.add_argument('--packet', help='Evidence binding: BKLG-YYYYMMDD-xxxx; does not complete it')
    parser.add_argument('--expected-build', help='Full 40-hex deployed API build SHA for closeout')
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None):
        parser.error('--start and --end must be supplied together')
    if args.start is not None:
        try:
            dates = [datetime.fromisoformat(v.replace('Z', '+00:00')) for v in (args.start, args.end)]
            if any(d.tzinfo is None for d in dates):
                raise ValueError()
            if not dates[0] < dates[1] <= datetime.now(timezone.utc):
                raise ValueError()
            if (dates[1] - dates[0]).total_seconds() > 14 * 86400:
                raise ValueError()
            args.start, args.end = [d.astimezone(timezone.utc).isoformat() for d in dates]
        except ValueError:
            parser.error('historical interval must be timezone-qualified, ordered, past, and at most 14 days')
    if (args.packet is None) != (args.expected_build is None):
        parser.error('--packet and --expected-build must be supplied together')
    if args.packet is not None:
        if not re.fullmatch(r'BKLG-[0-9]{8}-[a-z0-9]{4}', args.packet):
            parser.error('invalid packet ID')
        if not re.fullmatch(r'[0-9a-f]{40}', args.expected_build):
            parser.error('expected build must be a full lowercase 40-hex SHA')
    return args


def main():
    args = arguments()
    result = collect(args.start, args.end, args.packet, args.expected_build)
    print(json.dumps(result, indent=2))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
