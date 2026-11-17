package operator

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestRoutingPoliciesAreRenderedOncePerAccount(t *testing.T) {
	policy := func(account, alias string) string {
		return `{"account_id":"` + account + `","alias":"` + alias + `"}`
	}
	a := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	b := resource("beta", "dep-b", "acct-a", "beta-model", 1)
	c := resource("gamma", "dep-c", "acct-c", "gamma-model", 1)
	a.Spec.RoutingPolicies = "[" + policy("acct-a", "chat") + "]"
	b.Spec.RoutingPolicies = a.Spec.RoutingPolicies
	// A resource cannot speak for another account.
	c.Spec.RoutingPolicies = "[" + policy("acct-c", "own") + "," + policy("acct-a", "forged") + "]"

	plain, err := renderConfig(
		[]ModelDeployment{resource("alpha", "dep-a", "acct-a", "alpha-model", 1)}, nil,
	)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if strings.Contains(plain, "routing_policies") {
		t.Fatal("a stamp without policies rendered a routing_policies section")
	}

	rendered, err := renderConfig([]ModelDeployment{a, b, c}, nil)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	var document struct {
		RoutingPolicies struct {
			Revision string            `json:"revision"`
			Policies []json.RawMessage `json:"policies"`
		} `json:"routing_policies"`
	}
	if err := json.Unmarshal([]byte(rendered), &document); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(document.RoutingPolicies.Policies) != 2 || document.RoutingPolicies.Revision == "" {
		t.Fatalf("want two policies with a revision, got %s", rendered)
	}
	if strings.Contains(rendered, "forged") {
		t.Fatal("a resource injected another account's policy")
	}

	a.Spec.RoutingPolicies = "[" + policy("acct-a", "chat-v2") + "]"
	b.Spec.RoutingPolicies = a.Spec.RoutingPolicies
	changed, err := renderConfig([]ModelDeployment{a, b, c}, nil)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if configRevision(changed) == configRevision(rendered) {
		t.Fatal("a policy change kept the configuration revision")
	}
}
