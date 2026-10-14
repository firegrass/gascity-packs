"""Factory notices to the factory Slack channel (ADR-0237 §3).

The keeper and the daily report mail a human; when `LISKOV_FACTORY_SLACK_CHANNEL`
names a Slack channel id they ALSO post each notice there once, through the
`slack-channel` adapter. Mail stays the record and is always sent. Unset or
empty, nothing here runs: no request, no output.

The request is the one `gc slack-channel publish-to-channel` makes
(slack-channel/commands/_lib.sh, sc_call):

    POST <base>/publish-to-channel
    Content-Type: application/json
    X-GC-Request: gc-slack-channel
    {"channel_id": ..., "body": ..., "idempotency_key": ...}

where <base> is `$SLACK_CHANNEL_ADAPTER_URL` when set, else
`${GC_API_BASE_URL:-http://127.0.0.1:9443}/v0/city/<city>/svc/slack-channel`,
gc's reverse proxy to the adapter. The adapter holds the bot token; nothing
here reads one. A failed post is logged with its reason and never raises, so it
can neither change a run's exit status nor stop it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

CHANNEL_ENV = "LISKOV_FACTORY_SLACK_CHANNEL"
TIMEOUT = 10            # seconds; the keeper's whole pass has 120
LIMIT = 35000           # Slack truncates chat.postMessage text at 40,000 characters
DEFAULT_API = "http://127.0.0.1:9443"   # the verb wrapper's default, kept identical


class Unavailable(Exception):
    pass


def channel():
    return (os.environ.get(CHANNEL_ENV) or "").strip()


def city_name():
    """GC_CITY_NAME (what gc gives pack commands), else the city's site binding
    `.gc/site.toml` workspace_name, else city.toml `[workspace] name`, else the
    city directory's name — gc's own order (config.EffectiveCityName)."""
    name = (os.environ.get("GC_CITY_NAME") or "").strip()
    if not name:
        import tomllib
        import admit
        try:
            root = admit.city_root()
        except Exception as why:
            raise Unavailable("no GC_CITY_NAME and no city root: %s" % why)
        for path, read in ((os.path.join(root, ".gc", "site.toml"), lambda d: d.get("workspace_name")),
                           (os.path.join(root, "city.toml"), lambda d: (d.get("workspace") or {}).get("name"))):
            try:
                with open(path, "rb") as fh:
                    name = str(read(tomllib.load(fh)) or "").strip()
            except (OSError, ValueError):
                name = ""
            if name:
                break
        name = name or os.path.basename(os.path.normpath(root))
    # The name is interpolated into the URL path, as in sc_city.
    if not name or any(c in name for c in "/?#%") or any(c.isspace() for c in name):
        raise Unavailable("city name %r is not usable in a URL path" % name)
    return name


def adapter_base():
    override = os.environ.get("SLACK_CHANNEL_ADAPTER_URL") or ""
    if override:
        return override.rstrip("/")
    base = (os.environ.get("GC_API_BASE_URL") or DEFAULT_API).rstrip("/")
    return "%s/v0/city/%s/svc/slack-channel" % (base, city_name())


def text_of(subject, body):
    body = body.replace("```", "'''")
    if len(body) > LIMIT:
        body = body[:LIMIT] + "\n… truncated; the mail carries the full text"
    return "*%s*\n```\n%s\n```" % (subject, body)


def post(channel_id, subject, body, key=""):
    """Post once; returns the message ts. Raises Unavailable with the reason."""
    request = {"channel_id": channel_id, "body": text_of(subject, body)}
    if key:
        request["idempotency_key"] = key
    url = adapter_base() + "/publish-to-channel"
    req = urllib.request.Request(url, data=json.dumps(request).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-GC-Request": "gc-slack-channel"})
    try:
        # gc's API is the city's own controller: never route it through an
        # ambient HTTP proxy.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("error") or detail
        except (ValueError, AttributeError):
            pass
        raise Unavailable("adapter returned HTTP %d: %s" % (err.code, str(detail)[:200]))
    except urllib.error.URLError as err:
        raise Unavailable("request to %s failed: %s" % (url, err.reason))
    except (OSError, ValueError) as err:   # timeouts, resets, a malformed URL
        raise Unavailable("request to %s failed: %s" % (url, err))
    try:
        reply = json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise Unavailable("adapter did not confirm the post: %s" % raw[:200].decode("utf-8", "replace"))
    return str(reply.get("ts") or "")


def notify(who, subject, body, key=""):
    """Post a notice when a channel is configured, logging the outcome as `who`.
    Returns True when posted, False when it failed, None when not configured.
    Never raises."""
    target = channel()
    if not target:
        return None
    try:
        ts = post(target, subject, body, key)
    except Exception as why:   # a Slack failure must never stop the run
        print("%s: could not post %r to Slack channel %s: %s" % (who, subject, target, why), file=sys.stderr)
        return False
    print("%s: posted %r to Slack channel %s (ts %s)" % (who, subject, target, ts or "?"))
    return True
