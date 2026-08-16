// Command fabric-operator turns declared model deployments into cluster state.
//
// It holds Kubernetes permissions and no Fabric credentials, which is the inverse of
// the agent. Neither component can both talk to the control plane and mutate the
// cluster, so compromising either one is bounded.
//
// It renders the data plane's configuration into a ConfigMap and records what it
// observed on each resource. It does not start a model host: none exists in this
// project yet, and shipping a workload for one would assert a component that has
// never run.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
	"github.com/khushwant04/fabric/agent/internal/operator"
)

const version = "0.1.0"

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func main() {
	var (
		namespace = flag.String("namespace", envOr("FABRIC_OPERATOR_NAMESPACE", ""),
			"namespace to reconcile (defaults to the pod's own)")
		configMap = flag.String(
			"configmap", envOr("FABRIC_OPERATOR_CONFIGMAP", "fabric-deployments"),
			"ConfigMap holding the data plane's configuration")
		configKey = flag.String(
			"config-key", envOr("FABRIC_OPERATOR_CONFIG_KEY", "deployments.json"),
			"key within the ConfigMap, which is the file name the data plane mounts")
		interval = flag.Duration("interval", 15*time.Second, "reconcile interval")
		once     = flag.Bool("once", false, "reconcile once and exit")
		show     = flag.Bool("version", false, "print the version and exit")
	)
	flag.Parse()

	if *show {
		fmt.Println(version)
		return
	}

	log := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	client, err := kube.InCluster()
	if err != nil {
		log.Error("cannot reach the Kubernetes API", "error", err)
		os.Exit(1)
	}
	if *namespace == "" {
		*namespace = client.Namespace
	}
	if *namespace == "" {
		log.Error("no namespace: pass --namespace")
		os.Exit(1)
	}

	reconciler := operator.New(client, operator.Options{
		Namespace:     *namespace,
		ConfigMapName: *configMap,
		ConfigKey:     *configKey,
		Log:           log,
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if *once {
		result, err := reconciler.ReconcileOnce(ctx)
		if err != nil {
			log.Error("reconcile failed", "error", err)
			os.Exit(1)
		}
		log.Info("reconciled",
			"declared", result.Declared, "serving", result.Serving,
			"config_changed", result.ConfigChanged, "status_writes", result.StatusWrites)
		return
	}

	log.Info("reconciling",
		"namespace", *namespace, "configmap", *configMap, "interval", interval.String())
	if err := reconciler.Run(ctx, *interval); err != nil && ctx.Err() == nil {
		log.Error("operator stopped", "error", err)
		os.Exit(1)
	}
}
