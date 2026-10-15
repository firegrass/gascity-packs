#!/bin/sh
# gc slack-channel verify-message — ask Slack (through the adapter, which
# alone holds the bot token) who wrote the message at (channel, ts), in which
# thread, with what text. Exists for answer provenance: a non-zero exit means
# the message is missing, bot-authored, foreign, or unreadable, never "ok".
set -eu
. "$(dirname "$0")/_lib.sh"

channel=""
ts=""

while [ $# -gt 0 ]; do
  case "$1" in
    --channel)   channel="$2"; shift 2 ;;
    --channel=*) channel="${1#*=}"; shift ;;
    --ts)        ts="$2"; shift 2 ;;
    --ts=*)      ts="${1#*=}"; shift ;;
    -h|--help)   sc_help verify-message ;;
    *) sc_die "unknown argument: $1" 2 ;;
  esac
done

[ -n "$channel" ] || sc_die "--channel is required" 2
[ -n "$ts" ] || sc_die "--ts is required" 2
sc_require

req=$(jq -n --arg channel "$channel" --arg ts "$ts" '{channel: $channel, ts: $ts}')
sc_call POST verify-message "$req"
