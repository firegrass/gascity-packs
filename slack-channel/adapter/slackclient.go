package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

// slackHTTPError is a non-2xx HTTP status from the Slack web API. It keeps
// the status and Retry-After header so a caller can tell a rate limit (429)
// from any other transport-level refusal; its message is unchanged from the
// plain error the outbound verbs have always surfaced.
type slackHTTPError struct {
	Status     int
	RetryAfter string // raw Retry-After header, "" when absent
	Body       string
}

func (e *slackHTTPError) Error() string {
	return fmt.Sprintf("slack http %d: %s", e.Status, e.Body)
}

// slackCall POSTs req as JSON to a Slack web API method and decodes the
// response into *Resp. It applies the bot token, a per-call timeout, and
// surfaces any non-2xx HTTP status as an error. The outbound Slack write
// calls (chat.postMessage, reactions.add) differ only in method, request,
// and response type, so they share this body.
func slackCall[Resp any](ctx context.Context, s *server, method string, req any) (*Resp, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, slackPostTimeout)
	defer cancel()
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.slackAPIBase+method, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json; charset=utf-8")
	return slackDo[Resp](s, httpReq, method)
}

// slackGet calls a Slack web API read method with its arguments in the query
// string. Slack's read methods (conversations.replies, conversations.history,
// chat.getPermalink) do not accept a JSON body, so they cannot share
// slackCall's request encoding — only its response handling.
func slackGet[Resp any](ctx context.Context, s *server, method string, params url.Values) (*Resp, error) {
	ctx, cancel := context.WithTimeout(ctx, slackPostTimeout)
	defer cancel()
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.slackAPIBase+method+"?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	return slackDo[Resp](s, httpReq, method)
}

// slackDo sends a prepared Slack request with the bot token, reads the whole
// body, maps a non-2xx status to *slackHTTPError, and decodes the rest.
func slackDo[Resp any](s *server, httpReq *http.Request, method string) (*Resp, error) {
	httpReq.Header.Set("Authorization", "Bearer "+s.cfg.botToken)
	resp, err := s.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("post %s: %w", method, err)
	}
	defer func() { _ = resp.Body.Close() }()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read slack response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &slackHTTPError{
			Status:     resp.StatusCode,
			RetryAfter: strings.TrimSpace(resp.Header.Get("Retry-After")),
			Body:       strings.TrimSpace(string(respBody)),
		}
	}
	var out Resp
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, fmt.Errorf("decode slack response: %w", err)
	}
	return &out, nil
}

// postToSlack posts a message via chat.postMessage. Identity fields
// (username/icon_url/icon_emoji) on req are forwarded verbatim — the caller
// injects them from the identity registry.
func (s *server) postToSlack(ctx context.Context, req slackPostMessageReq) (*slackPostMessageResp, error) {
	return slackCall[slackPostMessageResp](ctx, s, "/chat.postMessage", req)
}

// addReaction adds an emoji reaction via reactions.add. The emoji name is
// forwarded verbatim minus surrounding colons (callers may send "eyes" or
// ":eyes:").
func (s *server) addReaction(ctx context.Context, channel, timestamp, emoji string) (*slackReactionsAddResp, error) {
	return slackCall[slackReactionsAddResp](ctx, s, "/reactions.add", slackReactionsAddReq{
		Channel:   channel,
		Name:      strings.Trim(emoji, ":"),
		Timestamp: timestamp,
	})
}
