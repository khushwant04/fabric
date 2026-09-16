package operator

import (
	"context"
	"encoding/json"
	"strings"
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

func TestObservedStatusIgnoresThePreviousGeneration(t *testing.T) {
	declared := resource("fabric-dep-a", "dep-a", "acct-a", "alpha-model", 3)
	declared.Metadata.Labels = map[string]string{"fabric.khushwant.dev/stamp-id": stampID}
	declared.Status = &Status{
		Phase:              "ready",
		ObservedGeneration: 2,
		Conditions: []Condition{{
			Type: ConditionApplied, Status: "True", Reason: "ModelHostAndConfigurationApplied",
		}},
	}
	_, client := newAPIServer(t, declared)

	observed, err := NewPublisher(client, namespace, stampID).Observed(context.Background())
	if err != nil {
		t.Fatalf("observed: %v", err)
	}
	if _, present := observed["dep-a"]; present {
		t.Fatalf("stale generation-2 status was forwarded for generation 3: %v", observed)
	}
}

func TestStrategyContractFlowsFromAgentStateThroughCRToDataPlaneDocument(t *testing.T) {
	publisher := NewPublisher(nil, namespace, stampID)
	declared := publisher.resource("fabric-dep-a", state.Deployment{
		DeploymentID: "dep-a",
		AccountID:    "acct-a",
		ModelAlias:   "alpha-model",
		UpstreamURL:  "http://legacy.test",
		Strategy:     "session_affinity",
	})
	if declared.Spec.Strategy != "session_affinity" {
		t.Fatalf("publisher dropped strategy: %+v", declared.Spec)
	}

	document, err := renderConfig(
		[]ModelDeployment{declared},
		map[string][]dataPlaneBackend{"dep-a": {{URL: "http://10.0.0.1:8000", ID: "pod-a"}}},
	)
	if err != nil {
		t.Fatalf("render config: %v", err)
	}
	var payload struct {
		Deployments []dataPlaneEntry `json:"deployments"`
	}
	if err := json.Unmarshal([]byte(document), &payload); err != nil {
		t.Fatalf("decode rendered document: %v", err)
	}
	if len(payload.Deployments) != 1 || payload.Deployments[0].Strategy != "session_affinity" {
		t.Fatalf("rendered strategy = %+v", payload.Deployments)
	}
}

func TestGPUCountContractFlowsFromAgentStateIntoTheCustomResource(t *testing.T) {
	// The control plane admits a placement against replicas x gpu_count (ADR 0013). The
	// number has to survive the trip to the cluster or the check is measuring a value that
	// governs nothing.
	publisher := NewPublisher(nil, namespace, stampID)

	declared := publisher.resource("fabric-dep-a", state.Deployment{
		DeploymentID: "dep-a",
		AccountID:    "acct-a",
		ModelAlias:   "alpha-model",
		UpstreamURL:  "http://legacy.test",
		Replicas:     2,
		GPUCount:     4,
	})

	if declared.Spec.GPUCount != 4 {
		t.Fatalf("publisher dropped the gpu count: %+v", declared.Spec)
	}
	// The CRD spells it gpuCount; a drifted name would be pruned by the API server and the
	// pod would silently fall back to the stamp's own setting.
	encoded, err := json.Marshal(declared.Spec)
	if err != nil {
		t.Fatalf("encode spec: %v", err)
	}
	if !strings.Contains(string(encoded), `"gpuCount":4`) {
		t.Fatalf("spec does not carry gpuCount: %s", encoded)
	}
	if got := declared.Spec.DesiredGPUs(1); got != 4 {
		t.Fatalf("DesiredGPUs = %d, want the declared 4", got)
	}
}

func TestAnUndeclaredGPUCountFallsBackToTheStampsSetting(t *testing.T) {
	publisher := NewPublisher(nil, namespace, stampID)
	declared := publisher.resource("fabric-dep-a", state.Deployment{
		DeploymentID: "dep-a",
		AccountID:    "acct-a",
		ModelAlias:   "alpha-model",
		UpstreamURL:  "http://legacy.test",
	})

	// Omitted from the wire form, so the API server stores nothing and an older control
	// plane's declaration is unchanged.
	encoded, _ := json.Marshal(declared.Spec)
	if strings.Contains(string(encoded), "gpuCount") {
		t.Fatalf("an absent gpu count was serialised: %s", encoded)
	}
	if got := declared.Spec.DesiredGPUs(2); got != 2 {
		t.Fatalf("DesiredGPUs = %d, want the configured 2", got)
	}
}
