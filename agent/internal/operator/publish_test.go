package operator

import (
	"context"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/state"
)

const stampID = "11111111-1111-4111-8111-111111111111"

func assignment(deploymentID, account, alias string) state.Deployment {
	return state.Deployment{
		DeploymentID:  deploymentID,
		AccountID:     account,
		ModelAlias:    alias,
		UpstreamURL:   "http://model-host:8000",
		UpstreamModel: "release-1",
	}
}

func TestPublishingDeclaresIntentAsCustomResources(t *testing.T) {
	state_, client := newAPIServer(t)
	publisher := NewPublisher(client, namespace, stampID)

	err := publisher.Apply(context.Background(), []state.Deployment{
		assignment("dep-a", "acct-a", "alpha-model"),
	})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}

	if len(state_.resources) != 1 {
		t.Fatalf("expected one resource, got %d", len(state_.resources))
	}
	item := state_.resources["fabric-dep-a"]
	if item == nil {
		t.Fatalf("resource was not named from the deployment id: %v", state_.resources)
	}
	if item.Spec.AccountID != "acct-a" {
		t.Fatalf("owning account did not travel: %+v", item.Spec)
	}
	if item.Metadata.Labels["fabric.khushwant.dev/stamp-id"] != stampID {
		t.Fatal("the resource is not labelled with the stamp that owns it")
	}
}

func TestWithdrawnAssignmentsAreDeleted(t *testing.T) {
	state_, client := newAPIServer(t)
	publisher := NewPublisher(client, namespace, stampID)
	ctx := context.Background()

	if err := publisher.Apply(ctx, []state.Deployment{
		assignment("dep-a", "acct-a", "alpha-model"),
		assignment("dep-b", "acct-b", "beta-model"),
	}); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	if err := publisher.Apply(ctx, []state.Deployment{
		assignment("dep-a", "acct-a", "alpha-model"),
	}); err != nil {
		t.Fatalf("second apply: %v", err)
	}

	if _, present := state_.resources["fabric-dep-b"]; present {
		t.Fatal("a withdrawn assignment was left declared, so it would keep being served")
	}
	if _, present := state_.resources["fabric-dep-a"]; !present {
		t.Fatal("the remaining assignment was removed")
	}
}

func TestAnotherStampsResourcesAreNeverTouched(t *testing.T) {
	foreign := resource("fabric-someone-else", "dep-x", "acct-x", "x-model", 1)
	foreign.Metadata.Labels = map[string]string{
		"fabric.khushwant.dev/stamp-id": "22222222-2222-4222-8222-222222222222",
	}
	state_, client := newAPIServer(t, foreign)

	err := NewPublisher(client, namespace, stampID).Apply(context.Background(), nil)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}

	if _, present := state_.resources["fabric-someone-else"]; !present {
		t.Fatal("a resource belonging to another stamp was deleted")
	}
}

func TestAnUnchangedDeclarationIsNotRewritten(t *testing.T) {
	state_, client := newAPIServer(t)
	publisher := NewPublisher(client, namespace, stampID)
	ctx := context.Background()
	assignments := []state.Deployment{assignment("dep-a", "acct-a", "alpha-model")}

	if err := publisher.Apply(ctx, assignments); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	before := state_.updates

	if err := publisher.Apply(ctx, assignments); err != nil {
		t.Fatalf("second apply: %v", err)
	}
	if state_.updates != before {
		t.Fatal("an identical declaration was rewritten, which would re-trigger the operator")
	}
}

func TestObservedStatusComesFromTheCluster(t *testing.T) {
	declared := resource("fabric-dep-a", "dep-a", "acct-a", "alpha-model", 2)
	declared.Metadata.Labels = map[string]string{"fabric.khushwant.dev/stamp-id": stampID}
	declared.Status = &Status{
		Phase:              "ready",
		ObservedGeneration: 2,
		Conditions: []Condition{{
			Type: ConditionApplied, Status: "True", Reason: "DataPlaneConfigurationRendered",
		}},
	}
	_, client := newAPIServer(t, declared)

	observed, err := NewPublisher(client, namespace, stampID).Observed(context.Background())
	if err != nil {
		t.Fatalf("observed: %v", err)
	}

	status, present := observed["dep-a"]
	if !present {
		t.Fatalf("no status for the declared deployment: %v", observed)
	}
	// The agent forwards the operator's verdict rather than asserting its own, so the
	// control plane learns what the cluster did, not what the agent asked for.
	if status.Conditions[0].Reason != "DataPlaneConfigurationRendered" {
		t.Fatalf("unexpected reason: %+v", status.Conditions[0])
	}
}
