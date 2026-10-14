#!/usr/bin/env python3
"""The three orchestrator-run checks of a live-test packet (ADR-0151 clause 4).

    livetest_runner.py preflight|observe|settle --run-id <id> --live-target <t> ...

`checks/livetest-preflight.sh`, `checks/livetest-observe.sh` and
`checks/livetest-settle.sh` are thin entrypoints: they resolve GC_BEAD_ID to the
workflow root with the existing check helper and hand this module the root's
facts as arguments. Everything that decides lives here, on the model of
`land-on-main.sh`: the exit status is the verdict, the tester's say-so closes
nothing, and every refusal prints the condition that refused.

Observe V1 only (`docs/knowledge/production-validation-observe-contract.md`).
Exactly one recipe is executed, `readyz-build-v1`: a read-only GET of the
public `https://api.liskov.proof.computer/readyz`, with TLS verified, redirects
refused, the body bounded at 64 KiB and a ten-second timeout. The origin comes
from `RECIPE_REQUESTS` below — checked-in code, never a bead or a record. The
run's two-read budget, its lifecycle and its verdict are journalled by
`livetest_state.RunStore`, the one run store; nothing here keeps a second copy.
`mutate` and `spend` are refused as not implemented, after a `spend` cap has
been resolved (or refused) by `checks/livetest_fence.py`.

Preflight, in order — each refusal names its condition in brackets:

  [run]           the root names an admitted run whose manifest verifies, and
                  that run has not yet been preflighted: one preflight per run.
  [spend_fence]   at spend, the declared cap resolves to a named production
                  fence (`livetest_fence.py`), and then
  [exercise_not_implemented]  mutate and spend refuse, whatever the fence said.
  [configured_command]  an operator preflight_command/settle_command is not
                  run by observe V1, so a configured one refuses rather than
                  being silently skipped.
  [credential]    condition 0: the named-profile token, loaded from
                  `<city>/proof/livetest-credentials/<profile>.token` (0600,
                  owned by this user, never a symlink), passes
                  `checks/livetest-credentials.sh`. It is delegated, not
                  reimplemented, and it runs before anything that reaches the
                  target; the only earlier checks read this host's own files.
  [target]        condition 1: the root's live_target is the admitted target
                  and the target answers /readyz as itself.
  [outstanding]   condition 4: this run holds the city's one live slot, and no
                  other run is unsettled or uncertain.
  [build]         condition 2: the public /readyz `build` field is the
                  manifest's full forty-hex expected build. Never a Machine's
                  env, which a Fly secret can silently shadow.
  [freshness]     condition 3: the reading is no older than the manifest's
                  max_age_seconds, by this host's receipt and by the server's
                  own `Date` and `Age` headers when it sends them.

Condition 5 — the fence — is [spend_fence]. The historical conditions about
holds, leases, in_flight targets and controllers belong to the mutate/spend
tiers; at observe V1 the recipe cannot create any, so [outstanding] is the
live slot and the run journal.

Observe reads once more (the second and last read of the budget), judges the
fixed assertions — full build equal to the admitted build, `ready: true`,
`service: liskov-api`, `processGroup: api` — and records `pass` or
`feature_failure` with bounded evidence. Exit 0 means a verdict is durably
recorded, whichever it is; the verdict, not the exit status, is what the
publisher reads. No verdict (no parseable reply, a stale reply, a spent budget)
exits non-zero and settles as inconclusive.

Settle is a teardown and must not assume the exercise got anywhere. With no run
on the root it changes nothing and exits 0. Otherwise it records the recipe's
verified no-op cleanup through the store, releases the slot, and exits 0 — the
same end state however many times it runs. It never contacts production. A run
whose cleanup is not a verified no-op, or whose journal cannot be read while it
holds the slot, exits non-zero: that is intervention, not a pass.

Handoff to admission (`BKLG-20260920-n0tn`): the run is named on the workflow
root as the metadata key `proof.live_run_id` — the only place these checks look
for it. The city is `GC_CITY`, `GC_CITY_PATH`, or the city this pack is
installed in; each must hold a `city.toml`. Tools the credential check needs
(`flyctl`) are found through the rig's `gate_path` formula var, the same
operator-set channel `proof_run_gates` uses; nothing is read from the
operator's HOME.

Standard library only. The clock, the transport and the credential check are
injected, so `gascity/tests/test_livetest_runner.py` drives every path against
recorded fixtures and never reaches production.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKS = os.path.join(HERE, "checks")
sys.path.insert(0, HERE)
sys.path.insert(0, CHECKS)
import livetest_contract as lt  # noqa: E402  the one parser for the record shape
import livetest_fence  # noqa: E402  the one list of production fences
import livetest_state as ls  # noqa: E402  the one run store

# The target-to-origin mapping, as trusted checked-in code. A recipe absent from
# this table is not executed, however it got into a manifest.
RECIPE_REQUESTS = {
    "readyz-build-v1": {
        "target": "liskov-api",
        "host": "api.liskov.proof.computer",
        "port": 443,
        "path": "/readyz",
        "service": "liskov-api",
        "process_group": "api",
    },
}
REQUEST_TIMEOUT_SECONDS = 10
# The bounded `status` string kept as evidence; components and reason strings
# are never copied.
STATUS_MAX = 64
# Gas City's own check PATH, less the tool directories it adds (gc 1.4.2,
# internal/convergence/condition.go). The credential check gets this plus the
# rig's gate_path and nothing inherited.
SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin"
GATE_PATH_RE = re.compile(r"^(/[A-Za-z0-9._/-]+(:/[A-Za-z0-9._/-]+)*)?$")
CREDENTIAL_CHECK = os.path.join(CHECKS, "livetest-credentials.sh")
CREDENTIAL_CHECK_TIMEOUT_SECONDS = 180
ROOT_RUN_KEY = "proof.live_run_id"
NOTHING_STARTED = ("SETTLED — the workflow root names no run (%s), so the exercise never started: "
                   "nothing was contacted and nothing changes" % ROOT_RUN_KEY)

NAMES = {"preflight": "livetest-preflight", "observe": "livetest-observe",
         "settle": "livetest-settle"}


class Refusal(Exception):
    """A check refused. `condition` is the bracketed name an operator reads."""

    def __init__(self, condition, message):
        self.condition = condition
        self.message = message
        super().__init__("[%s] %s" % (condition, message))


@dataclass(frozen=True)
class Response:
    """One read's result. `error_code` is empty when a reply was received whole."""
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)
    error_code: str = ""


# ---------------------------------------------------------------- the transport

def https_get(request):
    """The one production contact this module makes: a GET, never followed.

    TLS is verified against the system trust store and the host name; a 3xx is
    returned as `redirect_refused` rather than followed; the body is read to at
    most RESPONSE_BYTES_MAX and the whole exchange is bounded by
    REQUEST_TIMEOUT_SECONDS. No method other than GET is reachable from here.
    """
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    conn = http.client.HTTPSConnection(request["host"], request["port"],
                                       timeout=REQUEST_TIMEOUT_SECONDS,
                                       context=ssl.create_default_context())
    try:
        conn.request("GET", request["path"], headers={
            "Accept": "application/json", "User-Agent": "proof-livetest/readyz-build-v1",
            "Cache-Control": "no-cache"})
        reply = conn.getresponse()
        headers = {k.lower(): v for k, v in reply.getheaders()}
        if 300 <= reply.status < 400:
            return Response(reply.status, b"", headers, "redirect_refused")
        body = b""
        while len(body) <= ls.RESPONSE_BYTES_MAX:
            if time.monotonic() > deadline:
                return Response(reply.status, body[:ls.RESPONSE_BYTES_MAX], headers, "timeout")
            chunk = reply.read(min(8192, ls.RESPONSE_BYTES_MAX + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        if len(body) > ls.RESPONSE_BYTES_MAX:
            return Response(reply.status, body[:ls.RESPONSE_BYTES_MAX], headers, "body_too_large")
        return Response(reply.status, body, headers, "")
    except ssl.SSLError:
        return Response(0, b"", {}, "tls_failed")
    except (socket.timeout, TimeoutError):
        return Response(0, b"", {}, "timeout")
    except (OSError, http.client.HTTPException):
        return Response(0, b"", {}, "unreachable")
    finally:
        conn.close()


# ---------------------------------------------------------------- the credential

def load_token(city, profile):
    """The named profile's token, from the one file trusted config allows.

    A packet names a profile; it can never name a file, and nothing falls back
    to the operator's HOME, config or an ambient token.
    """
    if not isinstance(profile, str) or not ls.PROFILE_RE.match(profile):
        raise Refusal("credential", "credential profile %r is not a profile name" % (profile,))
    path = os.path.join(city, "proof", "livetest-credentials", profile + ".token")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise Refusal("credential", "no credential for profile %s: %s does not exist. The "
                      "operator supplies it (mode 0600); with none, the tester has no credential, "
                      "which is the correct fail-closed default" % (profile, path))
    if stat.S_ISLNK(info.st_mode):
        raise Refusal("credential", "%s is a symlink; the profile token must be the file itself"
                      % path)
    if not stat.S_ISREG(info.st_mode):
        raise Refusal("credential", "%s is not a regular file" % path)
    if info.st_uid != os.geteuid():
        raise Refusal("credential", "%s is owned by uid %d, not by this user (uid %d)"
                      % (path, info.st_uid, os.geteuid()))
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise Refusal("credential", "%s has mode %04o; a credential file must be 0600"
                      % (path, stat.S_IMODE(info.st_mode)))
    with open(path, encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise Refusal("credential", "%s is empty" % path)
    if "\n" in token or "\r" in token:
        raise Refusal("credential", "%s holds more than one line; it must hold exactly one token"
                      % path)
    return token


def run_credential_check(token, *, exercise, app, max_seconds, tool_path):
    """Delegate to `checks/livetest-credentials.sh` (ADR-0152), in an
    environment built from nothing: the profile's token, the declared tier, the
    sandbox PATH with the rig's gate_path in front, and an empty HOME that
    exists only for this call. Returns (exit status, redacted output)."""
    home = tempfile.mkdtemp(prefix="livetest-cred-home.")
    env = {"PATH": (tool_path + ":" if tool_path else "") + SANDBOX_PATH, "HOME": home,
           "FLY_API_TOKEN": token, "LISKOV_LIVETEST_EXERCISE": exercise, "LANG": "C.UTF-8"}
    try:
        done = subprocess.run([CREDENTIAL_CHECK, "--exercise", exercise, "--app", app,
                               "--max-exercise-seconds", str(max_seconds)],
                              env=env, capture_output=True, text=True,
                              timeout=CREDENTIAL_CHECK_TIMEOUT_SECONDS)
        rc, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired:
        rc, output = 124, "the credential check did not finish in %ds" % CREDENTIAL_CHECK_TIMEOUT_SECONDS
    except OSError as why:
        rc, output = 126, "the credential check could not run: %s" % why
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return rc, output.replace(token, "<redacted-token>")


# ---------------------------------------------------------------- the city

def find_city(env=None):
    """GC_CITY, GC_CITY_PATH, or the city this pack is installed in
    (`<city>/packs/proof/assets/scripts/`). Whichever it is must hold a
    city.toml: a store that guessed its city could hold the wrong fleet's slot."""
    env = os.environ if env is None else env
    city = env.get("GC_CITY") or env.get("GC_CITY_PATH") or \
        os.path.normpath(os.path.join(HERE, "..", "..", "..", ".."))
    if not os.path.isfile(os.path.join(city, "city.toml")):
        raise Refusal("run", "cannot find the city: %s holds no city.toml; set GC_CITY or run "
                      "the pack installed in a city" % city)
    return city


# ---------------------------------------------------------------- the runner

class Runner:
    """One invocation of one check. `out` receives every line printed."""

    def __init__(self, city, *, now=None, transport=None, credential_check=None, out=None):
        self.city = city
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.store = ls.RunStore(city, now=self._now)
        self.transport = transport or https_get
        self.credential_check = credential_check or run_credential_check
        self._out = out

    def say(self, name, text):
        line = "%s: %s" % (name, text)
        if self._out is not None:
            self._out(line)
        else:
            print(line, flush=True)

    def refuse(self, name, why):
        line = "%s: REFUSE [%s] %s" % (name, why.condition, why.message)
        if self._out is not None:
            self._out(line)
        else:
            print(line, flush=True)
            print(line, file=sys.stderr, flush=True)

    # -- shared reads -------------------------------------------------------

    def _manifest(self, run_id):
        """The admitted manifest, verified against its digest and its journal.

        `RunStore` exposes no public manifest reader; `_load_state` is the one
        path that checks the manifest, the journal and their identities
        together, so it is used rather than a second parser.
        """
        return self.store._load_state(run_id)["_manifest"]

    def _load_run(self, root):
        run_id = root.get("run_id") or ""
        if not run_id:
            raise Refusal("run", "the workflow root names no run (%s); admission records the run "
                          "it admitted there. Nothing was contacted" % ROOT_RUN_KEY)
        if not ls.RUN_ID_RE.match(run_id):
            raise Refusal("run", "%s %r is not a thirty-two-hex run id" % (ROOT_RUN_KEY, run_id))
        try:
            status = self.store.status(run_id)
            manifest = self._manifest(run_id)
        except ls.Refused as why:
            raise Refusal("run", "run %s cannot be used: %s%s" % (
                run_id, why.code, (": " + why.detail) if why.detail else ""))
        return run_id, status, manifest

    def _request(self, manifest):
        request = RECIPE_REQUESTS.get(manifest["recipe"])
        if request is None or request["target"] != manifest["target"]:
            raise Refusal("target", "recipe %s has no registered request for target %s; only a "
                          "registered recipe is executed" % (manifest["recipe"], manifest["target"]))
        return request

    def _tier(self, name, root, manifest):
        mutate = root.get("mutate") == "true"
        spend = root.get("spend") == "true"
        if spend:
            try:
                found = livetest_fence.resolve(root.get("spend_cap") or "")
            except livetest_fence.FenceRefused as why:
                raise Refusal("spend_fence", "%s: %s" % (why.code, why.message))
            self.say(name, "spend_cap resolves to %s (%s); resolving a fence does not authorize "
                     "a spend" % (found["fence"], found["name"]))
        level = "spend" if spend else "mutate" if mutate else manifest["exercise"]
        if not lt.is_authorized_exercise(level) or manifest["exercise"] != level:
            raise Refusal("exercise_not_implemented", "exercise %r is recognised but not "
                          "implemented; observe V1 executes %s only" % (
                              level, " | ".join(lt.AUTHORIZED_EXERCISES)))
        return level

    def _target(self, root, manifest):
        declared = root.get("live_target") or ""
        if declared != manifest["target"]:
            raise Refusal("target", "the workflow's live_target is %r, but run %s was admitted "
                          "against %r" % (declared, manifest["run_id"], manifest["target"]))

    def _read(self, run_id, phase, request):
        """One journalled read: persisted before it is sent, and after receipt."""
        try:
            receipt = self.store.begin_attempt(run_id, phase)
        except ls.Refused as why:
            raise Refusal("run", "the run's read budget refuses a %s read: %s%s" % (
                phase, why.code, (": " + why.detail) if why.detail else ""))
        reply = self.transport(request)
        self.store.complete_attempt(run_id, receipt.seq, http_status=reply.status,
                                    body=reply.body[:ls.RESPONSE_BYTES_MAX],
                                    error_code=reply.error_code)
        return reply, self._now()

    @staticmethod
    def _readyz(reply, request):
        """The reply as a /readyz object, or a [target] refusal saying why not."""
        if reply.error_code:
            raise Refusal("target", "%s did not answer /readyz: %s (HTTP %d)"
                          % (request["target"], reply.error_code, reply.status))
        try:
            doc = json.loads(reply.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refusal("target", "%s answered /readyz with HTTP %d and no JSON object"
                          % (request["target"], reply.status))
        if not isinstance(doc, dict):
            raise Refusal("target", "%s answered /readyz with HTTP %d and JSON that is not an "
                          "object" % (request["target"], reply.status))
        if doc.get("service") != request["service"] or \
                doc.get("processGroup") != request["process_group"]:
            raise Refusal("target", "the reply at %s is from service %r process group %r, not "
                          "%s/%s" % (request["host"], str(doc.get("service"))[:40],
                                     str(doc.get("processGroup"))[:40], request["service"],
                                     request["process_group"]))
        return doc

    @staticmethod
    def _freshness(reply, received, now, max_age):
        """How old the reading is, from every clock that can say, or a refusal."""
        age = (now - received).total_seconds()
        if age > max_age:
            raise Refusal("freshness", "the reading was received %ds ago; the manifest's window is "
                          "%ds" % (age, max_age))
        served = reply.headers.get("date")
        if served:
            try:
                at = parsedate_to_datetime(served)
            except (TypeError, ValueError):
                raise Refusal("freshness", "the reply's Date header %r cannot be read, so its "
                              "freshness cannot be measured" % served[:40])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            skew = (now - at).total_seconds()
            if skew > max_age:
                raise Refusal("freshness", "the reply is dated %s, %ds before now; the manifest's "
                              "window is %ds (a cached or stale reading)"
                              % (served[:40], skew, max_age))
        cached = reply.headers.get("age")
        if cached:
            if not cached.strip().isdigit():
                raise Refusal("freshness", "the reply's Age header %r cannot be read" % cached[:20])
            if int(cached) > max_age:
                raise Refusal("freshness", "the reply was served from a cache %ss old; the "
                              "manifest's window is %ds" % (cached.strip(), max_age))

    def _outstanding(self, run_id):
        slot = self.store.slot()
        if slot.needs_intervention:
            raise Refusal("outstanding", "the live slot is held by run %s, whose journal cannot be "
                          "read (%s); it is not released by time" % (slot.run_id, slot.detail))
        if not slot.held:
            raise Refusal("outstanding", "run %s does not hold the city's live slot; admission "
                          "takes it, and a run without it may not contact production" % run_id)
        if slot.run_id != run_id:
            raise Refusal("outstanding", "an earlier run %s (%s) holds the live slot"
                          % (slot.run_id, slot.item_id or "?"))
        root = self.store.runs_root
        others = sorted(n for n in (os.listdir(root) if os.path.isdir(root) else [])
                        if n != run_id and not n.startswith("."))
        for other in others:
            try:
                status = self.store.status(other)
            except ls.Refused as why:
                raise Refusal("outstanding", "earlier run %s cannot be read (%s); an uncertain run "
                              "is outstanding until a person settles it" % (other, why.code))
            if status.lifecycle not in ("settled", "published"):
                raise Refusal("outstanding", "earlier run %s (%s) is at %s and has not settled"
                              % (other, status.item_id or "?", status.lifecycle))

    def _record_refusal(self, name, run_id, why):
        if why.condition == "run":
            return      # not this preflight's verdict: the run was already past it
        try:
            self.store.record_preflight(run_id, passed=False, code=why.condition)
        except ls.Refused as failed:
            self.say(name, "the refusal could not be journalled on run %s: %s %s"
                     % (run_id, failed.code, failed.detail))

    # -- the three checks ---------------------------------------------------

    def preflight(self, root):
        name = NAMES["preflight"]
        try:
            run_id, status, manifest = self._load_run(root)
        except Refusal as why:
            self.refuse(name, why)
            return 1
        try:
            if status.lifecycle != "admitted" or status.attempts_used or status.outcome:
                raise Refusal("run", "run %s is at %s with %d read(s) spent%s; preflight happens "
                              "once per run, and a second one is a new run id" % (
                                  run_id, status.lifecycle, status.attempts_used,
                                  (" and outcome " + status.outcome) if status.outcome else ""))
            level = self._tier(name, root, manifest)
            configured = [k for k in ("preflight_command", "settle_command") if root.get(k)]
            if configured:
                raise Refusal("configured_command", "%s is set for this rig; observe V1 runs only "
                              "the registered recipe and does not run an operator command, so it "
                              "refuses rather than skip it silently" % " and ".join(configured))
            tool_path = root.get("tool_path") or ""
            if not GATE_PATH_RE.match(tool_path):
                raise Refusal("credential", "gate_path %r is not a list of absolute directories"
                              % tool_path[:80])

            # Condition 0. Only this host's files have been read so far.
            token = load_token(self.city, manifest["credential_profile"])
            rc, output = self.credential_check(
                token, exercise=level, app=manifest["target"],
                max_seconds=lt.OBSERVE_TIMEOUT_SECONDS, tool_path=tool_path)
            if rc != 0:
                lines = [ln for ln in output.splitlines() if ln.strip()][:4]
                raise Refusal("credential", "livetest-credentials.sh refused (exit %d): %s"
                              % (rc, " / ".join(lines) or "no output"))
            self.say(name, "credential: %s" % (output.strip().splitlines() or ["ok"])[-1])

            # Conditions 1 and 4, locally, before the target hears from us.
            self._target(root, manifest)
            request = self._request(manifest)
            self._outstanding(run_id)

            # The first of the run's two reads.
            reply, received = self._read(run_id, "preflight", request)
            doc = self._readyz(reply, request)
            build = doc.get("build")
            if not isinstance(build, str) or not lt.BUILD_RE.match(build):
                raise Refusal("build", "/readyz reports build %r, not a full forty-hex commit"
                              % (str(build)[:48],))
            if build != manifest["expected_build"]:
                raise Refusal("build", "/readyz reports build %s; the manifest expects %s"
                              % (build, manifest["expected_build"]))
            self._freshness(reply, received, self._now(), manifest["max_age_seconds"])
        except Refusal as why:
            self.refuse(name, why)
            self._record_refusal(name, run_id, why)
            return 1
        except ls.Refused as why:
            failed = Refusal("run", "the run journal refused: %s %s" % (why.code, why.detail))
            self.refuse(name, failed)
            return 1
        self.store.record_preflight(run_id, passed=True)
        self.say(name, "PASS run %s: %s at build %s, read %s; one read of %d remains" % (
            run_id, manifest["target"], build, ls.instant(received), ls.MAX_REQUESTS))
        return 0

    def observe(self, root):
        name = NAMES["observe"]
        try:
            run_id, status, manifest = self._load_run(root)
            if status.lifecycle != "preflight_passed":
                raise Refusal("run", "run %s is at %s; an observation follows a passed preflight"
                              % (run_id, status.lifecycle))
            self._tier(name, root, manifest)
            self._target(root, manifest)
            request = self._request(manifest)
            reply, received = self._read(run_id, "observation", request)
        except Refusal as why:
            self.refuse(name, why)
            return 1
        except ls.Refused as why:
            self.refuse(name, Refusal("run", "the run journal refused: %s %s" % (why.code, why.detail)))
            return 1

        try:
            doc = self._readyz(reply, request)
            self._freshness(reply, received, self._now(), manifest["max_age_seconds"])
        except Refusal as why:
            # No reply that can be judged: no verdict, and settle records the
            # run as inconclusive. Never a pass by default.
            self.refuse(name, why)
            self.say(name, "INCONCLUSIVE run %s: the observation cannot be judged" % run_id)
            return 1

        failed = []
        build = doc.get("build")
        if not (isinstance(build, str) and lt.BUILD_RE.match(build)):
            failed.append("build_not_full")
        elif build != manifest["expected_build"]:
            failed.append("build_mismatch")
        if doc.get("ready") is not True:
            failed.append("not_ready")
        verdict = "feature_failure" if failed else "pass"
        status_text = doc.get("status")
        evidence = {
            "http_status": reply.status,
            "build": build if isinstance(build, str) and lt.BUILD_RE.match(build) else "",
            "expected_build": manifest["expected_build"],
            "ready": doc.get("ready") is True,
            "status": status_text[:STATUS_MAX] if isinstance(status_text, str) else "",
            "service": doc.get("service"),
            "processGroup": doc.get("processGroup"),
            "failed_assertions": ",".join(failed),
            "received_at": ls.instant(received),
        }
        try:
            self.store.record_observation(run_id, verdict=verdict, evidence=evidence)
        except ls.Refused as why:
            self.refuse(name, Refusal("run", "the verdict could not be journalled: %s %s"
                                      % (why.code, why.detail)))
            return 1
        self.say(name, "VERDICT %s run %s: build %s (expected %s), ready %s, status %r%s" % (
            verdict, run_id, evidence["build"] or "?", manifest["expected_build"],
            str(evidence["ready"]).lower(), evidence["status"],
            (" — failed: " + evidence["failed_assertions"]) if failed else ""))
        return 0

    def settle(self, root):
        name = NAMES["settle"]
        run_id = root.get("run_id") or ""
        if root.get("settle_command"):
            self.say(name, "settle_command is set for this rig and is not run by observe V1; "
                     "the registered recipe's cleanup is the verified no-op below")
        if not run_id:
            self.say(name, NOTHING_STARTED)
            return 0
        if not ls.RUN_ID_RE.match(run_id):
            self.refuse(name, Refusal("run", "%s %r is not a thirty-two-hex run id; a person "
                                      "must find the run this workflow owns" % (ROOT_RUN_KEY, run_id)))
            return 1
        try:
            self.store.status(run_id)
        except ls.Refused as why:
            if why.code == "state_missing":
                try:
                    slot = self.store.slot()
                except ls.Refused as broken:
                    self.refuse(name, Refusal("run", "the live slot cannot be read: %s %s"
                                              % (broken.code, broken.detail)))
                    return 1
                if slot.run_id != run_id:
                    self.say(name, "SETTLED — run %s has no journal and does not hold the slot: "
                             "every read is journalled before it is sent, so nothing was "
                             "contacted and nothing changes" % run_id)
                    return 0
            self.refuse(name, Refusal("run", "run %s cannot be settled: %s%s; this needs a person, "
                                      "and the slot stays held" % (
                                          run_id, why.code, (": " + why.detail) if why.detail else "")))
            return 1

        try:
            settled = self.store.settle(run_id)
        except ls.Refused as why:
            self.refuse(name, Refusal("cleanup", "run %s: %s %s" % (run_id, why.code, why.detail)))
            return 1
        if settled.lifecycle == "cleanup_required":
            self.refuse(name, Refusal("cleanup", "run %s: %s; the slot stays held until a person "
                                      "settles it" % (run_id, settled.detail)))
            return 1
        try:
            slot = self.store.release(run_id)
        except ls.Refused as why:
            if why.code != "slot_foreign":
                self.refuse(name, Refusal("cleanup", "run %s settled but the slot was not released: "
                                          "%s %s" % (run_id, why.code, why.detail)))
                return 1
            # Released by an earlier settle; another run has the slot since.
            slot = None
        self.say(name, "SETTLED run %s: %s; cleanup is a verified no-op (the recipe creates no "
                 "production resource); outcome %s; slot %s" % (
                     run_id, settled.lifecycle, settled.reported or "none",
                     "released" if slot is None or not slot.held or slot.run_id != run_id
                     else "still held"))
        return 0


# ---------------------------------------------------------------- the entrypoint

def parse(argv):
    ap = argparse.ArgumentParser(prog="livetest_runner.py")
    ap.add_argument("check", choices=sorted(NAMES))
    # Every value below comes from the workflow root: data, never a command.
    ap.add_argument("--run-id", default="")
    ap.add_argument("--live-target", default="")
    ap.add_argument("--mutate", default="false")
    ap.add_argument("--spend", default="false")
    ap.add_argument("--spend-cap", default="")
    ap.add_argument("--preflight-command", default="")
    ap.add_argument("--settle-command", default="")
    ap.add_argument("--tool-path", default="")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    root = {"run_id": args.run_id, "live_target": args.live_target, "mutate": args.mutate,
            "spend": args.spend, "spend_cap": args.spend_cap,
            "preflight_command": args.preflight_command, "settle_command": args.settle_command,
            "tool_path": args.tool_path}
    name = NAMES[args.check]
    if args.check == "settle" and not args.run_id:
        # Needs no city: with no run, there is nothing a city could hold.
        print("%s: %s" % (name, NOTHING_STARTED), flush=True)
        return 0
    try:
        city = find_city()
    except Refusal as why:
        print("%s: REFUSE [%s] %s" % (name, why.condition, why.message), flush=True)
        print("%s: REFUSE [%s] %s" % (name, why.condition, why.message), file=sys.stderr)
        return 1
    return getattr(Runner(city), args.check)(root)


if __name__ == "__main__":
    sys.exit(main())
