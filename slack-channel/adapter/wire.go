package main

import (
	"encoding/json"
	"time"
)

// --- Slack Events API wire types (subset Tier 2 reads) --------------------

type slackEventEnvelope struct {
	Type      string          `json:"type"`
	Challenge string          `json:"challenge,omitempty"`
	TeamID    string          `json:"team_id,omitempty"`
	Event     json.RawMessage `json:"event,omitempty"`
}

// slackMessageEvent is the subset of an app_mention / message event Tier 2
// reads. Tier 2 widens beyond Tier 1's app_mention-only path to plain
// message.* events delivered in bound channels.
type slackMessageEvent struct {
	Type        string `json:"type"`
	Subtype     string `json:"subtype,omitempty"`
	User        string `json:"user,omitempty"`
	BotID       string `json:"bot_id,omitempty"`
	Text        string `json:"text,omitempty"`
	Channel     string `json:"channel,omitempty"`
	TS          string `json:"ts,omitempty"`
	ThreadTS    string `json:"thread_ts,omitempty"`
	ChannelType string `json:"channel_type,omitempty"`
}

// --- gc extmsg wire types (mirrored, wire-compatible only) ----------------

type conversationRef struct {
	ScopeID        string `json:"scope_id"`
	Provider       string `json:"provider"`
	AccountID      string `json:"account_id"`
	ConversationID string `json:"conversation_id"`
	Kind           string `json:"kind"`
}

type externalActor struct {
	ID          string `json:"id"`
	DisplayName string `json:"display_name"`
	IsBot       bool   `json:"is_bot"`
}

type externalInboundMessage struct {
	ProviderMessageID string          `json:"provider_message_id"`
	Conversation      conversationRef `json:"conversation"`
	Actor             externalActor   `json:"actor"`
	Text              string          `json:"text"`
	ExplicitTarget    string          `json:"explicit_target,omitempty"`
	ReplyToMessageID  string          `json:"reply_to_message_id,omitempty"`
	DedupKey          string          `json:"dedup_key,omitempty"`
	ReceivedAt        time.Time       `json:"received_at"`
}

type adapterCapabilities struct {
	SupportsChildConversations bool `json:"SupportsChildConversations"`
	SupportsAttachments        bool `json:"SupportsAttachments"`
	MaxMessageLength           int  `json:"MaxMessageLength"`
}

type adapterRegisterRequest struct {
	Provider     string              `json:"provider"`
	AccountID    string              `json:"account_id"`
	Name         string              `json:"name,omitempty"`
	CallbackURL  string              `json:"callback_url,omitempty"`
	Capabilities adapterCapabilities `json:"capabilities,omitempty"`
}

// --- Slack web API wire types --------------------------------------------

// slackPostMessageReq is the chat.postMessage payload. Username/IconURL/
// IconEmoji carry a per-session identity override (chat:write.customize)
// when one is registered; they are omitted otherwise.
type slackPostMessageReq struct {
	Channel   string `json:"channel"`
	Text      string `json:"text"`
	ThreadTS  string `json:"thread_ts,omitempty"`
	Username  string `json:"username,omitempty"`
	IconURL   string `json:"icon_url,omitempty"`
	IconEmoji string `json:"icon_emoji,omitempty"`
}

type slackPostMessageResp struct {
	OK      bool   `json:"ok"`
	TS      string `json:"ts,omitempty"`
	Channel string `json:"channel,omitempty"`
	Error   string `json:"error,omitempty"`
}

type slackReactionsAddReq struct {
	Channel   string `json:"channel"`
	Name      string `json:"name"`
	Timestamp string `json:"timestamp"`
}

type slackReactionsAddResp struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

// slackStoredMessage is the subset of a message object returned by
// conversations.replies / conversations.history that verify-message reads.
// Edited and BotProfile are kept raw: their presence (Slack sets
// {"user","ts"} on an edited message, and a bot_profile object on anything an
// app posted) is the whole signal.
type slackStoredMessage struct {
	Type       string          `json:"type,omitempty"`
	Subtype    string          `json:"subtype,omitempty"`
	User       string          `json:"user,omitempty"`
	BotID      string          `json:"bot_id,omitempty"`
	AppID      string          `json:"app_id,omitempty"`
	BotProfile json.RawMessage `json:"bot_profile,omitempty"`
	Team       string          `json:"team,omitempty"`
	UserTeam   string          `json:"user_team,omitempty"`
	Text       string          `json:"text,omitempty"`
	TS         string          `json:"ts,omitempty"`
	ThreadTS   string          `json:"thread_ts,omitempty"`
	Edited     json.RawMessage `json:"edited,omitempty"`
}

// slackMessagesResp is the shared response shape of conversations.replies
// and conversations.history.
type slackMessagesResp struct {
	OK               bool                 `json:"ok"`
	Error            string               `json:"error,omitempty"`
	Messages         []slackStoredMessage `json:"messages,omitempty"`
	HasMore          bool                 `json:"has_more,omitempty"`
	ResponseMetadata struct {
		NextCursor string `json:"next_cursor,omitempty"`
	} `json:"response_metadata,omitempty"`
}

type slackPermalinkResp struct {
	OK        bool   `json:"ok"`
	Error     string `json:"error,omitempty"`
	Channel   string `json:"channel,omitempty"`
	Permalink string `json:"permalink,omitempty"`
}
