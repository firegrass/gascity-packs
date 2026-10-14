package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

// verify-message answers "who wrote this Slack message, in which thread, with
// what text" by asking Slack, so a caller can record a human's answer with
// provenance without ever holding the bot token. The adapter is the only
// process with SLACK_BOT_TOKEN; agents reach this endpoint only through gc's
// /svc/slack-channel proxy onto the internal (supervisor-only) listener.
//
// It never returns an empty success: a message that cannot be found, was
// written by a bot, belongs to another workspace, or cannot be read from
// Slack is a typed refusal — a non-2xx status with {"ok":false,"error":
// "<code>"} — so a caller cannot mistake "Slack said nothing" for "a human
// said this".

// verifyMaxPages bounds the conversations.replies pagination walk. The
// oldest/latest window narrows the page set to the thread parent plus at
// most the one requested reply, so more than a couple of pages means Slack
// is not honouring the window; give up rather than walk a whole thread.
const verifyMaxPages = 10

// Refusal codes returned in the "error" field of a verify-message refusal.
const (
	verifyErrBadRequest       = "bad_request"
	verifyErrNotFound         = "message_not_found"
	verifyErrBotMessage       = "bot_message"
	verifyErrNoAuthor         = "no_author"
	verifyErrUnsupported      = "unsupported_subtype"
	verifyErrForeignWorkspace = "foreign_workspace"
	verifyErrSlack            = "slack_error"
	verifyErrRateLimited      = "rate_limited"
	verifyErrSlackHTTP        = "slack_http_error"
	verifyErrSlackUnreachable = "slack_unreachable"
)

var (
	slackTSPattern      = regexp.MustCompile(`^[0-9]+\.[0-9]+$`)
	slackChannelPattern = regexp.MustCompile(`^[A-Za-z0-9]+$`)
)

// verifyUserSubtypes are the message subtypes that still carry a human
// author's own words. Any other subtype (joins, topic changes, tombstones,
// integrations) is not an answer a person wrote and is refused.
var verifyUserSubtypes = map[string]bool{
	"":                 true,
	"thread_broadcast": true, // a thread reply also sent to the channel
	"file_share":       true, // a message posted with a file attached
}

type verifyMessageRequest struct {
	Channel string `json:"channel"`
	TS      string `json:"ts"`
}

// verifyMessageResult is the success body. ThreadTS is empty for a
// top-level message (including a thread parent, whose own thread_ts equals
// its ts); Edited is true when Slack reports the message was edited after
// posting, so a caller can refuse an answer that changed.
type verifyMessageResult struct {
	OK        bool   `json:"ok"`
	Channel   string `json:"channel"`
	TS        string `json:"ts"`
	ThreadTS  string `json:"thread_ts"`
	User      string `json:"user"`
	Text      string `json:"text"`
	Permalink string `json:"permalink"`
	Edited    bool   `json:"edited"`
}

// verifyRefusal is a typed verify-message failure. It is written as
// {"ok":false,"error":Code,"detail":...} with Status, plus the upstream
// Slack error string and Retry-After seconds when they apply.
type verifyRefusal struct {
	Status     int
	Code       string
	Detail     string
	SlackError string
	RetryAfter int // seconds; 0 = not supplied
}

func (r *verifyRefusal) Error() string {
	return fmt.Sprintf("%s: %s", r.Code, r.Detail)
}

func (r *verifyRefusal) write(w http.ResponseWriter) {
	body := map[string]any{"ok": false, "error": r.Code, "detail": r.Detail}
	if r.SlackError != "" {
		body["slack_error"] = r.SlackError
	}
	if r.RetryAfter > 0 {
		body["retry_after"] = r.RetryAfter
		w.Header().Set("Retry-After", strconv.Itoa(r.RetryAfter))
	}
	writeJSON(w, r.Status, body)
}

func badVerifyRequest(detail string) *verifyRefusal {
	return &verifyRefusal{Status: http.StatusBadRequest, Code: verifyErrBadRequest, Detail: detail}
}

// slackRefusal maps a Slack transport or API failure for method into a
// refusal. A 429 (or an ok:false "ratelimited") becomes rate_limited with
// Retry-After surfaced; any other ok:false error passes Slack's string
// through; anything else is a gateway failure.
func slackRefusal(method string, err error, apiError string) *verifyRefusal {
	if err == nil {
		if apiError == "ratelimited" {
			return &verifyRefusal{Status: http.StatusTooManyRequests, Code: verifyErrRateLimited,
				Detail: method + " rate limited", SlackError: apiError}
		}
		if apiError == "" {
			apiError = "unknown_error"
		}
		return &verifyRefusal{Status: http.StatusBadGateway, Code: verifyErrSlack,
			Detail: method + " returned ok=false", SlackError: apiError}
	}
	var httpErr *slackHTTPError
	if errors.As(err, &httpErr) {
		if httpErr.Status == http.StatusTooManyRequests {
			retry, _ := strconv.Atoi(httpErr.RetryAfter)
			if retry < 0 {
				retry = 0
			}
			return &verifyRefusal{Status: http.StatusTooManyRequests, Code: verifyErrRateLimited,
				Detail: method + " rate limited", RetryAfter: retry}
		}
		return &verifyRefusal{Status: http.StatusBadGateway, Code: verifyErrSlackHTTP,
			Detail: fmt.Sprintf("%s: %v", method, err)}
	}
	return &verifyRefusal{Status: http.StatusBadGateway, Code: verifyErrSlackUnreachable,
		Detail: fmt.Sprintf("%s: %v", method, err)}
}

// handleVerifyMessage serves POST /verify-message on the internal mux only.
func (s *server) handleVerifyMessage() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var req verifyMessageRequest
		dec := json.NewDecoder(r.Body)
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			badVerifyRequest(fmt.Sprintf("decode: %v", err)).write(w)
			return
		}
		res, refusal := s.verifyMessage(r.Context(), strings.TrimSpace(req.Channel), strings.TrimSpace(req.TS))
		if refusal != nil {
			log.Printf("verify-message: channel=%s ts=%s refused error=%s slack_error=%s detail=%s",
				req.Channel, req.TS, refusal.Code, refusal.SlackError, refusal.Detail)
			refusal.write(w)
			return
		}
		log.Printf("verify-message: channel=%s ts=%s thread_ts=%s user=%s edited=%t",
			res.Channel, res.TS, res.ThreadTS, res.User, res.Edited)
		writeJSON(w, http.StatusOK, res)
	}
}

// verifyMessage reads one message back from Slack and checks that a human in
// this workspace wrote it.
func (s *server) verifyMessage(ctx context.Context, channel, ts string) (*verifyMessageResult, *verifyRefusal) {
	switch {
	case channel == "":
		return nil, badVerifyRequest("channel is required")
	case !slackChannelPattern.MatchString(channel):
		return nil, badVerifyRequest(fmt.Sprintf("channel %q is not a Slack conversation id", channel))
	case ts == "":
		return nil, badVerifyRequest("ts is required")
	case !slackTSPattern.MatchString(ts):
		return nil, badVerifyRequest(fmt.Sprintf("ts %q is not a Slack message timestamp", ts))
	}

	msg, refusal := s.findMessage(ctx, channel, ts)
	if refusal != nil {
		return nil, refusal
	}
	if msg.BotID != "" || msg.Subtype == "bot_message" {
		return nil, &verifyRefusal{Status: http.StatusUnprocessableEntity, Code: verifyErrBotMessage,
			Detail: "message was posted by a bot, not a person"}
	}
	if !verifyUserSubtypes[msg.Subtype] {
		return nil, &verifyRefusal{Status: http.StatusUnprocessableEntity, Code: verifyErrUnsupported,
			Detail: fmt.Sprintf("message subtype %q is not a person's message", msg.Subtype)}
	}
	if msg.User == "" {
		return nil, &verifyRefusal{Status: http.StatusUnprocessableEntity, Code: verifyErrNoAuthor,
			Detail: "message carries no user id"}
	}
	// Workspace gate, as on the inbound path: a message whose author belongs
	// to another team (a Slack Connect guest) is not this workspace's answer.
	// An absent team is allowed through, as an absent team_id is inbound.
	for _, team := range []string{msg.Team, msg.UserTeam} {
		if team != "" && team != s.cfg.workspaceID {
			return nil, &verifyRefusal{Status: http.StatusForbidden, Code: verifyErrForeignWorkspace,
				Detail: fmt.Sprintf("message author belongs to team %s, not %s", team, s.cfg.workspaceID)}
		}
	}

	link, err := slackGet[slackPermalinkResp](ctx, s, "/chat.getPermalink",
		url.Values{"channel": {channel}, "message_ts": {ts}})
	if err != nil {
		return nil, slackRefusal("chat.getPermalink", err, "")
	}
	if !link.OK {
		return nil, slackRefusal("chat.getPermalink", nil, link.Error)
	}

	threadTS := msg.ThreadTS
	if threadTS == msg.TS {
		threadTS = "" // a thread parent is a top-level message
	}
	return &verifyMessageResult{
		OK:        true,
		Channel:   channel,
		TS:        msg.TS,
		ThreadTS:  threadTS,
		User:      msg.User,
		Text:      msg.Text,
		Permalink: link.Permalink,
		Edited:    len(msg.Edited) > 0 && string(msg.Edited) != "null",
	}, nil
}

// findMessage locates the message whose ts equals ts exactly. It asks
// conversations.replies first — which finds both a thread reply and a
// top-level message, but always returns the thread parent first, so the
// match is by exact ts across pages — then falls back to
// conversations.history over the inclusive [ts, ts] window when Slack
// reports no thread for the ts.
func (s *server) findMessage(ctx context.Context, channel, ts string) (*slackStoredMessage, *verifyRefusal) {
	cursor := ""
	for page := 0; page < verifyMaxPages; page++ {
		params := url.Values{
			"channel":   {channel},
			"ts":        {ts},
			"oldest":    {ts},
			"latest":    {ts},
			"inclusive": {"true"},
			"limit":     {"1"},
		}
		if cursor != "" {
			params.Set("cursor", cursor)
		}
		resp, err := slackGet[slackMessagesResp](ctx, s, "/conversations.replies", params)
		if err != nil {
			return nil, slackRefusal("conversations.replies", err, "")
		}
		if !resp.OK {
			if resp.Error == "thread_not_found" {
				break
			}
			return nil, slackRefusal("conversations.replies", nil, resp.Error)
		}
		if m := matchTS(resp.Messages, ts); m != nil {
			return m, nil
		}
		cursor = resp.ResponseMetadata.NextCursor
		if cursor == "" {
			break
		}
	}

	resp, err := slackGet[slackMessagesResp](ctx, s, "/conversations.history", url.Values{
		"channel":   {channel},
		"oldest":    {ts},
		"latest":    {ts},
		"inclusive": {"true"},
		"limit":     {"1"},
	})
	if err != nil {
		return nil, slackRefusal("conversations.history", err, "")
	}
	if !resp.OK {
		return nil, slackRefusal("conversations.history", nil, resp.Error)
	}
	if m := matchTS(resp.Messages, ts); m != nil {
		return m, nil
	}
	return nil, &verifyRefusal{Status: http.StatusNotFound, Code: verifyErrNotFound,
		Detail: fmt.Sprintf("no message with ts %s in channel %s", ts, channel)}
}

func matchTS(msgs []slackStoredMessage, ts string) *slackStoredMessage {
	for i := range msgs {
		if msgs[i].TS == ts {
			return &msgs[i]
		}
	}
	return nil
}
