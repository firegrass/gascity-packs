"""Credential policy reports rules without echoing supplied credential material."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
from io import StringIO
from pathlib import Path
import unittest
import subprocess
import sys
import os
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'assets/scripts/checks/livetest_token.py'
spec = importlib.util.spec_from_file_location('credential_judge', path)
judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge)
CANARY = 'synthetic-sensitive-marker-7fc2'


class DiagnosticTests(unittest.TestCase):
    def invoke(self, app='liskov-api', fields=None):
        argv = ['judge', '--exercise', 'observe', '--app', app, '--max-exercise-seconds', '120', '--now', '1000']
        output = StringIO()
        with patch('sys.argv', argv), patch('sys.stdin', StringIO('[]')), redirect_stdout(output), redirect_stderr(output):
            if fields is None:
                rc = judge.main()
            else:
                with patch.object(judge, 'collect', return_value=fields):
                    rc = judge.main()
        self.assertNotIn(CANARY, output.getvalue())
        return rc, output.getvalue()

    def test_unknown_target_is_not_echoed(self):
        self.assertEqual(self.invoke(app=CANARY)[0], 2)

    def test_caveat_app_is_not_echoed(self):
        fields = dict(not_before=None, not_after=4600, apps=[CANARY], commands=None, org_masks=['r'])
        rc, output = self.invoke(fields=fields)
        self.assertEqual(rc, 2)
        self.assertIn('app scope', output)

    def test_parser_and_shell_errors_do_not_echo_arguments(self):
        helper = [sys.executable, str(path)]
        wrapper = ['bash', str(path.with_name('livetest-credentials.sh'))]
        for prefix in (helper, wrapper):
            for flag in ('--exercise', '--max-exercise-seconds', '--unknown'):
                argv = ['--exercise', 'observe', '--app', 'liskov-api', '--max-exercise-seconds', '120', flag, CANARY]
                result = subprocess.run(prefix + argv, input='[]', capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(CANARY, result.stdout + result.stderr)
        environment = dict(os.environ, FLY_API_TOKEN='synthetic-credential', LISKOV_LIVETEST_EXERCISE=CANARY)
        result = subprocess.run(wrapper + ['--exercise', 'observe', '--app', 'liskov-api', '--max-exercise-seconds', '120'],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 12)
        self.assertNotIn(CANARY, result.stdout + result.stderr)

    def test_valid_credential_keeps_its_acceptance(self):
        fields = dict(not_before=None, not_after=4600, apps=None, commands=None, org_masks=['r'])
        self.assertEqual(self.invoke(fields=fields)[0], 0)


if __name__ == '__main__':
    unittest.main()
