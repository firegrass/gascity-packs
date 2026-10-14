package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	beads "github.com/steveyegge/beads"
)

func TestCheckedFullPolicy(t *testing.T) {
	evaluator := os.Getenv("LISKOV_PROOF_EVALUATOR")
	if evaluator == "" {
		t.Fatal("runner must name the pinned evaluator")
	}
	source := filepath.Join(filepath.Dir(evaluator), "..", "..", "work-helper", "testdata", "fixture.json")
	raw, err := os.ReadFile(source)
	must(t, err)
	var request Request
	must(t, json.Unmarshal(raw, &request))
	s := open(t)
	_, err = run(ctx, s, evaluator, request)
	must(t, err)
	snapshot := Request{"action": "snapshot", "epoch": "test"}
	before, err := run(ctx, s, evaluator, snapshot)
	must(t, err)
	if len(before) == 0 {
		t.Fatal("empty snapshot")
	}
	// The actual canonical scope/capacity policy executes on both snapshots.
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for _, id := range []string{"BKLG-20260101-bbbb", "BKLG-20260101-cccc"} {
		other := open(t)
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			_, e := run(ctx, other, evaluator, Request{
				"action": "claim", "epoch": "test", "id": id, "operation_id": "claim-" + id, "actor": "same-seat"})
			results <- e
		}(id)
	}
	wg.Wait()
	close(results)
	wins := 0
	for e := range results {
		if e == nil {
			wins++
		} else {
			t.Log(e)
		}
	}
	if wins != 1 {
		t.Fatalf("expected one full-policy claim, got %d", wins)
	}
	after, err := run(ctx, s, evaluator, snapshot)
	must(t, err)
	var read struct {
		Records []struct {
			ID     string         `json:"id"`
			Lease  string         `json:"lease_id"`
			Fields map[string]any `json:"fields"`
		} `json:"records"`
	}
	must(t, json.Unmarshal(after, &read))
	for _, record := range read.Records {
		if record.Fields["claimed_by"] != "same-seat" {
			continue
		}
		claim := Request{"action": "claim", "epoch": "test", "id": record.ID, "operation_id": "claim-" + record.ID, "actor": "same-seat"}
		_, err = run(ctx, s, evaluator, claim)
		must(t, err) // exact operation replay
		claim["actor"] = "changed-request"
		if _, err = run(ctx, s, evaluator, claim); err == nil {
			t.Fatal("operation identity collision accepted")
		}
		_, err = run(ctx, s, evaluator, Request{"action": "release", "epoch": "test", "id": record.ID,
			"operation_id": "release", "actor": "same-seat", "lease_id": record.Lease})
		must(t, err)
		_, err = run(ctx, s, evaluator, Request{"action": "claim", "epoch": "test", "id": record.ID,
			"operation_id": "replacement", "actor": "same-seat"})
		must(t, err)
		_, err = run(ctx, s, evaluator, Request{"action": "renew", "epoch": "test", "id": record.ID,
			"operation_id": "stale-renew", "actor": "same-seat", "lease_id": record.Lease})
		if err == nil {
			t.Fatal("same-seat old lease renewed replacement")
		}
	}
}

// isolated initialises a disposable proof_test_<name> database on the
// qualification server and returns its explicit workspace directory.
func isolated(t *testing.T, name string) string {
	t.Helper()
	dir := t.TempDir()
	command := exec.Command("git", "init", "-q", dir)
	must(t, command.Run())
	command = exec.Command("bd", "init", "--server", "--external", "--server-port", os.Getenv("BEADS_DOLT_SERVER_PORT"),
		"--server-socket", os.Getenv("BEADS_DOLT_SERVER_SOCKET"), "--database", "proof_test_"+name, "--prefix", "pw",
		"--skip-agents", "--skip-hooks", "--non-interactive")
	command.Dir = dir
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "BEADS_DIR=") {
			command.Env = append(command.Env, entry)
		}
	}
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("isolated init: %v: %s", err, output)
	}
	return filepath.Join(dir, ".beads")
}

func fixtureRequest(t *testing.T, name string) Request {
	t.Helper()
	evaluator := os.Getenv("LISKOV_PROOF_EVALUATOR")
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(evaluator), "..", "..", "work-helper", "testdata", name))
	must(t, err)
	var request Request
	must(t, json.Unmarshal(raw, &request))
	return request
}

func TestCheckedCapacityRace(t *testing.T) {
	workspace := isolated(t, "capacity")
	dir := filepath.Dir(workspace)
	evaluator := os.Getenv("LISKOV_PROOF_EVALUATOR")
	request := fixtureRequest(t, "capacity.json")
	s, err := beads.OpenFromConfig(ctx, workspace)
	must(t, err)
	defer s.Close()
	_, err = run(ctx, s, evaluator, request)
	must(t, err)
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for _, id := range []string{"BKLG-20260101-b024", "BKLG-20260101-b025"} {
		other, e := beads.OpenFromConfig(ctx, filepath.Join(dir, ".beads"))
		must(t, e)
		defer other.Close()
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			_, e := run(ctx, other, evaluator, Request{"action": "claim", "epoch": "capacity",
				"id": id, "operation_id": "capacity-" + id, "actor": "new-seat"})
			results <- e
		}(id)
	}
	wg.Wait()
	close(results)
	wins := 0
	for e := range results {
		if e == nil {
			wins++
		} else {
			t.Log(e)
		}
	}
	if wins != 1 {
		t.Fatalf("disjoint scopes with one epic slot: expected one winner, got %d", wins)
	}
}

type checkedLedger struct {
	t         *testing.T
	s         beads.Storage
	evaluator string
	n         int
}

func (l *checkedLedger) do(request Request) (map[string]any, error) {
	l.n++
	if _, ok := request["operation_id"]; !ok && !readOnly(request["action"].(string)) {
		request["operation_id"] = fmt.Sprintf("op-%03d-%s", l.n, request["action"])
	}
	request["epoch"] = "test"
	raw, err := run(ctx, l.s, l.evaluator, request)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	must(l.t, json.Unmarshal(raw, &result))
	return result, nil
}

func (l *checkedLedger) ok(request Request) map[string]any {
	l.t.Helper()
	result, err := l.do(request)
	if err != nil {
		l.t.Fatalf("%v refused: %v", request["action"], err)
	}
	return result
}

func (l *checkedLedger) refused(request Request, reason string) {
	l.t.Helper()
	if _, err := l.do(request); err == nil || !strings.Contains(err.Error(), reason) {
		l.t.Fatalf("%v: expected refusal %q, got %v", request["action"], reason, err)
	}
}

func (l *checkedLedger) record(id string) map[string]any {
	l.t.Helper()
	snapshot := l.ok(Request{"action": "snapshot"})
	for _, raw := range snapshot["records"].([]any) {
		row := raw.(map[string]any)
		if row["id"] == id {
			return row
		}
	}
	l.t.Fatalf("record %s missing from snapshot", id)
	return nil
}

func fields(row map[string]any) map[string]any { return row["fields"].(map[string]any) }

func kinds(history map[string]any) []string {
	out := []string{}
	for _, raw := range history["events"].([]any) {
		out = append(out, raw.(map[string]any)["kind"].(string))
	}
	return out
}

func TestCheckedActions(t *testing.T) {
	workspace := isolated(t, "actions")
	evaluator := os.Getenv("LISKOV_PROOF_EVALUATOR")
	s, err := beads.OpenFromConfig(ctx, workspace)
	must(t, err)
	defer s.Close()
	l := &checkedLedger{t: t, s: s, evaluator: evaluator}
	imported := fixtureRequest(t, "fixture.json")
	imported["reservations_reconciled"] = true
	l.ok(imported)
	const pr = "https://github.com/proof-computer/liskov-rs/pull/1"
	mainSHA := strings.Repeat("b", 40)

	// revision is answered by the helper alone: a missing evaluator still answers.
	raw, err := run(ctx, s, "/nonexistent/evaluator.py", Request{"action": "revision", "epoch": "test"})
	must(t, err)
	var rev map[string]any
	must(t, json.Unmarshal(raw, &rev))
	if rev["mode"] != "shadow" || rev["revision"] != float64(0) || rev["epoch"] != "test" || rev["generated_at"] == nil {
		t.Fatalf("revision answered wrongly: %s", raw)
	}
	if _, err = run(ctx, s, "/nonexistent/evaluator.py", Request{"action": "revision", "epoch": "other"}); err == nil {
		t.Fatal("revision accepted a wrong epoch")
	}

	// activate: shadow -> live, bound to the rehearsed source revision.
	l.refused(Request{"action": "activate", "actor": "patrick", "expected_source_sha": mainSHA}, "source revision changed")
	activated := l.ok(Request{"action": "activate", "actor": "patrick", "expected_source_sha": strings.Repeat("a", 40)})
	if activated["mode"] != "live" {
		t.Fatalf("activate: %v", activated)
	}
	raw, err = run(ctx, s, "/nonexistent/evaluator.py", Request{"action": "revision", "epoch": "test"})
	must(t, err)
	must(t, json.Unmarshal(raw, &rev))
	if rev["mode"] != "live" || rev["revision"] != float64(1) {
		t.Fatalf("revision after activate: %s", raw)
	}

	// Factory completion of the first item through the real transaction.
	claimed := l.ok(Request{"action": "claim", "id": "BKLG-20260101-bbbb", "actor": "gascity/lrs"})
	lease, semanticDigest := claimed["lease_id"].(string), claimed["semantic_digest"].(string)
	complete := func(id, actor, lease, semanticDigest string, extra Request) Request {
		request := Request{"action": "complete", "id": id, "actor": actor, "lease_id": lease,
			"expected_semantic_digest": semanticDigest, "resolution": "done",
			"evidence": map[string]any{"pull_request": pr, "main_sha": mainSHA,
				"attempts": []any{map[string]any{"bead_id": "lrs-1", "rig": "lrs"}}}}
		for k, v := range extra {
			request[k] = v
		}
		return request
	}
	// A replaced lease generation is stale: release, re-claim, then present the old lease.
	l.ok(Request{"action": "release", "id": "BKLG-20260101-bbbb", "actor": "gascity/lrs", "lease_id": lease})
	reclaimed := l.ok(Request{"action": "claim", "id": "BKLG-20260101-bbbb", "actor": "gascity/lrs"})
	l.refused(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, semanticDigest, nil), "lease generation changed")
	lease = reclaimed["lease_id"].(string)
	l.refused(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, strings.Repeat("0", 64), nil), "semantic contract changed")
	l.refused(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, semanticDigest,
		Request{"evidence": map[string]any{"acceptance": map[string]any{"ok": true}}}), "merged pull request")
	if fields(l.record("BKLG-20260101-bbbb"))["status"] != "working" {
		t.Fatal("a refused completion changed the row")
	}
	done := l.ok(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, semanticDigest, nil))
	if done["status"] != "done" || done["lease_id"] != nil {
		t.Fatalf("completion result: %v", done)
	}
	row := l.record("BKLG-20260101-bbbb")
	if fields(row)["status"] != "done" || fields(row)["done_at"] == nil || row["lease_id"] != nil {
		t.Fatalf("completed row: %v", row)
	}
	if evidence := fields(row)["evidence"].([]any); len(evidence) != 1 || evidence[0] != pr {
		t.Fatalf("evidence: %v", evidence)
	}
	// Completion is idempotent by operation identity and refuses a changed request.
	replay := done["operation_id"].(string)
	l.ok(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, semanticDigest, Request{"operation_id": replay}))
	l.refused(complete("BKLG-20260101-bbbb", "gascity/lrs", lease, semanticDigest,
		Request{"operation_id": replay, "notes": "changed"}), "operation identity reused")

	// The epic's last active child: a factory seat can never close it.
	claimed = l.ok(Request{"action": "claim", "id": "BKLG-20260101-cccc", "actor": "gascity/lrs"})
	lease, semanticDigest = claimed["lease_id"].(string), claimed["semantic_digest"].(string)
	l.refused(complete("BKLG-20260101-cccc", "gascity/lrs", lease, semanticDigest, nil), "last active child")
	l.refused(complete("BKLG-20260101-cccc", "gascity/lrs", lease, semanticDigest, Request{"close_epic": true}),
		"factory seat can never close an epic")
	l.ok(Request{"action": "release", "id": "BKLG-20260101-cccc", "actor": "gascity/lrs", "lease_id": lease})
	claimed = l.ok(Request{"action": "claim", "id": "BKLG-20260101-cccc", "actor": "patrick"})
	lease, semanticDigest = claimed["lease_id"].(string), claimed["semantic_digest"].(string)
	l.ok(complete("BKLG-20260101-cccc", "patrick", lease, semanticDigest, Request{"close_epic": true,
		"evidence": map[string]any{"acceptance": map[string]any{"verdict": "accepted"}}, "notes": "owner accepted"}))
	if fields(l.record("EPIC-20260101-aaaa"))["status"] != "done" {
		t.Fatal("person with close_epic did not close the epic")
	}

	// history returns ordered events with the full completed row.
	history := l.ok(Request{"action": "history", "id": "BKLG-20260101-bbbb"})
	want := []string{"claim", "release", "claim", "completion"}
	if got := kinds(history); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("history kinds %v, want %v", got, want)
	}
	events := history["events"].([]any)
	last := events[len(events)-1].(map[string]any)
	if last["id"] != "BKLG-20260101-bbbb" || last["resolution"] != "done" || fields(last["after"].(map[string]any))["status"] != "done" {
		t.Fatalf("completion event: %v", last)
	}
	if receipts := history["receipts"].([]any); len(receipts) != 4 {
		t.Fatalf("expected 4 receipts, got %d", len(receipts))
	}
	epicHistory := kinds(l.ok(Request{"action": "history", "id": "EPIC-20260101-aaaa"}))
	if strings.Join(epicHistory, ",") != "revision,completion" {
		t.Fatalf("epic history %v", epicHistory)
	}

	// author a new epic with its first item atomically, then claim the item.
	template := fields(l.record("EPIC-20260101-aaaa"))
	epicFields := map[string]any{}
	for k, v := range template {
		epicFields[k] = v
	}
	delete(epicFields, "done_at")
	epicFields["status"], epicFields["epic_id"], epicFields["rank"], epicFields["owner"] = "open", "EPIC-20260101-ffff", "1", "unassigned"
	itemFields := map[string]any{}
	for k, v := range fields(l.record("BKLG-20260101-bbbb")) {
		itemFields[k] = v
	}
	delete(itemFields, "done_at")
	itemFields["status"], itemFields["parent_epic"], itemFields["evidence"] = "open", "EPIC-20260101-ffff", []any{}
	l.refused(Request{"action": "author", "actor": "patrick", "record": map[string]any{"id": "EPIC-20260101-ffff",
		"path": "docs/backlog/epics/EPIC-20260101-ffff-new.md", "fields": epicFields, "body": "x"}}, "active backlog item")
	authored := l.ok(Request{"action": "author", "actor": "patrick", "records": []any{
		map[string]any{"id": "EPIC-20260101-ffff", "path": "docs/backlog/epics/EPIC-20260101-ffff-new.md", "fields": epicFields, "body": "x"},
		map[string]any{"id": "BKLG-20260101-gggg", "path": "docs/backlog/items/BKLG-20260101-gggg-new.md", "fields": itemFields, "body": "y"},
	}})
	if authored["authored_revision"] != float64(1) || authored["operational_revision"] != float64(0) {
		t.Fatalf("author result: %v", authored)
	}
	claimed = l.ok(Request{"action": "claim", "id": "BKLG-20260101-gggg", "actor": "gascity/lrs"})
	if claimed["status"] != "working" || claimed["lease_id"] == nil {
		t.Fatalf("claim of authored item: %v", claimed)
	}
	if got := kinds(l.ok(Request{"action": "history", "id": "BKLG-20260101-gggg"})); strings.Join(got, ",") != "authored,claim" {
		t.Fatalf("authored history %v", got)
	}

	// freeze: live -> frozen, after which every mutation is refused and reads still answer.
	l.ok(Request{"action": "freeze", "actor": "patrick", "reason": "rollback drill"})
	l.refused(Request{"action": "claim", "id": "BKLG-20260101-gggg", "actor": "gascity/orc"}, "frozen")
	l.refused(Request{"action": "release", "id": "BKLG-20260101-gggg", "actor": "gascity/lrs", "lease_id": claimed["lease_id"]}, "frozen")
	l.refused(Request{"action": "activate", "actor": "patrick", "expected_source_sha": strings.Repeat("a", 40)}, "frozen")
	snapshot := l.ok(Request{"action": "snapshot"})
	if snapshot["mode"] != "frozen" || snapshot["freeze_reason"] != "rollback drill" {
		t.Fatalf("frozen snapshot: %v %v", snapshot["mode"], snapshot["freeze_reason"])
	}
	raw, err = run(ctx, s, "/nonexistent/evaluator.py", Request{"action": "revision", "epoch": "test"})
	must(t, err)
	must(t, json.Unmarshal(raw, &rev))
	if rev["mode"] != "frozen" {
		t.Fatalf("revision after freeze: %s", raw)
	}
}
