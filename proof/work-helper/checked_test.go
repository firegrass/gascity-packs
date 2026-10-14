package main

import (
	"encoding/json"
	beads "github.com/steveyegge/beads"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
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

func TestCheckedCapacityRace(t *testing.T) {
	dir := t.TempDir()
	command := exec.Command("git", "init", "-q", dir)
	must(t, command.Run())
	command = exec.Command("bd", "init", "--server", "--external", "--server-port", os.Getenv("BEADS_DOLT_SERVER_PORT"),
		"--server-socket", os.Getenv("BEADS_DOLT_SERVER_SOCKET"), "--database", "proof_test_capacity", "--prefix", "pw",
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
	evaluator := os.Getenv("LISKOV_PROOF_EVALUATOR")
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(evaluator), "..", "..", "work-helper", "testdata", "capacity.json"))
	must(t, err)
	var request Request
	must(t, json.Unmarshal(raw, &request))
	s, err := beads.OpenFromConfig(ctx, filepath.Join(dir, ".beads"))
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
