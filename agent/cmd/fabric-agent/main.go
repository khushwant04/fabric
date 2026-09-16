// Command fabric-agent reconciles Fabric control-plane desired state into the
// local configuration an inference stamp serves from.
//
// It is outbound-only: nothing connects to the cluster. Enrollment happens once
// with a single-use token, after which the agent holds a revocable stamp
// credential that authorizes heartbeat, capability refresh, desired-state reads,
// and status writes, and nothing else.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/khushwant04/fabric/agent/internal/agent"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/hardware"
	"github.com/khushwant04/fabric/agent/internal/kube"
	"github.com/khushwant04/fabric/agent/internal/operator"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "dev"

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func main() {
	stateDir := flag.String("state-dir", envOr("FABRIC_AGENT_STATE_DIR", "/var/lib/fabric-agent"),
		"directory holding credentials.json (0600) and deployments.json")
	credentialsPath := flag.String("credentials-file", envOr("FABRIC_AGENT_CREDENTIALS_FILE", ""),
		"path to credentials.json (default <state-dir>/credentials.json)")
	deploymentsPath := flag.String("deployments-file", envOr("FABRIC_AGENT_DEPLOYMENTS_FILE", ""),
		"path to deployments.json (default <state-dir>/deployments.json)")
	publish := flag.String("publish", envOr("FABRIC_AGENT_PUBLISH", "file"),
		"where to publish assignments: file, or kubernetes for FabricModelDeployment "+
			"resources reconciled by the operator")
	telemetryCredentialPath := flag.String("telemetry-credential-file",
		envOr("FABRIC_AGENT_TELEMETRY_CREDENTIAL_FILE", ""),
		"write the collector's telemetry credential here (0600); empty runs no hand-off")
	controlPlane := flag.String("control-plane", envOr("FABRIC_AGENT_CONTROL_PLANE", ""),
		"control-plane base URL")
	stampName := flag.String("stamp-name", envOr("FABRIC_AGENT_STAMP_NAME", ""),
		"name to register this stamp under")
	upstream := flag.String("upstream", envOr("FABRIC_AGENT_UPSTREAM", ""),
		"model host base URL the data plane should proxy to")
	orchestrator := flag.String("orchestrator", envOr("FABRIC_AGENT_ORCHESTRATOR", "k3s"),
		"Kubernetes distribution reported in capabilities")
	region := flag.String("region", envOr("FABRIC_AGENT_REGION", ""), "region reported in capabilities")
	gpus := flag.Int("gpus", 0,
		"allocatable GPUs to report when the cluster cannot be measured; a successful "+
			"measurement replaces it")
	measureCapacity := flag.Bool("measure-capacity", true,
		"read this cluster's GPU nodes and pod claims and report them as capabilities, "+
			"which is what the control plane places against")
	capacityInterval := flag.Duration("capacity-interval", time.Minute,
		"how often to re-measure the cluster's GPUs")
	poll := flag.Duration("poll", 15*time.Second, "desired-state poll interval")
	once := flag.Bool("once", false, "reconcile a single time and exit")
	showVersion := flag.Bool("version", false, "print the version and exit")

	// Repeatable, so the GPU nodes are named the way the cluster labels them. It should
	// match the operator's --model-host-node-selector: measuring nodes a host will never
	// be placed on would report capacity this stamp cannot actually serve from.
	var gpuNodeSelector multiFlag
	flag.Var(&gpuNodeSelector, "gpu-node-selector",
		"restrict capacity measurement to nodes matching key=value; repeat for several")

	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}

	log := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	if *controlPlane == "" || *upstream == "" {
		log.Error("control-plane and upstream are both required")
		os.Exit(2)
	}

	// The enrollment token arrives through the environment so it never appears in
	// a process listing, and it is used at most once.
	token := os.Getenv("FABRIC_AGENT_ENROLLMENT_TOKEN")

	config := agent.Config{
		ControlPlaneURL: *controlPlane,
		EnrollmentToken: token,
		StampName:       *stampName,
		// Kept separable because the two files have different audiences: the
		// credentials are secret and belong on the agent's own volume, while the
		// rendered configuration is published to the data plane. In Kubernetes they
		// are different mounts, which is what stops the data plane's container from
		// being able to read the agent's credential at all.
		CredentialsPath:         orDefault(*credentialsPath, filepath.Join(*stateDir, "credentials.json")),
		DeploymentsPath:         orDefault(*deploymentsPath, filepath.Join(*stateDir, "deployments.json")),
		TelemetryCredentialPath: *telemetryCredentialPath,
		UpstreamURL:             *upstream,
		PollInterval:            *poll,
		CapacityInterval:        *capacityInterval,
		Capabilities: controlplane.Capabilities{
			Orchestrator:    *orchestrator,
			Region:          *region,
			GPUs:            []controlplane.GPU{},
			AllocatableGPUs: *gpus,
			AgentVersion:    version,
		},
	}

	if *publish != "file" && *publish != "kubernetes" {
		log.Error("--publish must be file or kubernetes", "value", *publish)
		os.Exit(1)
	}

	nodeSelector, err := operator.ParseNodeSelector(gpuNodeSelector)
	if err != nil {
		log.Error("invalid --gpu-node-selector", "error", err)
		os.Exit(1)
	}

	// One client for both jobs, built only when something needs it. Publishing to the
	// cluster is opt-in because an agent without an operator has no reason to hold
	// Kubernetes permissions at all; measuring capacity needs only read access, and
	// degrades to the configured --gpus when it is absent.
	var client *kube.Client
	if *publish == "kubernetes" || *measureCapacity {
		client, err = kube.InCluster()
		if err != nil {
			if *publish == "kubernetes" {
				log.Error("--publish=kubernetes needs in-cluster credentials", "error", err)
				os.Exit(1)
			}
			// Not fatal: a stamp running outside Kubernetes, or with no service-account
			// token mounted, reports the capacity it was configured with.
			log.Info("not measuring capacity: no in-cluster credentials",
				"error", err, "reporting_allocatable_gpus", *gpus)
			client = nil
		}
	}

	if *publish == "kubernetes" {
		// The stamp id is not known until enrollment completes, so the publisher is
		// attached after Ensure below.
		config.SinkFactory = func(stampID string) agent.Sink {
			return operator.NewPublisher(client, client.Namespace, stampID)
		}
	}

	if *measureCapacity && client != nil {
		config.Capacity = agent.CapacityFunc(
			func(ctx context.Context) (hardware.Capacity, error) {
				return hardware.Measure(ctx, client, nodeSelector)
			},
		)
	}

	instance := agent.New(config, log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if *once {
		if err := instance.Ensure(ctx); err != nil {
			log.Error("enrollment failed", "error", err)
			os.Exit(1)
		}
		configured, err := instance.ReconcileOnce(ctx)
		if err != nil {
			log.Error("reconcile failed", "error", err)
			os.Exit(1)
		}
		log.Info("reconciled once", "stamp_id", instance.StampID(), "deployments", len(configured))
		return
	}

	if err := instance.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		log.Error("agent stopped", "error", err)
		os.Exit(1)
	}
	log.Info("agent stopped")
}

// orDefault returns value when set, otherwise fallback.
func orDefault(value, fallback string) string {
	if value != "" {
		return value
	}
	return fallback
}

// multiFlag collects a flag given more than once.
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }

func (m *multiFlag) Set(value string) error {
	*m = append(*m, value)
	return nil
}
