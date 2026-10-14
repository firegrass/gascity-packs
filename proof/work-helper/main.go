// proof-work is the sole transaction adapter. Policy remains in the pinned pack.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	beads "github.com/steveyegge/beads"
	"gopkg.in/yaml.v3"
)

const coordination = "pw-coordination"

type Request map[string]any
type Evaluation struct {
	State  map[string]any      `json:"state"`
	Writes []*beads.Issue      `json:"writes"`
	Edges  []*beads.Dependency `json:"edges"`
	Events []json.RawMessage   `json:"events"`
	Result json.RawMessage     `json:"result"`
}
type Receipt struct {
	Digest string          `json:"digest"`
	Result json.RawMessage `json:"result"`
}

func hash(value []byte) string          { h := sha256.Sum256(value); return hex.EncodeToString(h[:]) }
func marshal(value any) ([]byte, error) { return json.Marshal(value) }

func run(ctx context.Context, s beads.Storage, evaluator string, request Request) (json.RawMessage, error) {
	action, _ := request["action"].(string)
	readonly := action == "snapshot"
	op, _ := request["operation_id"].(string)
	if !readonly && (op == "" || len(op) > 128) {
		return nil, errors.New("mutation requires operation_id of 1–128 bytes")
	}
	encoded, err := marshal(request)
	if err != nil {
		return nil, err
	}
	requestDigest := hash(encoded)
	receiptID := "pw-op-" + hash([]byte(op))[:32]
	var result json.RawMessage
	// Public API runs each callback once. On serialization conflict retry all
	// reads, epoch checks and Python policy, never just the final row update.
	for attempt := 0; attempt < 5; attempt++ {
		err = s.RunInTransaction(ctx, "PROOF work "+action, func(tx beads.Transaction) error {
			issues, e := tx.SearchIssues(ctx, "", beads.IssueFilter{})
			if e != nil {
				return e
			}
			existing := map[string]*beads.Issue{}
			state := map[string]any{}
			edges := []*beads.Dependency{}
			for _, issue := range issues {
				if issue.Ephemeral || issue.NoHistory || issue.CompactionLevel != 0 || (issue.StorageClass != "" && issue.StorageClass != "versioned") {
					return errors.New("non-durable record in project ledger")
				}
				existing[issue.ID] = issue
				if issue.ID == coordination {
					if e = json.Unmarshal(issue.Metadata, &state); e != nil {
						return e
					}
				}
				ds, e := tx.GetDependencyRecords(ctx, issue.ID)
				if e != nil {
					return e
				}
				edges = append(edges, ds...)
			}
			if !readonly {
				if prior := existing[receiptID]; prior != nil {
					var receipt Receipt
					if e = json.Unmarshal(prior.Metadata, &receipt); e != nil {
						return e
					}
					if receipt.Digest != requestDigest {
						return errors.New("operation identity reused with different request")
					}
					// Do not repair an indeterminate commit by replaying domain
					// changes. An operator must reconcile Dolt history first.
					result = receipt.Result
					return nil
				}
			}
			payload, e := marshal(map[string]any{"request": request, "state": state, "issues": issues, "edges": edges,
				"now": time.Now().UTC().Truncate(time.Second).Format(time.RFC3339)})
			if e != nil {
				return e
			}
			cmd := exec.CommandContext(ctx, "python3", evaluator)
			cmd.Stdin = bytes.NewReader(payload)
			var out, diagnostic bytes.Buffer
			cmd.Stdout = &out
			cmd.Stderr = &diagnostic
			if e = cmd.Run(); e != nil {
				return fmt.Errorf("policy refused: %s: %w", strings.TrimSpace(diagnostic.String()), e)
			}
			var evaluated Evaluation
			if e = json.Unmarshal(out.Bytes(), &evaluated); e != nil {
				return e
			}
			result = evaluated.Result
			if readonly {
				return nil
			}
			if evaluated.State == nil {
				return errors.New("evaluator omitted coordination state")
			}
			stateJSON, e := marshal(evaluated.State)
			if e != nil {
				return e
			}
			if existing[coordination] == nil {
				if e = tx.CreateIssue(ctx, &beads.Issue{ID: coordination, Title: "PROOF work coordination", Status: beads.StatusOpen,
					IssueType: beads.TypeTask, Priority: 0, StorageClass: "versioned", Metadata: stateJSON}, "proof-work"); e != nil {
					return e
				}
			} else if e = tx.UpdateIssue(ctx, coordination, map[string]any{"metadata": json.RawMessage(stateJSON)}, "proof-work"); e != nil {
				return e
			}
			creates := []*beads.Issue{}
			for _, issue := range evaluated.Writes {
				if issue.Ephemeral || issue.NoHistory || issue.CompactionLevel != 0 || (issue.StorageClass != "" && issue.StorageClass != "versioned") {
					return errors.New("evaluator attempted ephemeral record")
				}
				if existing[issue.ID] == nil {
					creates = append(creates, issue)
				} else {
					if e = tx.UpdateIssue(ctx, issue.ID, map[string]any{"title": issue.Title, "description": issue.Description,
						"status": string(issue.Status), "priority": issue.Priority, "assignee": issue.Assignee,
						"metadata": issue.Metadata}, "proof-work"); e != nil {
						return e
					}
				}
			}
			if len(creates) > 0 {
				if e = tx.CreateIssues(ctx, creates, "proof-work"); e != nil {
					return e
				}
			}

			wanted := map[string]*beads.Dependency{}
			for _, edge := range evaluated.Edges {
				wanted[edge.IssueID+"\x00"+edge.DependsOnID] = edge
			}
			for _, edge := range edges {
				key := edge.IssueID + "\x00" + edge.DependsOnID
				target := wanted[key]
				if target != nil && target.Type == edge.Type {
					delete(wanted, key)
					continue
				}
				if e = tx.RemoveDependency(ctx, edge.IssueID, edge.DependsOnID, "proof-work"); e != nil {
					return e
				}
			}
			added := [][2]string{}
			for _, edge := range wanted {
				if e = tx.AddDependencyWithOptions(ctx, edge, "proof-work", beads.DependencyAddOptions{SkipCycleCheck: true}); e != nil {
					return e
				}
				if edge.Type == beads.DepBlocks {
					added = append(added, [2]string{edge.IssueID, edge.DependsOnID})
				}
			}
			if len(added) > 0 {
				cycle, e := tx.CycleThroughEdges(ctx, added)
				if e != nil {
					return e
				}
				if cycle != "" {
					return fmt.Errorf("native dependency cycle: %s", cycle)
				}
			}

			for i, event := range evaluated.Events {
				id := fmt.Sprintf("pw-event-%s-%d", hash([]byte(op))[:32], i)
				if e = tx.CreateIssue(ctx, &beads.Issue{ID: id, Title: "PROOF immutable work event", Status: beads.StatusOpen,
					IssueType: beads.TypeTask, Priority: 4, StorageClass: "versioned", Metadata: event}, "proof-work"); e != nil {
					return e
				}
			}
			receipt, e := marshal(Receipt{Digest: requestDigest, Result: result})
			if e != nil {
				return e
			}
			return tx.CreateIssue(ctx, &beads.Issue{ID: receiptID, Title: "PROOF immutable operation receipt", Status: beads.StatusOpen,
				IssueType: beads.TypeTask, Priority: 4, StorageClass: "versioned", Metadata: receipt}, "proof-work")
		})
		if err == nil {
			return result, nil
		}
		if errors.Is(err, beads.ErrCommitIndeterminate) {
			return nil, fmt.Errorf("indeterminate operation %s; reconcile receipt AND Dolt history before retry: %w", op, err)
		}
		message := strings.ToLower(err.Error())
		if !strings.Contains(message, "deadlock") && !strings.Contains(message, "serialization") && !strings.Contains(message, "retry transaction") {
			return nil, err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 25 * time.Millisecond):
		}
	}
	return nil, err
}

func main() {
	workspace := flag.String("workspace", "", "explicit dedicated .beads directory")
	evaluator := flag.String("evaluator", "", "pinned pack evaluator path")
	flag.Parse()
	err := mainRun(*workspace, *evaluator)
	if err != nil {
		fmt.Fprintln(os.Stderr, "proof-work:", err)
		os.Exit(1)
	}
}
func mainRun(workspace, evaluator string) error {
	if workspace == "" || evaluator == "" {
		return errors.New("explicit workspace and evaluator required")
	}
	// The dedicated workspace must not be a registered execution rig. Refuse
	// ambient database-routing overrides instead of risking another ledger.
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, "BEADS_") || strings.HasPrefix(name, "BD_") {
			return fmt.Errorf("unset ambient routing/config override %s", name)
		}
	}
	raw, err := os.ReadFile(filepath.Join(workspace, "metadata.json"))
	if err != nil {
		return err
	}
	var metadata map[string]any
	if err = json.Unmarshal(raw, &metadata); err != nil {
		return err
	}
	db, _ := metadata["dolt_database"].(string)
	if db != "proof_work" && !strings.HasPrefix(db, "proof_test_") {
		return errors.New("workspace database must be proof_work or isolated proof_test_*")
	}
	configRaw, err := os.ReadFile(filepath.Join(workspace, "config.yaml"))
	if err != nil {
		return err
	}
	var config map[string]any
	if err = yaml.Unmarshal(configRaw, &config); err != nil {
		return err
	}
	if config["events-journal-auto-prune"] != false {
		return errors.New("retention drift: events-journal-auto-prune must be explicitly false")
	}
	raw, err = io.ReadAll(io.LimitReader(os.Stdin, 128*1024*1024+1))
	if err != nil {
		return err
	}
	if len(raw) > 128*1024*1024 {
		return errors.New("request too large")
	}
	var request Request
	if err = json.Unmarshal(raw, &request); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	s, err := beads.OpenFromConfig(ctx, workspace)
	if err != nil {
		return err
	}
	vc, ok := s.(beads.VersionControlReader)
	if !ok {
		_ = s.Close()
		return errors.New("history inspection unavailable")
	}
	if request["action"] != "snapshot" {
		status, e := vc.Status(ctx)
		if e != nil {
			_ = s.Close()
			return e
		}
		if len(status.Staged)+len(status.Unstaged) != 0 {
			_ = s.Close()
			return errors.New("uncommitted Dolt history; reconcile history before mutation or receipt replay")
		}
	}
	result, operationError := run(ctx, s, evaluator, request)
	if operationError == nil && request["action"] != "snapshot" {
		status, e := vc.Status(ctx)
		if e != nil {
			operationError = e
		} else if len(status.Staged)+len(status.Unstaged) != 0 {
			operationError = errors.New("operation rows may have committed; reconcile receipt AND Dolt history")
		}
	}
	closeError := s.Close()
	if operationError != nil {
		return operationError
	}
	if closeError != nil {
		return closeError
	}
	_, err = fmt.Fprintln(os.Stdout, string(result))
	return err
}
