"""Keeper and daily-report notices reach the factory Slack channel (ADR-0237 §3).

A fake HTTP server stands in for gc's reverse proxy to the slack-channel
adapter. Nothing here talks to a live city or to Slack.
"""
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'assets/scripts'
sys.path.insert(0, str(SCRIPTS))
import factory_lib as fl  # noqa: E402
import factory_slack  # noqa: E402
import keeper  # noqa: E402
import report  # noqa: E402
import tokens  # noqa: E402
import watchdog  # noqa: E402

CHANNEL = 'C0TESTCHAN'
PUBLISH = '/v0/city/proof/svc/slack-channel/publish-to-channel'
CLEAN = {k: '' for k in ('LISKOV_FACTORY_SLACK_CHANNEL', 'SLACK_CHANNEL_ADAPTER_URL', 'GC_API_BASE_URL',
                         'GC_CITY_NAME', 'GC_CITY', 'GC_CITY_PATH', 'GC_SESSION_ID', 'PROOF_KEEPER_RECIPIENT')}


class Adapter:
    """Records every request; answers with `status` and `reply` after `delay`."""

    def __init__(self, status=200, reply=None, delay=0.0):
        self.status, self.delay = status, delay
        self.reply = {'ok': True, 'ts': '1792018193.991299', 'channel': CHANNEL} if reply is None else reply
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                size = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(size) if size else b''
                outer.requests.append(dict(method=self.command, path=self.path, headers={k.lower(): v for k, v in self.headers.items()},
                                           body=json.loads(raw) if raw else None))
                if outer.delay:
                    time.sleep(outer.delay)
                data = json.dumps(outer.reply).encode()
                try:
                    self.send_response(outer.status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass       # the client gave up (timeout test)

            do_GET = do_POST = do_PUT = do_DELETE = handle_any

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port_url():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    return 'http://127.0.0.1:%d' % port


def completed(stdout='', rc=0, stderr=''):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class FakeCity:
    """`gc` for the keeper: one rig, one packet whose workflow failed."""

    def __init__(self, mail_rc=0):
        self.mail_rc = mail_rc
        self.calls = []
        self.work = {'id': 'w1', 'status': 'open', 'created_at': '2026-10-14T10:00:00Z',
                     'metadata': {'proof.packet_id': 'BKLG-20261014-test', 'proof.branch': 'agent/test'}}
        root = {'id': 'wf1', 'status': 'closed', 'created_at': '2026-10-14T10:00:05Z',
                'metadata': {'gc.kind': 'workflow', 'gc.var.packet_id': 'BKLG-20261014-test', 'gc.outcome': 'fail'}}
        self.beads = [self.work, root]

    def sh(self, argv, cwd=None):
        self.calls.append(list(argv))
        if argv[:3] == ['gc', 'rig', 'list']:
            return completed(json.dumps({'rigs': [{'name': 'r1', 'path': ''}]}))
        if argv[:2] == ['gc', 'bd'] and 'list' in argv:
            return completed(json.dumps(self.beads))
        if argv[:3] == ['gc', 'mail', 'send']:
            return completed(rc=self.mail_rc, stderr='' if self.mail_rc == 0 else 'mail store unavailable')
        if argv[:2] == ['gc', 'bd'] and 'update' in argv:
            bead = next(b for b in self.beads if b['id'] == argv[argv.index('update') + 1])
            for n, arg in enumerate(argv):
                if arg == '--set-metadata':
                    key, _, value = argv[n + 1].partition('=')
                    bead['metadata'][key] = value
        return completed()

    def mails(self):
        return [c for c in self.calls if c[:3] == ['gc', 'mail', 'send']]


USAGE = {'w1': {'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 3,
                'cache_creation_input_tokens': 4, 'conversations': 1}}


def run_keeper(city, env):
    out, err = StringIO(), StringIO()
    with patch.dict(os.environ, {**CLEAN, **env}), patch.object(keeper, 'sh', city.sh), \
            patch.object(tokens, 'usage_by_admission', return_value=USAGE), \
            redirect_stdout(out), redirect_stderr(err):
        rc = keeper.main()
    return rc, out.getvalue(), err.getvalue()


NO_CLONE = 'keeper: no Gas City-owned clone of liskov-agent-orchestrator; skipping claim-expiry checks\n'


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.addCleanup(self.adapter.close)
        self.env = {'LISKOV_FACTORY_SLACK_CHANNEL': CHANNEL, 'GC_API_BASE_URL': self.adapter.url,
                    'GC_CITY_NAME': 'proof'}

    def test_configured_posts_each_notice_once_and_still_mails(self):
        city = FakeCity()
        rc, out, err = run_keeper(city, self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(city.mails()), 1)
        self.assertEqual(city.work['metadata']['proof.notified'], 'failed')
        self.assertEqual(len(self.adapter.requests), 1)
        req = self.adapter.requests[0]
        self.assertEqual((req['method'], req['path']), ('POST', PUBLISH))
        self.assertEqual(req['headers']['content-type'], 'application/json')
        self.assertEqual(req['headers']['x-gc-request'], 'gc-slack-channel')
        self.assertEqual(set(req['body']), {'channel_id', 'body', 'idempotency_key'})
        self.assertEqual(req['body']['channel_id'], CHANNEL)
        self.assertEqual(req['body']['idempotency_key'], 'proof-keeper/r1/w1/failed')
        mailed = city.mails()[0]
        subject, body = mailed[mailed.index('-s') + 1], mailed[mailed.index('-m') + 1]
        self.assertEqual(req['body']['body'], '*%s*\n```\n%s\n```' % (subject, body))
        self.assertIn('PROOF packet stopped: BKLG-20261014-test', req['body']['body'])
        self.assertIn("keeper: posted 'PROOF packet stopped: BKLG-20261014-test' to Slack channel %s "
                      "(ts 1792018193.991299)" % CHANNEL, out)
        self.assertEqual(err, NO_CLONE)

        # A second pass re-mails nothing, so it re-posts nothing.
        rc, out, err = run_keeper(city, self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(city.mails()), 1)
        self.assertEqual(len(self.adapter.requests), 1)
        self.assertEqual(out, '')

    def test_a_notice_whose_mail_failed_is_posted_with_the_mail_next_pass(self):
        city = FakeCity(mail_rc=1)
        rc, _out, err = run_keeper(city, self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(self.adapter.requests, [])
        self.assertNotIn('proof.notified', city.work['metadata'])
        self.assertIn('could not mail human about w1', err)
        city.mail_rc = 0
        rc, _out, _err = run_keeper(city, self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(city.mails()), 2)
        self.assertEqual(len(self.adapter.requests), 1)
        rc, _out, _err = run_keeper(city, self.env)
        self.assertEqual((len(city.mails()), len(self.adapter.requests)), (2, 1))

    def test_unconfigured_sends_no_request_and_output_is_unchanged(self):
        configured = FakeCity()
        run_keeper(configured, self.env)
        for value in (None, '', '   '):
            env = dict(self.env)
            if value is None:
                del env['LISKOV_FACTORY_SLACK_CHANNEL']
            else:
                env['LISKOV_FACTORY_SLACK_CHANNEL'] = value
            city = FakeCity()
            rc, out, err = run_keeper(city, env)
            self.assertEqual(rc, 0)
            self.assertEqual(self.adapter.requests[1:], [], value)
            self.assertEqual(out, 'keeper: w1 failed -> human\n')
            self.assertEqual(err, NO_CLONE)
            self.assertEqual(city.calls, configured.calls)   # Slack never adds or removes a gc call

    def assert_failure_logged(self, env, *reasons):
        city = FakeCity()
        rc, out, err = run_keeper(city, env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(city.mails()), 1)
        self.assertEqual(city.work['metadata']['proof.notified'], 'failed')
        self.assertEqual(out, 'keeper: w1 failed -> human\n')
        self.assertIn("keeper: could not post 'PROOF packet stopped: BKLG-20261014-test' to Slack channel %s: "
                      % CHANNEL, err)
        for reason in reasons:
            self.assertIn(reason, err)
        return city

    def test_adapter_down_connection_refused_is_logged_and_exits_0(self):
        self.assert_failure_logged(dict(self.env, GC_API_BASE_URL=closed_port_url()), 'Connection refused')

    def test_adapter_http_5xx_is_logged_with_its_error_and_exits_0(self):
        self.adapter.status, self.adapter.reply = 502, {'ok': False, 'error': 'slack: channel_not_found'}
        self.assert_failure_logged(self.env, 'adapter returned HTTP 502: slack: channel_not_found')
        self.assertEqual(len(self.adapter.requests), 1)

    def test_adapter_without_ok_true_is_a_failure(self):
        self.adapter.reply = {'ok': False}
        self.assert_failure_logged(self.env, 'adapter did not confirm the post')

    def test_a_hung_adapter_times_out_and_exits_0(self):
        self.adapter.delay = 1.0
        with patch.object(factory_slack, 'TIMEOUT', 0.2):
            self.assert_failure_logged(self.env, 'timed out')

    def test_an_unusable_city_name_is_logged_without_a_request(self):
        self.assert_failure_logged(dict(self.env, GC_CITY_NAME='pro/of'), 'not usable in a URL path')
        self.assertEqual(self.adapter.requests, [])


class AddressTests(unittest.TestCase):
    def name(self, files, env=None):
        with tempfile.TemporaryDirectory() as root:
            city = Path(root) / 'city'
            (city / '.gc').mkdir(parents=True)
            for rel, text in files.items():
                (city / rel).write_text(text)
            with patch.dict(os.environ, {**CLEAN, 'GC_CITY': str(city), **(env or {})}):
                return factory_slack.adapter_base()

    def test_city_name_follows_gc_order(self):
        both = {'.gc/site.toml': 'workspace_name = "proof"\n', 'city.toml': '[workspace]\nname = "declared"\n'}
        self.assertEqual(self.name(both, {'GC_CITY_NAME': 'env'}),
                         'http://127.0.0.1:9443/v0/city/env/svc/slack-channel')
        self.assertEqual(self.name(both, {'GC_API_BASE_URL': 'http://127.0.0.1:8372/'}),
                         'http://127.0.0.1:8372/v0/city/proof/svc/slack-channel')
        self.assertEqual(self.name({'city.toml': '[workspace]\nprovider = "codex"\nname = "declared"\n'}),
                         'http://127.0.0.1:9443/v0/city/declared/svc/slack-channel')
        self.assertEqual(self.name({'city.toml': '[workspace]\nprovider = "codex"\n'}),
                         'http://127.0.0.1:9443/v0/city/city/svc/slack-channel')

    def test_adapter_url_override_wins(self):
        self.assertEqual(self.name({}, {'SLACK_CHANNEL_ADAPTER_URL': 'http://127.0.0.1:1/x/'}), 'http://127.0.0.1:1/x')


class ReportTests(unittest.TestCase):
    NOTE = {'id': 'm1', 'created_at': '9999-01-01T08:00:00Z', 'subject': 'NOTE: BKLG-20261014-note', 'from': 'r1/w'}

    def setUp(self):
        self.adapter = Adapter()
        self.addCleanup(self.adapter.close)
        self.env = {'LISKOV_FACTORY_SLACK_CHANNEL': CHANNEL, 'GC_API_BASE_URL': self.adapter.url,
                    'GC_CITY_NAME': 'proof'}

    def run_report(self, env, argv=(), mail_ok=True):
        out, err = StringIO(), StringIO()
        mailed, marked = [], []

        def mail(subject, body):
            mailed.append((subject, body))
            return mail_ok
        with patch.dict(os.environ, {**CLEAN, **env}), \
                patch.object(fl, 'city_dir', return_value='/nonexistent-city'), \
                patch.object(fl, 'load_policy', return_value={'rigs': {}}), \
                patch.object(fl, 'mail', mail), \
                patch.object(fl.admit, 'load_rigs', return_value={}), \
                patch.object(fl.admit, 'backlog_clone_of', side_effect=fl.Refused('no clone')), \
                patch.object(fl.admit, 'gc', lambda city, *args, **kw: marked.append(args)), \
                patch.object(watchdog, 'inbox', return_value=[self.NOTE]), \
                patch.object(watchdog, 'notes', side_effect=lambda mail: mail), \
                patch.object(watchdog, 'mail_body', return_value='proceeding with the note'), \
                redirect_stdout(out), redirect_stderr(err):
            rc = report.main(list(argv))
        return rc, out.getvalue(), err.getvalue(), mailed, marked

    def test_configured_posts_the_report_once_and_still_mails(self):
        rc, out, err, mailed, marked = self.run_report(self.env)
        self.assertEqual(rc, 0)
        self.assertEqual(len(mailed), 1)
        self.assertEqual(marked, [('mail', 'mark-read', 'm1')])
        self.assertEqual(len(self.adapter.requests), 1)
        req = self.adapter.requests[0]
        self.assertEqual((req['method'], req['path']), ('POST', PUBLISH))
        self.assertEqual(req['headers']['x-gc-request'], 'gc-slack-channel')
        self.assertEqual(req['body']['channel_id'], CHANNEL)
        self.assertTrue(req['body']['idempotency_key'].startswith('proof-report/'))
        self.assertEqual(req['body']['body'], '*PROOF factory report*\n```\n%s\n```' % mailed[0][1])
        self.assertIn('BKLG-20261014-note', req['body']['body'])
        self.assertTrue(out.startswith(mailed[0][1] + '\n'))
        self.assertIn("report: posted 'PROOF factory report' to Slack channel %s" % CHANNEL, out)
        self.assertEqual(err, '')

    def test_unconfigured_sends_no_request_and_prints_only_the_report(self):
        env = dict(self.env)
        del env['LISKOV_FACTORY_SLACK_CHANNEL']
        rc, out, err, mailed, marked = self.run_report(env)
        self.assertEqual(rc, 0)
        self.assertEqual(self.adapter.requests, [])
        self.assertEqual(out, mailed[0][1] + '\n')
        self.assertEqual(err, '')
        self.assertEqual(marked, [('mail', 'mark-read', 'm1')])

    def test_print_never_posts(self):
        rc, out, _err, mailed, marked = self.run_report(self.env, argv=['--print'])
        self.assertEqual((rc, mailed, marked, self.adapter.requests), (0, [], [], []))
        self.assertNotIn('Slack', out)

    def test_a_failed_mail_still_posts_the_run_once(self):
        rc, _out, _err, mailed, marked = self.run_report(self.env, mail_ok=False)
        self.assertEqual((rc, len(mailed), marked, len(self.adapter.requests)), (0, 1, [], 1))

    def test_adapter_failures_are_logged_and_exit_0(self):
        for env, status, reason in ((dict(self.env, GC_API_BASE_URL=closed_port_url()), 200, 'Connection refused'),
                                    (self.env, 500, 'adapter returned HTTP 500: slack: rate_limited')):
            self.adapter.status, self.adapter.reply = status, {'ok': False, 'error': 'slack: rate_limited'}
            rc, out, err, mailed, marked = self.run_report(env)
            self.assertEqual(rc, 0)
            self.assertEqual(len(mailed), 1)
            self.assertEqual(marked, [('mail', 'mark-read', 'm1')])
            self.assertEqual(out, mailed[0][1] + '\n')
            self.assertIn("report: could not post 'PROOF factory report' to Slack channel %s: " % CHANNEL, err)
            self.assertIn(reason, err)


if __name__ == '__main__':
    unittest.main()
