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
	"strings"
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

		// Model host. Without an image the operator only configures the data plane
		// against an upstream someone else operates, which is the existing behaviour.
		hostImage = flag.String("model-host-image", envOr("FABRIC_OPERATOR_MODEL_HOST_IMAGE", ""),
			"inference server image; empty means the operator runs no model host")
		hostModelRef = flag.String("model-host-model", envOr("FABRIC_OPERATOR_MODEL_REF", ""),
			"what the server loads: a repository id or a path inside the image")
		hostServedName = flag.String("model-host-served-name",
			envOr("FABRIC_OPERATOR_SERVED_NAME", ""),
			"name the server answers to, which is the release rather than a customer alias")
		hostGPUs   = flag.Int("model-host-gpus", 1, "GPUs per replica")
		hostMaxLen = flag.Int("model-host-max-model-len", 0,
			"context bound; required in practice because the family default will not fit")
		hostMaxSeqs   = flag.Int("model-host-max-num-seqs", 0, "concurrent sequences")
		hostGPUMemory = flag.String("model-host-gpu-memory-utilization", "",
			"fraction of total device memory the server may use")
		hostEager = flag.Bool("model-host-enforce-eager", false,
			"disable CUDA graph capture, trading latency for the memory it holds")
		hostDType = flag.String("model-host-dtype", "", "weight dtype, for example bfloat16")
		hostPort  = flag.Int("model-host-port", 8000, "port the server listens on")
		hostCache = flag.String("model-host-cache-claim", envOr("FABRIC_OPERATOR_CACHE_CLAIM", ""),
			"PersistentVolumeClaim for the weight cache when the mode is pvc")
		hostCacheMode = flag.String("model-host-cache-mode",
			envOr("FABRIC_OPERATOR_CACHE_MODE", "hostPath"),
			"where weights are cached: hostPath, pvc, or none")
		hostCachePath = flag.String("model-host-cache-host-path",
			envOr("FABRIC_OPERATOR_CACHE_HOST_PATH", operator.DefaultCacheHostPath),
			"node directory used when the cache mode is hostPath")
		hostRuntimeClass = flag.String("model-host-runtime-class",
			envOr("FABRIC_OPERATOR_RUNTIME_CLASS", ""), "RuntimeClass for GPU nodes")
		hostSpread = flag.Bool("model-host-spread-across-nodes", false,
			"prefer placing model hosts on different nodes")

		readyTimeout = flag.Duration("rollout-ready-timeout", 15*time.Minute,
			"how long a new release has to become ready before it is abandoned")
		maxParallel = flag.Int("rollout-max-parallel", 1,
			"how many hosts may change release at once on this stamp")
		autoRollback = flag.Bool("rollout-auto-rollback", true,
			"return to the last release observed ready when a new one misses its deadline")

		once = flag.Bool("once", false, "reconcile once and exit")
		show = flag.Bool("version", false, "print the version and exit")
	)

	// Repeatable, so a cluster's GPU labels and taints are expressed as they are rather
	// than squeezed into one string.
	var nodeSelectorEntries, tolerationEntries multiFlag
	flag.Var(&nodeSelectorEntries, "model-host-node-selector",
		"node selector as key=value; repeat for several")
	flag.Var(&tolerationEntries, "model-host-toleration",
		"toleration as key[=value]:effect; repeat for several")

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

	host := operator.ModelHost{
		Image:                *hostImage,
		ModelRef:             *hostModelRef,
		ServedName:           *hostServedName,
		GPUs:                 *hostGPUs,
		MaxModelLen:          *hostMaxLen,
		MaxNumSeqs:           *hostMaxSeqs,
		GPUMemoryUtilization: *hostGPUMemory,
		EnforceEager:         *hostEager,
		DType:                *hostDType,
		Port:                 *hostPort,
		CacheMode:            *hostCacheMode,
		CacheHostPath:        *hostCachePath,
		CacheClaim:           *hostCache,
		RuntimeClassName:     *hostRuntimeClass,
		SpreadAcrossNodes:    *hostSpread,
	}

	selector, err := operator.ParseNodeSelector(nodeSelectorEntries)
	if err != nil {
		log.Error("invalid node selector", "error", err)
		os.Exit(1)
	}
	host.NodeSelector = selector

	tolerations, err := operator.ParseTolerations(tolerationEntries)
	if err != nil {
		log.Error("invalid toleration", "error", err)
		os.Exit(1)
	}
	host.Tolerations = tolerations
	if host.Enabled() {
		// Checked here rather than discovered by a server that starts and then fails:
		// without these the container would either load nothing or answer to a name
		// the data plane never asks for.
		if host.ModelRef == "" || host.ServedName == "" {
			log.Error("--model-host-image needs --model-host-model and --model-host-served-name")
			os.Exit(1)
		}
		log.Info("managing the model host",
			"image", host.Image, "served_name", host.ServedName, "gpus", host.GPUs)
	}

	reconciler := operator.New(client, operator.Options{
		Namespace:     *namespace,
		ConfigMapName: *configMap,
		ConfigKey:     *configKey,
		Log:           log,
		ModelHost:     host,
		Rollout: operator.Rollout{
			ReadyTimeout: *readyTimeout,
			MaxParallel:  *maxParallel,
			AutoRollback: *autoRollback,
		},
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

// multiFlag collects a flag given more than once.
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }

func (m *multiFlag) Set(value string) error {
	*m = append(*m, value)
	return nil
}
