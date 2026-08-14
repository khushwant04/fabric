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
	"syscall"
	"time"

	"github.com/khushwant04/fabric/agent/internal/agent"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
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
	gpus := flag.Int("gpus", 0, "allocatable GPUs reported in capabilities")
	poll := flag.Duration("poll", 15*time.Second, "desired-state poll interval")
	once := flag.Bool("once", false, "reconcile a single time and exit")
	showVersion := flag.Bool("version", false, "print the version and exit")
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
		ControlPlaneURL:         *controlPlane,
		EnrollmentToken:         token,
		StampName:               *stampName,
		CredentialsPath:         filepath.Join(*stateDir, "credentials.json"),
		DeploymentsPath:         filepath.Join(*stateDir, "deployments.json"),
		TelemetryCredentialPath: *telemetryCredentialPath,
		UpstreamURL:             *upstream,
		PollInterval:            *poll,
		Capabilities: controlplane.Capabilities{
			Orchestrator:    *orchestrator,
			Region:          *region,
			GPUs:            []controlplane.GPU{},
			AllocatableGPUs: *gpus,
			AgentVersion:    version,
		},
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
