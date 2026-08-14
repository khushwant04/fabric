package operator

import (
	"context"
	"fmt"
	"strings"

	"github.com/khushwant04/fabric/agent/internal/agentcontract"
	"github.com/khushwant04/fabric/agent/internal/kube"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// Publisher writes declared intent into the cluster as custom resources.
//
// The agent uses this instead of writing the data plane's file directly when an
// operator is present. The reason is not layering for its own sake: the agent holds
// central credentials and no Kubernetes permissions, the operator holds Kubernetes
// permissions and no central credentials, so neither one can both talk to Fabric and
// mutate the cluster. Declaring intent through the API server is also visible to an
// operator of the cluster, who can see and audit what Fabric asked for with kubectl.
type Publisher struct {
	client    *kube.Client
	namespace string
	// Label marking resources this agent owns, so resources it did not create are
	// never adopted or deleted.
	stampID string
}

// NewPublisher builds a publisher for one stamp's namespace.
func NewPublisher(client *kube.Client, namespace, stampID string) *Publisher {
	return &Publisher{client: client, namespace: namespace, stampID: stampID}
}

func (p *Publisher) collectionPath() string {
	return fmt.Sprintf(
		"/apis/%s/%s/namespaces/%s/%s", Group, Version, p.namespace, Plural,
	)
}

// resourceName derives a stable, valid object name from a deployment id.
//
// Deployment ids are UUIDs, which are already valid label values and DNS names once
// lowercased, so the mapping is one to one and needs no truncation or hashing.
func resourceName(deploymentID string) string {
	return "fabric-" + strings.ToLower(deploymentID)
}

// Apply makes the cluster's declared set match the assignments given.
//
// Resources for deployments no longer assigned are deleted, which is how a withdrawn
// placement stops being served: the operator drops it from configuration on its next
// pass.
func (p *Publisher) Apply(ctx context.Context, deployments []state.Deployment) error {
	existing, err := p.mine(ctx)
	if err != nil {
		return err
	}

	wanted := make(map[string]state.Deployment, len(deployments))
	for _, deployment := range deployments {
		wanted[resourceName(deployment.DeploymentID)] = deployment
	}

	for name, deployment := range wanted {
		if current, present := existing[name]; present {
			if err := p.update(ctx, current, deployment); err != nil {
				return err
			}
			continue
		}
		if err := p.create(ctx, name, deployment); err != nil {
			return err
		}
	}

	for name := range existing {
		if _, keep := wanted[name]; keep {
			continue
		}
		if err := p.client.Delete(ctx, p.collectionPath()+"/"+name); err != nil {
			if kube.IsNotFound(err) {
				continue
			}
			return fmt.Errorf("delete %s: %w", name, err)
		}
	}
	return nil
}

// mine returns the resources this stamp's agent declared.
func (p *Publisher) mine(ctx context.Context) (map[string]ModelDeployment, error) {
	var list modelDeploymentList
	if err := p.client.Get(ctx, p.collectionPath(), &list); err != nil {
		return nil, fmt.Errorf("list declared deployments: %w", err)
	}

	owned := make(map[string]ModelDeployment, len(list.Items))
	for _, item := range list.Items {
		// Only resources labelled with this stamp are managed. Anything else in the
		// namespace belongs to someone else and must not be modified or deleted.
		if item.Metadata.Labels["fabric.khushwant.dev/stamp-id"] != p.stampID {
			continue
		}
		owned[item.Metadata.Name] = item
	}
	return owned, nil
}

func (p *Publisher) resource(name string, deployment state.Deployment) ModelDeployment {
	return ModelDeployment{
		APIVersion: Group + "/" + Version,
		Kind:       Kind,
		Metadata: Metadata{
			Name:      name,
			Namespace: p.namespace,
			Labels: map[string]string{
				"app.kubernetes.io/managed-by":       "fabric-agent",
				"app.kubernetes.io/part-of":          "fabric",
				"fabric.khushwant.dev/stamp-id":      p.stampID,
				"fabric.khushwant.dev/account-id":    deployment.AccountID,
				"fabric.khushwant.dev/deployment-id": deployment.DeploymentID,
			},
		},
		Spec: Spec{
			DeploymentID:  deployment.DeploymentID,
			AccountID:     deployment.AccountID,
			ModelAlias:    deployment.ModelAlias,
			UpstreamURL:   deployment.UpstreamURL,
			UpstreamModel: deployment.UpstreamModel,
		},
	}
}

func (p *Publisher) create(ctx context.Context, name string, deployment state.Deployment) error {
	if err := p.client.Create(ctx, p.collectionPath(), p.resource(name, deployment), nil); err != nil {
		return fmt.Errorf("create %s: %w", name, err)
	}
	return nil
}

func (p *Publisher) update(
	ctx context.Context, current ModelDeployment, deployment state.Deployment,
) error {
	desired := p.resource(current.Metadata.Name, deployment)
	if current.Spec == desired.Spec {
		// Nothing changed, so no write: an update would bump the generation and make
		// the operator re-report status for an identical declaration.
		return nil
	}

	desired.Metadata.ResourceVersion = current.Metadata.ResourceVersion
	path := p.collectionPath() + "/" + current.Metadata.Name
	if err := p.client.Update(ctx, path, desired, nil); err != nil {
		return fmt.Errorf("update %s: %w", current.Metadata.Name, err)
	}
	return nil
}

// Observed returns what the operator reported for each deployment, keyed by
// deployment id, so the agent can forward the cluster's view rather than its own.
func (p *Publisher) Observed(ctx context.Context) (map[string]Status, error) {
	owned, err := p.mine(ctx)
	if err != nil {
		return nil, err
	}

	observed := make(map[string]Status, len(owned))
	for _, item := range owned {
		if item.Status == nil {
			continue
		}
		observed[item.Spec.DeploymentID] = *item.Status
	}
	return observed, nil
}

// ObservedConditions reports the operator's verdict per deployment, in the shape the
// agent forwards to the control plane.
//
// Only the Applied condition is translated: it is the operator's single statement
// about whether the cluster reflects the declaration, and inventing a richer mapping
// would imply detail the operator does not produce.
func (p *Publisher) ObservedConditions(
	ctx context.Context,
) (map[string]agentcontract.ObservedCondition, error) {
	statuses, err := p.Observed(ctx)
	if err != nil {
		return nil, err
	}

	conditions := make(map[string]agentcontract.ObservedCondition, len(statuses))
	for deploymentID, status := range statuses {
		for _, condition := range status.Conditions {
			if condition.Type != ConditionApplied {
				continue
			}
			conditions[deploymentID] = agentcontract.ObservedCondition{
				Reason:             condition.Reason,
				Message:            condition.Message,
				Applied:            condition.Status == "True",
				ObservedGeneration: status.ObservedGeneration,
			}
		}
	}
	return conditions, nil
}
