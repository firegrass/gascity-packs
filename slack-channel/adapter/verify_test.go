package main

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
)

// fakeSlackReads stands in for the Slack read methods verify-message calls.
// Each method's handler returns (status, headers, body); every request's
// path and query are recorded so tests can assert what was asked.
type fakeSlackReads struct {
	srv   *httptest.Server
	mu    sync.Mutex
	calls []recordedCall

	replies   func(q url.Values) (int, map[string]string, any)
	history   func(q url.Values) (int, map[string]string, any)
	permalink func(q url.Values) (int, map[string]string, any)
}

type recordedCall struct {
	method string // Slack method, e.g. "conversations.replies"
	query  url.Values
	auth   string
}

func newFakeSlackReads(t *testing.T, s *server) *fakeSlackReads {
	t.Helper()
	f := &fakeSlackReads{
		permalink: func(q url.Values) (int, map[string]string, any) {
			return http.StatusOK, nil, slackPermalinkResp{OK: true, Channel: q.Get("channel"),
				Permalink: "https://example.slack.com/archives/" + q.Get("channel") + "/p" + strings.ReplaceAll(q.Get("message_ts"), ".", "")}
		},
	}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("slack read %s used method %s, want GET", r.URL.Path, r.Method)
		}
		method := strings.TrimPrefix(r.URL.Path, "/")
		f.mu.Lock()
		f.calls = append(f.calls, recordedCall{method: method, query: r.URL.Query(), auth: r.Header.Get("Authorization")})
		var h func(url.Values) (int, map[string]string, any)
		switch method {
		case "conversations.replies":
			h = f.replies
		case "conversations.history":
			h = f.history
		case "chat.getPermalink":
			h = f.permalink
		}
		f.mu.Unlock()
		if h == nil {
			t.Errorf("unexpected slack call %s", method)
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		status, headers, body := h(r.URL.Query())
		for k, v := range headers {
			w.Header().Set(k, v)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if raw, ok := body.(rawBody); ok {
			_, _ = w.Write([]byte(raw))
			return
		}
		_ = json.NewEncoder(w).Encode(body)
	}))
	t.Cleanup(f.srv.Close)
	s.cfg.slackAPIBase = f.srv.URL
	return f
}

// rawBody is written to the response verbatim instead of JSON-encoded, for
// bodies that are not valid JSON.
type rawBody string

func (f *fakeSlackReads) callsTo(method string) []recordedCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []recordedCall
	for _, c := range f.calls {
		if c.method == method {
			out = append(out, c)
		}
	}
	return out
}

func (f *fakeSlackReads) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func okMessages(cursor string, msgs ...slackStoredMessage) (int, map[string]string, any) {
	resp := slackMessagesResp{OK: true, Messages: msgs, HasMore: cursor != ""}
	resp.ResponseMetadata.NextCursor = cursor
	return http.StatusOK, nil, resp
}

func slackErr(code string) (int, map[string]string, any) {
	return http.StatusOK, nil, slackMessagesResp{OK: false, Error: code}
}

func verify(t *testing.T, s *server, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := doJSON(s.handleVerifyMessage(), body)
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("response is not JSON: %v body=%s", err, rec.Body.String())
	}
	return rec, out
}

func wantRefusal(t *testing.T, rec *httptest.ResponseRecorder, out map[string]any, status int, code string) {
	t.Helper()
	if rec.Code != status {
		t.Fatalf("status = %d, want %d; body=%s", rec.Code, status, rec.Body.String())
	}
	if out["ok"] != false || out["error"] != code {
		t.Fatalf("body = %v, want ok=false error=%s", out, code)
	}
}

var (
	parent = slackStoredMessage{Type: "message", User: "UBOT", BotID: "B1", Text: "Which region?", TS: "1700000000.000100", ThreadTS: "1700000000.000100"}
	reply  = slackStoredMessage{Type: "message", User: "UOWNER", Team: "T123", Text: "eu-west", TS: "1700000000.000500", ThreadTS: "1700000000.000100"}
)

func TestVerifyMessageThreadReplySelectedByTSAcrossPages(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	slack.replies = func(q url.Values) (int, map[string]string, any) {
		// The thread parent always comes first; the reply is on page two.
		if q.Get("cursor") == "" {
			return okMessages("page2", parent)
		}
		return okMessages("", parent, reply)
	}

	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	want := map[string]any{
		"ok": true, "channel": "C1", "ts": "1700000000.000500", "thread_ts": "1700000000.000100",
		"user": "UOWNER", "text": "eu-west", "edited": false,
		"permalink": "https://example.slack.com/archives/C1/p1700000000000500",
	}
	for k, v := range want {
		if out[k] != v {
			t.Errorf("%s = %v, want %v", k, out[k], v)
		}
	}

	calls := slack.callsTo("conversations.replies")
	if len(calls) != 2 {
		t.Fatalf("replies calls = %d, want 2", len(calls))
	}
	q := calls[0].query
	for k, v := range map[string]string{"channel": "C1", "ts": "1700000000.000500", "oldest": "1700000000.000500",
		"latest": "1700000000.000500", "inclusive": "true", "limit": "1"} {
		if q.Get(k) != v {
			t.Errorf("replies %s = %q, want %q", k, q.Get(k), v)
		}
	}
	if calls[1].query.Get("cursor") != "page2" {
		t.Errorf("second page cursor = %q", calls[1].query.Get("cursor"))
	}
	if calls[0].auth != "Bearer xoxb-test" {
		t.Errorf("Authorization = %q", calls[0].auth)
	}
	if n := len(slack.callsTo("conversations.history")); n != 0 {
		t.Errorf("history called %d times for a thread reply", n)
	}
	if pl := slack.callsTo("chat.getPermalink"); len(pl) != 1 || pl[0].query.Get("message_ts") != "1700000000.000500" {
		t.Errorf("permalink calls = %+v", pl)
	}
}

func TestVerifyMessageTopLevel(t *testing.T) {
	top := slackStoredMessage{Type: "message", User: "UOWNER", Text: "ship it", TS: "1700000100.000200"}

	t.Run("via replies", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		// A thread parent: its own thread_ts equals its ts, so it is reported
		// as top-level.
		withThread := top
		withThread.ThreadTS = top.TS
		slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", withThread) }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000100.000200"}`)
		if rec.Code != http.StatusOK || out["thread_ts"] != "" || out["user"] != "UOWNER" || out["text"] != "ship it" {
			t.Fatalf("status=%d body=%v", rec.Code, out)
		}
	})

	t.Run("via history fallback", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) { return slackErr("thread_not_found") }
		slack.history = func(url.Values) (int, map[string]string, any) { return okMessages("", top) }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000100.000200"}`)
		if rec.Code != http.StatusOK || out["thread_ts"] != "" || out["user"] != "UOWNER" || out["edited"] != false {
			t.Fatalf("status=%d body=%v", rec.Code, out)
		}
		h := slack.callsTo("conversations.history")
		if len(h) != 1 {
			t.Fatalf("history calls = %d", len(h))
		}
		for k, v := range map[string]string{"channel": "C1", "oldest": "1700000100.000200",
			"latest": "1700000100.000200", "inclusive": "true"} {
			if h[0].query.Get(k) != v {
				t.Errorf("history %s = %q, want %q", k, h[0].query.Get(k), v)
			}
		}
	})
}

func TestVerifyMessageRefusesBotMessage(t *testing.T) {
	for name, msg := range map[string]slackStoredMessage{
		"bot_id":              {User: "U9", BotID: "B1", Text: "x", TS: "1.000001"},
		"bot_message subtype": {Subtype: "bot_message", Text: "x", TS: "1.000001"},
	} {
		t.Run(name, func(t *testing.T) {
			srv := newTestServer(t)
			slack := newFakeSlackReads(t, srv)
			slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", msg) }
			rec, out := verify(t, srv, `{"channel":"C1","ts":"1.000001"}`)
			wantRefusal(t, rec, out, http.StatusUnprocessableEntity, verifyErrBotMessage)
			if n := len(slack.callsTo("chat.getPermalink")); n != 0 {
				t.Errorf("permalink fetched for a refused message")
			}
		})
	}
}

func TestVerifyMessageRefusesNonPersonMessages(t *testing.T) {
	cases := map[string]struct {
		msg    slackStoredMessage
		status int
		code   string
	}{
		"system subtype":   {slackStoredMessage{Subtype: "channel_join", User: "U1", TS: "1.000001"}, http.StatusUnprocessableEntity, verifyErrUnsupported},
		"no user":          {slackStoredMessage{Text: "x", TS: "1.000001"}, http.StatusUnprocessableEntity, verifyErrNoAuthor},
		"foreign team":     {slackStoredMessage{User: "U1", Team: "T999", TS: "1.000001"}, http.StatusForbidden, verifyErrForeignWorkspace},
		"foreign usr team": {slackStoredMessage{User: "U1", UserTeam: "T999", TS: "1.000001"}, http.StatusForbidden, verifyErrForeignWorkspace},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			srv := newTestServer(t)
			slack := newFakeSlackReads(t, srv)
			slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", tc.msg) }
			rec, out := verify(t, srv, `{"channel":"C1","ts":"1.000001"}`)
			wantRefusal(t, rec, out, tc.status, tc.code)
		})
	}
}

func TestVerifyMessageReportsEdited(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	edited := reply
	edited.Edited = json.RawMessage(`{"user":"UOWNER","ts":"1700000000.000900"}`)
	slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, edited) }
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	if rec.Code != http.StatusOK || out["edited"] != true {
		t.Fatalf("status=%d body=%v", rec.Code, out)
	}
}

func TestVerifyMessageNotFound(t *testing.T) {
	t.Run("no thread, no history", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) { return slackErr("thread_not_found") }
		slack.history = func(url.Values) (int, map[string]string, any) { return okMessages("") }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000777"}`)
		wantRefusal(t, rec, out, http.StatusNotFound, verifyErrNotFound)
	})
	t.Run("thread without the ts", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		// Only the parent comes back: a near-miss ts must not be accepted.
		slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent) }
		slack.history = func(url.Values) (int, map[string]string, any) { return okMessages("") }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000777"}`)
		wantRefusal(t, rec, out, http.StatusNotFound, verifyErrNotFound)
		if n := len(slack.callsTo("chat.getPermalink")); n != 0 {
			t.Errorf("permalink fetched for a missing message")
		}
	})
}

func TestVerifyMessageRateLimited(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	slack.replies = func(url.Values) (int, map[string]string, any) {
		return http.StatusTooManyRequests, map[string]string{"Retry-After": "30"}, slackMessagesResp{OK: false, Error: "ratelimited"}
	}
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	wantRefusal(t, rec, out, http.StatusTooManyRequests, verifyErrRateLimited)
	if out["retry_after"] != float64(30) {
		t.Errorf("retry_after = %v, want 30", out["retry_after"])
	}
	if got := rec.Header().Get("Retry-After"); got != "30" {
		t.Errorf("Retry-After header = %q, want 30", got)
	}
}

func TestVerifyMessageSlackError(t *testing.T) {
	t.Run("replies", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) { return slackErr("channel_not_found") }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlack)
		if out["slack_error"] != "channel_not_found" {
			t.Errorf("slack_error = %v", out["slack_error"])
		}
		if n := len(slack.callsTo("conversations.history")); n != 0 {
			t.Errorf("history consulted after a non-thread_not_found error")
		}
	})
	t.Run("permalink", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, reply) }
		slack.permalink = func(url.Values) (int, map[string]string, any) {
			return http.StatusOK, nil, slackPermalinkResp{OK: false, Error: "message_not_found"}
		}
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlack)
		if out["slack_error"] != "message_not_found" {
			t.Errorf("slack_error = %v", out["slack_error"])
		}
	})
	t.Run("http 500", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) {
			return http.StatusInternalServerError, nil, map[string]any{}
		}
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlackHTTP)
	})
	t.Run("unreachable", func(t *testing.T) {
		srv := newTestServer(t)
		srv.cfg.slackAPIBase = "http://127.0.0.1:1" // connection refused
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlackUnreachable)
	})
}

func TestVerifyMessageMalformedRequest(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	for name, body := range map[string]string{
		"bad json":      `{`,
		"no channel":    `{"ts":"1700000000.000500"}`,
		"no ts":         `{"channel":"C1"}`,
		"ts not a ts":   `{"channel":"C1","ts":"yesterday"}`,
		"channel path":  `{"channel":"C1/../x","ts":"1700000000.000500"}`,
		"unknown field": `{"channel":"C1","ts":"1700000000.000500","user":"UOWNER"}`,
	} {
		t.Run(name, func(t *testing.T) {
			rec, out := verify(t, srv, body)
			wantRefusal(t, rec, out, http.StatusBadRequest, verifyErrBadRequest)
		})
	}
	if n := slack.count(); n != 0 {
		t.Errorf("malformed requests reached Slack %d times", n)
	}
}

func TestVerifyMessageOnlyOnInternalMux(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, reply) }
	body := `{"channel":"C1","ts":"1700000000.000500"}`

	pub := httptest.NewRecorder()
	newPublicMux(srv).ServeHTTP(pub, httptest.NewRequest(http.MethodPost, "/verify-message", strings.NewReader(body)))
	if pub.Code != http.StatusNotFound {
		t.Fatalf("public mux status = %d, want 404", pub.Code)
	}
	if n := slack.count(); n != 0 {
		t.Fatalf("public mux reached Slack %d times", n)
	}

	internal := httptest.NewRecorder()
	newInternalMux(srv).ServeHTTP(internal, httptest.NewRequest(http.MethodPost, "/verify-message", strings.NewReader(body)))
	if internal.Code != http.StatusOK {
		t.Fatalf("internal mux status = %d body=%s", internal.Code, internal.Body.String())
	}
}

// --- review hardening (N1-N5) --------------------------------------------

func TestVerifyMessageRefusesAppPostedMessageWithoutBotID(t *testing.T) {
	cases := map[string]slackStoredMessage{
		"app_id and bot_profile": {User: "UOWNER", Team: "T123", AppID: "A1",
			BotProfile: json.RawMessage(`{"id":"B1","app_id":"A1","name":"crew"}`), Text: "x", TS: "1.000001"},
		"app_id only":      {User: "UOWNER", AppID: "A1", Text: "x", TS: "1.000001"},
		"bot_profile only": {User: "UOWNER", BotProfile: json.RawMessage(`{"id":"B1"}`), Text: "x", TS: "1.000001"},
	}
	for name, msg := range cases {
		t.Run(name, func(t *testing.T) {
			srv := newTestServer(t)
			slack := newFakeSlackReads(t, srv)
			slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", msg) }
			rec, out := verify(t, srv, `{"channel":"C1","ts":"1.000001"}`)
			wantRefusal(t, rec, out, http.StatusUnprocessableEntity, verifyErrBotMessage)
		})
	}
}

func TestVerifyMessageRefusesEmptyPermalink(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, reply) }
	slack.permalink = func(url.Values) (int, map[string]string, any) {
		return http.StatusOK, nil, slackPermalinkResp{OK: true, Channel: "C1"}
	}
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlack)
	if d, _ := out["detail"].(string); !strings.Contains(d, "empty permalink") {
		t.Errorf("detail = %q", d)
	}
}

func TestVerifyMessageRepliesPageBound(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	// Every page returns only the parent and another cursor: the walk never
	// ends, so it must be refused, not fall through to history or not-found.
	slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("more", parent) }
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlack)
	if d, _ := out["detail"].(string); !strings.Contains(d, "replies pagination exceeded") {
		t.Errorf("detail = %q", d)
	}
	if n := len(slack.callsTo("conversations.replies")); n != verifyMaxPages {
		t.Errorf("replies calls = %d, want %d", n, verifyMaxPages)
	}
	if n := len(slack.callsTo("conversations.history")); n != 0 {
		t.Errorf("history consulted after the page bound: %d calls", n)
	}
}

func TestVerifyMessageHistoryFallbackAfterWalkWithoutMatch(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	top := slackStoredMessage{User: "UOWNER", Text: "yes", TS: "1700000000.000777"}
	slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent) }
	slack.history = func(url.Values) (int, map[string]string, any) { return okMessages("", top) }
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000777"}`)
	if rec.Code != http.StatusOK || out["ts"] != "1700000000.000777" || out["user"] != "UOWNER" {
		t.Fatalf("status=%d body=%v", rec.Code, out)
	}
}

func TestVerifyMessageUndecodableSlackBody(t *testing.T) {
	for name, setup := range map[string]func(*fakeSlackReads){
		"replies": func(f *fakeSlackReads) {
			f.replies = func(url.Values) (int, map[string]string, any) {
				return http.StatusOK, nil, rawBody("<html>proxy</html>")
			}
		},
		"permalink": func(f *fakeSlackReads) {
			f.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, reply) }
			f.permalink = func(url.Values) (int, map[string]string, any) { return http.StatusOK, nil, rawBody("{") }
		},
	} {
		t.Run(name, func(t *testing.T) {
			srv := newTestServer(t)
			setup(newFakeSlackReads(t, srv))
			rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
			wantRefusal(t, rec, out, http.StatusBadGateway, verifyErrSlackBadResponse)
		})
	}
}

func TestVerifyMessageAcceptsPersonSubtypes(t *testing.T) {
	for _, subtype := range []string{"thread_broadcast", "file_share"} {
		t.Run(subtype, func(t *testing.T) {
			srv := newTestServer(t)
			slack := newFakeSlackReads(t, srv)
			msg := reply
			msg.Subtype = subtype
			slack.replies = func(url.Values) (int, map[string]string, any) { return okMessages("", parent, msg) }
			rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
			if rec.Code != http.StatusOK || out["user"] != "UOWNER" {
				t.Fatalf("status=%d body=%v", rec.Code, out)
			}
		})
	}
}

func TestVerifyMessageEditedNullIsNotEdited(t *testing.T) {
	srv := newTestServer(t)
	slack := newFakeSlackReads(t, srv)
	slack.replies = func(url.Values) (int, map[string]string, any) {
		return http.StatusOK, nil, rawBody(`{"ok":true,"messages":[{"type":"message","user":"UOWNER","text":"eu-west",` +
			`"ts":"1700000000.000500","thread_ts":"1700000000.000100","edited":null}]}`)
	}
	rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
	if rec.Code != http.StatusOK || out["edited"] != false {
		t.Fatalf("status=%d body=%v", rec.Code, out)
	}
}

func TestVerifyMessageRateLimitedVariants(t *testing.T) {
	t.Run("ok false ratelimited at 200", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) { return slackErr("ratelimited") }
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusTooManyRequests, verifyErrRateLimited)
	})
	t.Run("429 without Retry-After", func(t *testing.T) {
		srv := newTestServer(t)
		slack := newFakeSlackReads(t, srv)
		slack.replies = func(url.Values) (int, map[string]string, any) {
			return http.StatusTooManyRequests, nil, slackMessagesResp{OK: false, Error: "ratelimited"}
		}
		rec, out := verify(t, srv, `{"channel":"C1","ts":"1700000000.000500"}`)
		wantRefusal(t, rec, out, http.StatusTooManyRequests, verifyErrRateLimited)
		if _, ok := out["retry_after"]; ok {
			t.Errorf("retry_after present without a Slack Retry-After: %v", out["retry_after"])
		}
		if got := rec.Header().Get("Retry-After"); got != "" {
			t.Errorf("Retry-After header = %q, want none", got)
		}
	})
}

func TestVerifyMessageRefusalLogCannotForgeALine(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	srv := newTestServer(t)
	newFakeSlackReads(t, srv)
	rec, out := verify(t, srv, `{"channel":"C1\nverify-message: forged","ts":"1.000001"}`)
	wantRefusal(t, rec, out, http.StatusBadRequest, verifyErrBadRequest)
	if lines := strings.Count(strings.TrimRight(buf.String(), "\n"), "\n"); lines != 0 {
		t.Fatalf("refusal log spans %d extra lines: %q", lines, buf.String())
	}
	if !strings.Contains(buf.String(), `channel="C1\nverify-message: forged"`) {
		t.Errorf("channel not %%q-quoted in log: %q", buf.String())
	}
}
