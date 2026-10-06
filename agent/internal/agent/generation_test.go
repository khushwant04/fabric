package agent

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/agentcontract"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/state"
)

func TestFailedIntentPublishIsRetriedBeforeAcknowledgingNewGeneration(t *testing.T) {
	updated := controlplane.DesiredState{
		StampID: stampID, MaxGeneration: 15,
		Deployments: []controlplane.DesiredDeployment{
			assignment(deployA, customerA, "alpha", 15, "release-b"),
		},
	}
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{{
		StampID: stampID, MaxGeneration: 14,
		Deployments: []controlplane.DesiredDeployment{
			assignment(deployA, customerA, "alpha", 14, "release-a"),
		},
	}, updated, updated}}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	sink := &observingSink{}
	instance.config.Sink = sink
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	ready, unavailable := 1, 0
	sink.observed = map[string]agentcontract.ObservedCondition{deployA: {
		Phase: "ready", Applied: true, ObservedGeneration: 1,
		ReadyReplicas: &ready, UnavailableReplicas: &unavailable,
	}}
	sink.err = errors.New("API server unavailable")
	if _, err := instance.ReconcileOnce(context.Background()); err == nil {
		t.Fatal("a failed declaration must fail the pass")
	}
	if instance.credentials.AckedGeneration != 14 || instance.credentials.DeploymentGenerations[deployA] != 14 {
		t.Fatal("a failed declaration advanced the acknowledged placement")
	}
	sink.err = nil
	// A real Publisher filters old CR status once the changed spec is written.
	sink.observed = nil
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(sink.applies) != 2 || sink.applies[1][0].UpstreamModel != "release-b" {
		t.Fatalf("the failed release was not declared again: %+v", sink.applies)
	}
	last := stub.statuses[len(stub.statuses)-1]
	if last.Phase != "pending" || last.ReadyReplicas != 0 || last.ObservedGeneration == nil ||
		*last.ObservedGeneration != 15 {
		t.Fatalf("an unobserved replacement inherited the previous ready state: %+v", last)
	}
}

func TestReadyVerdictsUsePlacementGenerationInsteadOfKubernetesOrStreamGeneration(t *testing.T) {
	// A stamp's 14th assignment can create a generation-1 Kubernetes CR. Another
	// deployment advances the stream to 21 while this deployment is starting.
	// Readiness must still acknowledge placement 14, not CR 1 or stream cursor 21.
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{{
		StampID: stampID, MaxGeneration: 21,
		Deployments: []controlplane.DesiredDeployment{
			assignment(deployA, customerA, "alpha", 14, "release-a"),
			assignment(deployB, customerB, "beta", 21, "release-b"),
		},
	}}}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	sink := &observingSink{}
	instance.config.Sink = sink
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	ready, unavailable := 1, 0
	sink.observed = map[string]agentcontract.ObservedCondition{deployA: {
		Phase: "ready", Reason: "ModelHostAndConfigurationApplied", Applied: true,
		ObservedGeneration: 1, ReadyReplicas: &ready, UnavailableReplicas: &unavailable,
	}}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	last := stub.statuses[len(stub.statuses)-1]
	if last.DeploymentID != deployA || last.Phase != "ready" || last.ReadyReplicas != 1 ||
		last.ObservedGeneration == nil || *last.ObservedGeneration != 14 {
		t.Fatalf("ready report used another generation counter: %+v", last)
	}
	saved, err := state.LoadCredentials(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatal(err)
	}
	if saved.AckedGeneration != 21 || saved.DeploymentGenerations[deployA] != 14 ||
		saved.DeploymentGenerations[deployB] != 21 {
		t.Fatalf("placement generations were not durable independently: %+v", saved.DeploymentGenerations)
	}
}

func TestRestartRestoresEachPlacementGenerationFromDurableState(t *testing.T) {
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{{
		StampID: stampID, MaxGeneration: 21,
		Deployments: []controlplane.DesiredDeployment{
			assignment(deployA, customerA, "alpha", 14, "release-a"),
			assignment(deployB, customerB, "beta", 21, "release-b"),
		},
	}}}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	delivered := len(stub.statuses)
	restarted, _ := newAgentAt(t, server.URL, dir, "")
	if err := restarted.Ensure(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := restarted.ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	wanted := map[string]int{deployA: 14, deployB: 21}
	if len(stub.statuses)-delivered != len(wanted) {
		t.Fatalf("missing status recovery: %+v", stub.statuses[delivered:])
	}
	for _, report := range stub.statuses[delivered:] {
		if report.ObservedGeneration == nil || *report.ObservedGeneration != wanted[report.DeploymentID] {
			t.Fatalf("restarted report used the stamp cursor: %+v", report)
		}
	}
	if stub.lastAfter != "21" {
		t.Fatalf("durable direct configuration should resume from its stream cursor: %q", stub.lastAfter)
	}
}

func TestLegacyCredentialsAndOperatorRestartsReplayDesiredAssignments(t *testing.T) {
	for _, operatorBacked := range []bool{false, true} {
		name := "legacy-credentials"
		if operatorBacked {
			name = "operator-routed-config"
		}
		t.Run(name, func(t *testing.T) {
			stub := &controlPlaneStub{desired: []controlplane.DesiredState{{
				StampID: stampID, MaxGeneration: 14,
				Deployments: []controlplane.DesiredDeployment{
					assignment(deployA, customerA, "alpha", 14, "release-a"),
				},
			}}}
			server := stub.server(t)
			instance, dir := newAgent(t, server.URL)
			if err := instance.Ensure(context.Background()); err != nil {
				t.Fatal(err)
			}
			if _, err := instance.ReconcileOnce(context.Background()); err != nil {
				t.Fatal(err)
			}
			if !operatorBacked {
				instance.credentials.DeploymentGenerations = nil
				if err := state.SaveCredentials(instance.config.CredentialsPath, instance.credentials); err != nil {
					t.Fatal(err)
				}
			}
			stub.desiredIndex = 0
			restarted, _ := newAgentAt(t, server.URL, dir, "")
			if operatorBacked {
				restarted.config.Sink = &observingSink{}
			}
			if err := restarted.Ensure(context.Background()); err != nil {
				t.Fatal(err)
			}
			if _, err := restarted.ReconcileOnce(context.Background()); err != nil {
				t.Fatal(err)
			}
			if stub.lastAfter != "0" || restarted.credentials.DeploymentGenerations[deployA] != 14 {
				t.Fatalf("restart did not recover placement generation from full desired state: after=%q", stub.lastAfter)
			}
			if stub.enrollments.Load() != 1 {
				t.Fatal("desired-state replay must retain the stamp identity")
			}
		})
	}
}
