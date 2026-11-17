package agent

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/state"
)

func policySnapshot(revision string, accounts ...string) *controlplane.RoutingPolicies {
	snapshot := &controlplane.RoutingPolicies{Revision: revision, Policies: []json.RawMessage{}}
	for _, account := range accounts {
		snapshot.Policies = append(snapshot.Policies, json.RawMessage(
			`{"account_id":"`+account+`","alias":"chat-default","version":1,"rules":[],`+
				`"default":{"id":"default","targets":[{"deployment_id":"d","weight":100}]}}`,
		))
	}
	return snapshot
}

func TestRoutingPoliciesAreRenderedAndFollowTheControlPlane(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{
			{
				StampID:         stampID,
				MaxGeneration:   1,
				Deployments:     []controlplane.DesiredDeployment{assignment(deployA, customerA, "alpha-model", 1, "release-a")},
				RoutingPolicies: policySnapshot("rev-1", customerA),
			},
			// A steady-state pass carries no assignments but the same snapshot: nothing to do.
			{StampID: stampID, MaxGeneration: 1, RoutingPolicies: policySnapshot("rev-1", customerA)},
			// A newer snapshot with no assignments still rewrites the file.
			{StampID: stampID, MaxGeneration: 1, RoutingPolicies: policySnapshot("rev-2")},
			// An older control plane sends none: the last snapshot is kept.
			{StampID: stampID, MaxGeneration: 1},
		},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	path := filepath.Join(dir, "deployments.json")
	read := func() *state.DeploymentsFile {
		t.Helper()
		file, err := state.ReadDeployments(path)
		if err != nil {
			t.Fatalf("read deployments: %v", err)
		}
		return file
	}

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	first := read()
	if first.RoutingPolicies == nil || first.RoutingPolicies.Revision != "rev-1" {
		t.Fatalf("routing policies not rendered: %+v", first.RoutingPolicies)
	}
	if len(first.RoutingPolicies.Policies) != 1 {
		t.Fatalf("want one policy, got %d", len(first.RoutingPolicies.Policies))
	}

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if read().RoutingPolicies.Revision != "rev-1" {
		t.Fatal("an unchanged snapshot changed the file")
	}

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("third pass: %v", err)
	}
	third := read()
	if third.RoutingPolicies.Revision != "rev-2" || len(third.RoutingPolicies.Policies) != 0 {
		t.Fatalf("a new snapshot was not rendered: %+v", third.RoutingPolicies)
	}

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("fourth pass: %v", err)
	}
	if read().RoutingPolicies.Revision != "rev-2" {
		t.Fatal("a pass without a snapshot dropped the last good one")
	}
}

func TestRestartKeepsTheRenderedSnapshot(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{{
			StampID:         stampID,
			MaxGeneration:   1,
			Deployments:     []controlplane.DesiredDeployment{assignment(deployA, customerA, "alpha-model", 1, "release-a")},
			RoutingPolicies: policySnapshot("rev-1", customerA),
		}},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("pass: %v", err)
	}

	restarted, _ := newAgentAt(t, server.URL, dir, "")
	if err := restarted.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure after restart: %v", err)
	}
	if restarted.routingPolicies == nil || restarted.routingPolicies.Revision != "rev-1" {
		t.Fatalf("restart forgot the rendered snapshot: %+v", restarted.routingPolicies)
	}
}

func TestForAccountFiltersPolicies(t *testing.T) {
	snapshot := policySnapshot("r", customerA, customerB)
	policies := &state.RoutingPolicies{Revision: snapshot.Revision, Policies: snapshot.Policies}
	if got := len(policies.ForAccount(customerA)); got != 1 {
		t.Fatalf("customer A owns %d policies, want 1", got)
	}
	var nilPolicies *state.RoutingPolicies
	if nilPolicies.ForAccount(customerA) != nil {
		t.Fatal("nil snapshot returned policies")
	}
}
