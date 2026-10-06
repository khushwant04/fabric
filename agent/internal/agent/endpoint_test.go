package agent

import (
	"context"
	"strings"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/agentcontract"
)

func TestPublicInferenceURLRejectsPrivateOrCredentialedTargets(t *testing.T) {
	for _, raw := range []string{
		"http://gpu.example.com", "https://user:pass@gpu.example.com", "https://gpu.example.com/path",
		"https://gpu.example.com/?token=secret", "https://gpu.example.com?", "https://gpu.example.com/#token",
		"https://gpu.example.com/#", "https://localhost", "https://10.0.0.1", "https://127.0.0.1",
		"https://host.fabric.svc.cluster.local", "https://*.example.com", "https://_gpu.example.com",
		"https://-gpu.example.com", "https://gpu-.example.com", "https://gpu..example.com",
		"https://gpu.example.com.", "https://gpu.example.com:", "https://gpu.example.com:0",
		"https://gpu.example.com:65536", "https://gpu.example.com:invalid", "https://256.256.256.256",
		"https://[::1]", "https://[fc00::1]", "https://[ff02::1]",
		"https://" + strings.Repeat("g", 64) + ".example.com",
	} {
		if _, err := ValidateInferenceURL(raw); err == nil {
			t.Errorf("accepted invalid public endpoint %q", raw)
		}
	}
	for _, raw := range []string{
		"https://gpu.example.com/", "https://GPU-WEST.example.com", "https://gpu.example.com:443",
		"https://gpu.example.com:8443", "https://203.0.113.1", "https://[2001:db8::1]",
	} {
		if got, err := ValidateInferenceURL(raw); err != nil || got != strings.TrimSuffix(raw, "/") {
			t.Fatalf("valid public base URL %s rejected: %s %v", raw, got, err)
		}
	}
}

func TestPublicEndpointRequiresObservedAvailability(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.InferenceURL = "https://stamp.example.com"
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatal(err)
	}
	item := assignment(deployA, customerA, "chat", 1, "model")
	ready, unavailable := 1, 0
	available := false
	instance.observed = map[string]agentcontract.ObservedCondition{deployA: {
		Phase: "ready", Applied: true, Available: &available, ReadyReplicas: &ready, UnavailableReplicas: &unavailable,
	}}
	if err := instance.reportStatus(context.Background(), item); err != nil {
		t.Fatal(err)
	}
	if stub.statuses[0].Endpoint != "" {
		t.Fatal("configured phase must not publish an unavailable endpoint")
	}
	firstFingerprint := instance.observationFingerprint(deployA)
	available = true
	if err := instance.reportStatus(context.Background(), item); err != nil {
		t.Fatal(err)
	}
	if stub.statuses[1].Endpoint != instance.config.InferenceURL || stub.statuses[1].Conditions[1]["status"] != "True" {
		t.Fatalf("available host did not publish its public endpoint: %+v", stub.statuses[1])
	}
	if firstFingerprint == instance.observationFingerprint(deployA) {
		t.Fatal("an availability change must trigger a new report")
	}
	item.Deleted = true
	if err := instance.reportStatus(context.Background(), item); err != nil {
		t.Fatal(err)
	}
	if stub.statuses[2].Endpoint != "" || stub.statuses[2].Conditions[1]["status"] != "False" {
		t.Fatal("a withdrawn model must not advertise an inference endpoint")
	}
	if instance.reportedCapabilities().InferenceURL != instance.config.InferenceURL {
		t.Fatal("enrollment and heartbeat must report the configured ingress base URL")
	}
}
