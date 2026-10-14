Ask Slack who wrote one message, in which thread, with what text.

The adapter reads the message back with its own bot token
(`conversations.replies`, falling back to `conversations.history`, then
`chat.getPermalink`), so the caller never holds the token. It exists for
answer provenance: a command that records a person's answer calls it first
and records the result, rather than trusting text an agent relayed.

Usage:
  gc slack-channel verify-message --channel <id> --ts <ts>

Flags:
  --channel  Slack conversation id (C..., G..., or D...) — required.
  --ts       The message's Slack ts, e.g. 1700000000.000500 — required.
             Matched exactly; a thread reply is found by its own ts.

On success, prints the adapter's JSON:
  {"ok":true,"channel":...,"ts":...,"thread_ts":...,"user":...,
   "text":...,"permalink":...,"edited":false}
`thread_ts` is empty for a top-level message (including a thread parent).
`edited` is true when the message was edited after posting.

Refusals exit non-zero and print {"ok":false,"error":"<code>",...}:
  bad_request          malformed request (missing/invalid channel or ts)
  message_not_found    no message has exactly that ts in that channel
  bot_message          the message was posted by a bot
  unsupported_subtype  a system message, not one a person wrote
  no_author            the message carries no user id
  foreign_workspace    the author belongs to another Slack team
  slack_error          Slack returned ok=false (its error in slack_error)
  rate_limited         Slack rate-limited the read (retry_after seconds)
  slack_http_error     Slack returned another non-2xx status
  slack_unreachable    the adapter could not reach Slack

Example:
  gc slack-channel verify-message --channel C0123 --ts 1700000000.000500

Requires: GC_CITY_NAME + GC_API_BASE_URL, plus `jq` and `curl` on PATH. The
slack-channel service must be running.
