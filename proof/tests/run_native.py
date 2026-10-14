#!/usr/bin/env python3
"""Qualify a pack-owned Beads helper on a disposable server, never a live city."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1] / 'work-helper'


def run(args, cwd, env, *, data=None, check=True, timeout=180):
    result = subprocess.run(args, cwd=cwd, env=env, input=data, text=True,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{args[0:3]} exited {result.returncode}\n{result.stdout}\n{result.stderr}')
    return result


def main():
    binaries = {name: shutil.which(name) for name in ('bd', 'dolt', 'go', 'git')}
    if binaries['go'] is None and Path('/usr/local/go/bin/go').exists():
        binaries['go'] = '/usr/local/go/bin/go'
    if not all(binaries.values()):
        raise RuntimeError(f'missing binary: {binaries}')
    with tempfile.TemporaryDirectory(prefix='liskov-beads-proof-') as scratch:
        base = Path(scratch)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('BEADS_', 'BD_', 'GC_', 'DOLT_', 'LISKOV_BEADS_PROBE_'))}
        env.update(DOLT_ROOT_PATH=str(base / 'dolt-config'),
                   XDG_CONFIG_HOME=str(base / 'config'),
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='Beads qualification', GIT_AUTHOR_EMAIL='probe@localhost',
                   GIT_COMMITTER_NAME='Beads qualification', GIT_COMMITTER_EMAIL='probe@localhost',
                   GOTOOLCHAIN='local', GOENV='off', GOWORK='off', GOFLAGS='',
                   GOCACHE=os.environ.get('GOCACHE', str(Path(tempfile.gettempdir()) / 'liskov-beads-qualification-go-cache')))
        env['PATH'] = str(Path(binaries['go']).parent) + os.pathsep + env.get('PATH', '')
        versions = {}
        for name in ('bd', 'dolt', 'go'):
            path = Path(binaries[name])
            versions[name] = dict(version=run([str(path), 'version'], ROOT, env).stdout.strip(),
                                  sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        print(json.dumps({'binaries': versions}, sort_keys=True), flush=True)
        if not versions['bd']['version'].startswith('bd version 1.3.1 '):
            raise RuntimeError('qualification requires bd 1.3.1, matching the library pin')
        server = base / 'server'
        server.mkdir()
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        sql_socket = base / 'mysql.sock'
        config = base / 'server.yaml'
        config.write_text(f'log_level: warning\nlistener:\n  host: 127.0.0.1\n  port: {port}\n'
                          f'  socket: {json.dumps(str(sql_socket))}\ndata_dir: {json.dumps(str(server))}\n'
                          'behavior:\n  auto_gc_behavior:\n    enable: false\n')
        project = base / 'project'
        project.mkdir()
        run([binaries['git'], 'init', '-q'], project, env)
        run([binaries['git'], 'config', 'user.name', 'Beads qualification'], project, env)
        run([binaries['git'], 'config', 'user.email', 'probe@localhost'], project, env)
        with (base / 'server.log').open('w') as log:
            process = subprocess.Popen([binaries['dolt'], 'sql-server', '--config', str(config)],
                                       cwd=server, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while not sql_socket.exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError((base / 'server.log').read_text())
                    time.sleep(.05)
                env.update(BEADS_DOLT_SERVER_HOST='127.0.0.1', BEADS_DOLT_SERVER_PORT=str(port),
                           BEADS_DOLT_SERVER_SOCKET=str(sql_socket), BEADS_DOLT_AUTO_START='0',
                           BEADS_DOLT_SERVER_USER='root')
                run([binaries['bd'], 'init', '--server', '--external', '--server-port', str(port),
                     '--server-socket', str(sql_socket), '--prefix', 'pq', '--database', 'proof_test_native', '--skip-agents',
                     '--skip-hooks', '--non-interactive'], project, env)
                env['BEADS_DIR'] = str(project / '.beads')
                env['LISKOV_BEADS_PROBE_DIR'] = env['BEADS_DIR']
                env['LISKOV_PROOF_EVALUATOR'] = str(ROOT.parent / 'assets/scripts/proof_work_evaluate.py')
                # Although this probe uses server mode, the upstream library's
                # transitive dependencies still require ICU development headers.
                run([binaries['go'], 'test', '-mod=readonly', '-c', '-o', str(base / 'probe.test')], ROOT, env, timeout=600)
                result = run([str(base / 'probe.test'), '-test.v', '-test.timeout=120s'], project, env, check=False)
                print(result.stdout, end='', flush=True)
                if result.stderr:
                    print(result.stderr, end='', flush=True)
                if result.returncode:
                    raise RuntimeError('public-library qualification failed')
                # Exercise the actual CLI boundary, not only run() inside Go.
                helper = base / 'proof-work'
                run([binaries['go'], 'build', '-mod=readonly', '-o', str(helper), '.'], ROOT, env, timeout=600)
                helper_env = {k: v for k, v in env.items() if not k.startswith(('BEADS_', 'BD_'))}
                metadata_path = project / '.beads/metadata.json'
                metadata = json.loads(metadata_path.read_text())
                # The test workspace uses the explicitly allowed isolated name.
                with (project / '.beads/config.yaml').open('a') as stream:
                    stream.write('\nevents-journal-auto-prune: false\n')
                argv = [str(helper), '--workspace', str(project / '.beads'),
                        '--evaluator', env['LISKOV_PROOF_EVALUATOR']]
                request = json.dumps({'action': 'snapshot', 'epoch': 'test'})
                baseline = json.loads(run(argv, project, helper_env, data=request).stdout)['records']
                refused = run(argv, project, dict(helper_env, BEADS_DIR='/wrong'), data=request, check=False)
                if refused.returncode == 0 or 'ambient' not in refused.stderr:
                    raise RuntimeError('CLI accepted routing override')
                metadata_path.write_text(json.dumps(dict(metadata, dolt_database='execution_db')))
                refused = run(argv, project, helper_env, data=request, check=False)
                metadata_path.write_text(json.dumps(metadata))
                if refused.returncode == 0 or 'database must be' not in refused.stderr:
                    raise RuntimeError('CLI accepted execution database')
                refused = run(argv, project, helper_env, data=json.dumps({'action': 'snapshot', 'epoch': 'wrong'}), check=False)
                if refused.returncode == 0 or 'epoch' not in refused.stderr:
                    raise RuntimeError('CLI accepted wrong epoch')
                refused = run(argv[:-1] + ['/missing/evaluator.py'], project, helper_env, data=request, check=False)
                if refused.returncode == 0:
                    raise RuntimeError('CLI accepted missing policy')
                # revision is answered from the coordination bead without the evaluator.
                revision = json.loads(run(argv[:-1] + ['/missing/evaluator.py'], project, helper_env,
                                          data=json.dumps({'action': 'revision', 'epoch': 'test'})).stdout)
                if revision['mode'] != 'shadow' or type(revision['revision']) is not int or revision['revision'] < 1 or not revision['generated_at']:
                    raise RuntimeError('CLI revision answered wrongly: %r' % revision)
                if json.loads(run(argv, project, helper_env, data=request).stdout)['records'] != baseline:
                    raise RuntimeError('CLI refusals changed records')
                print('PASS checked executable routing, epoch, missing-policy refusals and evaluator-free revision preserve records', flush=True)
                # Negative control: native close does NOT know our acceptance policy.
                created = run([binaries['bd'], 'create', 'Acceptance bypass control', '--json'], project, env)
                issue = json.loads(created.stdout)
                issue = issue[0] if isinstance(issue, list) else issue
                ident = issue['id']
                run([binaries['bd'], 'update', ident, '--set-metadata', 'proof.acceptance=missing'], project, env)
                run([binaries['bd'], 'close', ident, '--json'], project, env)
                shown = json.loads(run([binaries['bd'], 'show', ident, '--json'], project, env).stdout)
                shown = shown[0] if isinstance(shown, list) else shown
                if shown['status'] != 'closed':
                    raise RuntimeError('negative control changed: re-assess native close semantics')
                print('PASS negative control: raw bd close bypasses pack acceptance metadata', flush=True)
                # The batch CLI is atomic for supported writes, but not a general
                # conditional read/metadata transaction for domain commands.
                other = json.loads(run([binaries['bd'], 'create', 'Batch rollback', '--json'], project, env).stdout)
                other = other[0] if isinstance(other, list) else other
                result = run([binaries['bd'], 'batch'], project, env,
                             data=f'update {other["id"]} status=in_progress\nupdate pq-missing title=fail\n', check=False)
                if result.returncode == 0:
                    raise RuntimeError('batch accepted missing target')
                shown = json.loads(run([binaries['bd'], 'show', other['id'], '--json'], project, env).stdout)
                shown = shown[0] if isinstance(shown, list) else shown
                if shown['status'] != 'open':
                    raise RuntimeError('failed batch left partial state')
                print('PASS CLI batch rollback after first update', flush=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
