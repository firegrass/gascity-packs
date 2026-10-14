package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	beads "github.com/steveyegge/beads"
)

var ctx = context.Background()

func open(t *testing.T) beads.Storage {
	t.Helper()
	dir := os.Getenv("LISKOV_BEADS_PROBE_DIR")
	if dir == "" {
		t.Fatal("run with run.py: an isolated server is required")
	}
	s, err := beads.OpenFromConfig(ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := s.Close(); err != nil {
			t.Error(err)
		}
	})
	return s
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func create(t *testing.T, s beads.Storage, suffix string) string {
	t.Helper()
	id := "pq-" + strings.ToLower(strings.ReplaceAll(t.Name(), "/", "-")) + "-" + suffix
	i := &beads.Issue{ID: id, Title: suffix, Status: beads.StatusOpen, IssueType: beads.TypeTask, Priority: 2}
	must(t, s.CreateIssue(ctx, i, "qualification"))
	return id
}

func get(t *testing.T, s beads.Storage, id string) *beads.Issue {
	t.Helper()
	i, err := s.GetIssue(ctx, id)
	must(t, err)
	return i
}

func readMetadata(s beads.Storage, key string) (string, error) {
	var value string
	err := s.RunInTransaction(ctx, "qualification receipt read", func(tx beads.Transaction) error {
		var err error
		value, err = tx.GetMetadata(ctx, key)
		return err
	})
	return value, err
}

func TestRollbackAndReadYourWrites(t *testing.T) {
	s := open(t)
	a, b := create(t, s, "a"), create(t, s, "b")
	stop := errors.New("injected failure after second write and receipt")
	err := s.RunInTransaction(ctx, "qualification rollback", func(tx beads.Transaction) error {
		if err := tx.UpdateIssue(ctx, a, map[string]interface{}{"status": "in_progress"}, "probe"); err != nil {
			return err
		}
		if err := tx.UpdateIssue(ctx, b, map[string]interface{}{"status": "in_progress"}, "probe"); err != nil {
			return err
		}
		if err := tx.SetMetadata(ctx, "proof.probe.rollback", "receipt"); err != nil {
			return err
		}
		i, err := tx.GetIssue(ctx, a)
		if err != nil {
			return err
		}
		if i.Status != beads.StatusInProgress {
			return errors.New("no read-your-writes")
		}
		return stop
	})
	if !errors.Is(err, stop) {
		t.Fatalf("wrong failure: %v", err)
	}
	for _, id := range []string{a, b} {
		if get(t, s, id).Status != beads.StatusOpen {
			t.Fatal("partial claim persisted")
		}
	}
	r, err := readMetadata(s, "proof.probe.rollback")
	must(t, err)
	if r != "" {
		t.Fatal("receipt survived rollback")
	}
}

func TestAtomicGroupVisibility(t *testing.T) {
	s, r := open(t), open(t)
	a, b := create(t, s, "a"), create(t, s, "b")
	err := s.RunInTransaction(ctx, "qualification group", func(tx beads.Transaction) error {
		for _, id := range []string{a, b} {
			if err := tx.UpdateIssue(ctx, id, map[string]interface{}{"status": "in_progress", "assignee": "group-owner"}, "probe"); err != nil {
				return err
			}
		}
		for _, id := range []string{a, b} {
			if get(t, r, id).Status != beads.StatusOpen {
				return errors.New("uncommitted group visible")
			}
		}
		return tx.SetMetadata(ctx, "proof.probe.group", "committed")
	})
	must(t, err)
	for _, id := range []string{a, b} {
		if get(t, r, id).Status != beads.StatusInProgress {
			t.Fatal("group incomplete")
		}
	}
	v, err := readMetadata(r, "proof.probe.group")
	must(t, err)
	if v != "committed" {
		t.Fatal("missing receipt")
	}
}

// Both transactions read the same prior policy state. A common durable bead is
// deliberately written by every admission, forcing conflicting transactions to
// arbitrate rather than allowing write skew across separate work items. A loser
// must retry the ENTIRE policy evaluation, not just its writes.
func TestCompetingAdmissions(t *testing.T) { admissionRace(t, true) }

func TestWithoutFenceAllowsWriteSkew(t *testing.T) { admissionRace(t, false) }

func admissionRace(t *testing.T, coordinated bool) {
	s := open(t)
	fence := create(t, s, "admission-fence")
	a, b := create(t, s, "a"), create(t, s, "b")
	ready, release := make(chan struct{}, 2), make(chan struct{})
	results := make(chan error, 2)
	var wg sync.WaitGroup
	for n, id := range []string{a, b} {
		other := open(t)
		wg.Add(1)
		go func(n int, id string) {
			defer wg.Done()
			results <- other.RunInTransaction(ctx, "qualification competing claim", func(tx beads.Transaction) error {
				for _, candidate := range []string{a, b} {
					i, e := tx.GetIssue(ctx, candidate)
					if e != nil {
						return e
					}
					if i.Status == beads.StatusInProgress {
						return errors.New("scope/capacity unavailable")
					}
				}
				ready <- struct{}{}
				<-release
				if coordinated {
					if err := tx.UpdateIssue(ctx, fence, map[string]interface{}{"notes": fmt.Sprintf("attempt-%d", n)}, "probe"); err != nil {
						return err
					}
				}
				return tx.UpdateIssue(ctx, id, map[string]interface{}{"status": "in_progress", "assignee": fmt.Sprintf("worker-%d", n)}, "probe")
			})
		}(n, id)
	}
	for range 2 {
		select {
		case <-ready:
		case <-time.After(20 * time.Second):
			t.Fatal("transactions did not reach concurrent policy reads")
		}
	}
	close(release)
	wg.Wait()
	close(results)
	wins := 0
	for err := range results {
		if err == nil {
			wins++
		} else {
			t.Logf("loser refused: %v", err)
		}
	}
	expected := 2
	if coordinated {
		expected = 1
	}
	if wins != expected {
		t.Fatalf("coordinated=%v: expected %d commits, got %d", coordinated, expected, wins)
	}
	active := 0
	for _, id := range []string{a, b} {
		if get(t, s, id).Status == beads.StatusInProgress {
			active++
		}
	}
	if active != expected {
		t.Fatalf("coordinated=%v: expected %d active, got %d", coordinated, expected, active)
	}
	if coordinated {
		err := s.RunInTransaction(ctx, "qualification retry policy", func(tx beads.Transaction) error {
			for _, id := range []string{a, b} {
				i, err := tx.GetIssue(ctx, id)
				if err != nil {
					return err
				}
				if i.Status == beads.StatusInProgress {
					return errors.New("scope/capacity unavailable")
				}
			}
			return nil
		})
		if err == nil || !strings.Contains(err.Error(), "scope/capacity unavailable") {
			t.Fatalf("retry did not re-evaluate occupied scope/capacity: %v", err)
		}
	}
}

func TestStaleEditAndLeaseGeneration(t *testing.T) {
	s := open(t)
	id := create(t, s, "item")
	before := get(t, s, id)
	must(t, s.UpdateIssue(ctx, id, map[string]interface{}{"title": "newer title"}, "probe"))
	err := s.UpdateIssueChecked(ctx, id, map[string]interface{}{"title": "stale title"}, "probe", beads.UpdateIssueOptions{ExpectedVersion: &before.RowVersion})
	if !errors.Is(err, beads.ErrVersionMismatch) {
		t.Fatalf("expected version refusal, got %v", err)
	}
	if get(t, s, id).Title != "newer title" {
		t.Fatal("stale edit overwrote title")
	}
	for _, generation := range []string{"lease-a", "lease-b"} {
		must(t, s.UpdateIssue(ctx, id, map[string]interface{}{"metadata": json.RawMessage(fmt.Sprintf(`{"proof.lease_id":%q}`, generation)), "status": "in_progress", "assignee": "same-seat"}, "probe"))
	}
	err = s.RunInTransaction(ctx, "qualification stale renewal", func(tx beads.Transaction) error {
		i, e := tx.GetIssue(ctx, id)
		if e != nil {
			return e
		}
		var m map[string]string
		if e = json.Unmarshal(i.Metadata, &m); e != nil {
			return e
		}
		if m["proof.lease_id"] != "lease-a" {
			return errors.New("lease generation changed")
		}
		return tx.UpdateIssue(ctx, id, map[string]interface{}{"status": "open"}, "probe")
	})
	if err == nil || !strings.Contains(err.Error(), "lease generation changed") {
		t.Fatalf("stale lease not refused: %v", err)
	}
	if get(t, s, id).Status != beads.StatusInProgress {
		t.Fatal("replacement lease released")
	}
}

func TestReceiptReplayAndEvidenceGate(t *testing.T) {
	s := open(t)
	id := create(t, s, "item")
	receipt := "proof.probe.complete"
	complete := func(tree string) error {
		return s.RunInTransaction(ctx, "qualification accept", func(tx beads.Transaction) error {
			r, e := tx.GetMetadata(ctx, receipt)
			if e != nil {
				return e
			}
			if r != "" {
				if r != tree {
					return errors.New("operation identity reused with changed request")
				}
				return nil
			}
			// Deliberately deterministic fixture predicate. Production must obtain
			// trusted exact-tree acceptance evidence; the worker supplies no verdict.
			if tree != "accepted-tree" {
				return errors.New("missing/stale acceptance evidence")
			}
			if e = tx.CloseIssue(ctx, id, "verified fixture evidence", "completer", ""); e != nil {
				return e
			}
			return tx.SetMetadata(ctx, receipt, tree)
		})
	}
	if complete("wrong-tree") == nil {
		t.Fatal("accepted bad evidence")
	}
	if get(t, s, id).Status != beads.StatusOpen {
		t.Fatal("failed evidence closed work")
	}
	must(t, complete("accepted-tree"))
	before := get(t, s, id).RowVersion
	// Simulates a committed result whose response was lost: recover/retry by
	// operation identity. A second open handle sees the durable receipt.
	r := open(t)
	v, e := readMetadata(r, receipt)
	must(t, e)
	if v != "accepted-tree" {
		t.Fatal("receipt not durable")
	}
	must(t, complete("accepted-tree"))
	if get(t, s, id).RowVersion != before {
		t.Fatal("replayed completion mutated work")
	}
	if complete("changed-request") == nil {
		t.Fatal("operation identity collision accepted")
	}
}

func TestProcessDeathRollsBack(t *testing.T) {
	if os.Getenv("LISKOV_BEADS_PROBE_CHILD") == "1" {
		s := open(t)
		_ = s.RunInTransaction(ctx, "qualification killed transaction", func(tx beads.Transaction) error {
			must(t, tx.UpdateIssue(ctx, os.Getenv("LISKOV_BEADS_PROBE_ID"), map[string]interface{}{"status": "closed"}, "probe"))
			must(t, tx.SetMetadata(ctx, "proof.probe.crash", "should rollback"))
			must(t, os.WriteFile(os.Getenv("LISKOV_BEADS_PROBE_SIGNAL"), []byte("ready"), 0600))
			select {}
		})
		return
	}
	s := open(t)
	id := create(t, s, "item")
	signal := filepath.Join(t.TempDir(), "ready")
	cmd := exec.Command(os.Args[0], "-test.run=^TestProcessDeathRollsBack$")
	cmd.Env = append(os.Environ(), "LISKOV_BEADS_PROBE_CHILD=1", "LISKOV_BEADS_PROBE_ID="+id, "LISKOV_BEADS_PROBE_SIGNAL="+signal)
	must(t, cmd.Start())
	defer func() { _ = cmd.Process.Kill() }()
	deadline := time.Now().Add(20 * time.Second)
	for {
		if _, e := os.Stat(signal); e == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("child did not write transaction")
		}
		time.Sleep(20 * time.Millisecond)
	}
	must(t, cmd.Process.Kill())
	_ = cmd.Wait()
	if get(t, s, id).Status != beads.StatusOpen {
		t.Fatal("killed transaction persisted")
	}
	v, e := readMetadata(s, "proof.probe.crash")
	must(t, e)
	if v != "" {
		t.Fatal("killed receipt persisted")
	}
}
