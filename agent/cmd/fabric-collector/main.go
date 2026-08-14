// Command fabric-collector forwards a stamp's usage to the control plane.
//
// It holds only the write-only telemetry credential, which cannot read desired
// state, write deployment status, or invoke inference. It is deliberately a
// separate process from the agent so that the two credentials live in different
// Secrets, and it never reads the agent's credentials file.
//
// All communication is outbound: it drains the data plane's administrative
// listener inside the cluster and posts to the control plane.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/khushwant04/fabric/agent/internal/collector"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// version is the build identity reported by --version.
const version = "0.1.0"

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func main() {
	var (
		controlPlane = flag.String("control-plane", env("FABRIC_COLLECTOR_CONTROL_PLANE", ""),
			"Control-plane base URL")
		dataPlane = flag.String("data-plane-admin", env("FABRIC_COLLECTOR_DATA_PLANE_ADMIN", "http://127.0.0.1:8081"),
			"Data-plane administrative base URL")
		credentialFile = flag.String("credential-file", env("FABRIC_COLLECTOR_CREDENTIAL_FILE", ""),
			"File holding the write-only telemetry credential")
		interval = flag.Duration("interval", 60*time.Second, "Forwarding interval")
		capacity = flag.Int("queue-capacity", 10000,
			"Maximum records held while the control plane is unreachable")
		timeout     = flag.Duration("timeout", 30*time.Second, "Per-request timeout")
		once        = flag.Bool("once", false, "Run a single pass and exit")
		showVersion = flag.Bool("version", false, "Print the version and exit")
	)
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}

	logger := log.New(os.Stderr, "fabric-collector ", log.LstdFlags|log.LUTC)

	if *controlPlane == "" {
		logger.Fatal("--control-plane is required")
	}

	// The credential comes from a file or the environment, never a flag: a flag
	// would expose it in the process list to anything that can read /proc.
	credential := os.Getenv("FABRIC_COLLECTOR_TELEMETRY_CREDENTIAL")
	if credential == "" {
		if *credentialFile == "" {
			logger.Fatal("supply --credential-file or FABRIC_COLLECTOR_TELEMETRY_CREDENTIAL")
		}
		loaded, err := state.ReadTelemetryCredential(*credentialFile)
		if err != nil {
			logger.Fatalf("telemetry credential: %v", err)
		}
		credential = loaded
	}

	client := controlplane.New(*controlPlane, *timeout)
	client.Credential = credential

	worker := collector.New(
		client, collector.NewDataPlane(*dataPlane, *timeout), logger, *capacity,
	)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if *once {
		stats, err := worker.RunOnce(ctx)
		if err != nil {
			logger.Fatalf("collect: %v", err)
		}
		logger.Printf(
			"drained=%d accepted=%d duplicates=%d rejected=%d pending=%d dropped=%d",
			stats.Drained, stats.Accepted, stats.Duplicates,
			stats.Rejected, stats.Pending, stats.Dropped,
		)
		return
	}

	logger.Printf("forwarding usage from %s to %s every %s", *dataPlane, *controlPlane, *interval)
	if err := worker.Run(ctx, *interval); err != nil && ctx.Err() == nil {
		logger.Fatalf("collector stopped: %v", err)
	}
}
