// Command fabric-operator turns declared model deployments into cluster state.
//
// It holds Kubernetes permissions and no Fabric credentials, which is the inverse of
// the agent. Neither component can both talk to the control plane and mutate the
// cluster, so compromising either one is bounded.
//
// It renders data-plane routing, creates model-host workloads when configured, and records
// observed rollout state. Release changes use a separate candidate workload so the active
// model is never patched in place.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
	"github.com/khushwant04/fabric/agent/internal/operator"
)

const version = "0.1.0"

func envInt(name string, fallback int) int {
	if raw := os.Getenv(name); raw != "" {
		if value, err := strconv.Atoi(raw); err == nil {
			return value
		}
	}
	return fallback
}

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
			"disable torch.compile and CUDA graph capture, trading latency for the memory it holds")
		hostDType    = flag.String("model-host-dtype", "", "weight dtype, for example bfloat16")
		hostTextOnly = flag.Bool("model-host-text-only", false,
			"disable multimodal inputs and profiling; requires a compatible vLLM image")
		hostTextOnlyModels = flag.String("model-host-text-only-models", "",
			"optional comma-separated exact model ids limiting the text-only policy")
		hostToolCallingModelDefaults = flag.Bool("model-host-tool-calling-model-defaults", true,
			"enable automatic tool calling only for exact models with verified native parser profiles")
		hostCPURequest     = flag.String("model-host-cpu-request", "", "model host CPU request, empty leaves it unset")
		hostMemoryRequest  = flag.String("model-host-memory-request", "", "model host memory request, empty leaves it unset")
		hostServiceAccount = flag.String("model-host-service-account", "", "dedicated ServiceAccount for model hosts, without mounted API credentials")
		hostHFSecret       = flag.String("model-host-hf-secret", "", "existing namespace Secret providing the Hugging Face token")
		hostHFTokenKey     = flag.String("model-host-hf-token-key", "token", "key in the Hugging Face Secret")
		hostPort           = flag.Int("model-host-port", 8000, "port the server listens on")
		hostCache          = flag.String("model-host-cache-claim", envOr("FABRIC_OPERATOR_CACHE_CLAIM", ""),
			"PersistentVolumeClaim for the weight cache when the mode is pvc")
		kernelBlockV = flag.Int("model-host-kernel-block-v",
			envInt("FABRIC_OPERATOR_KERNEL_BLOCK_V", 0),
			"value-tile width for the Fabric kernel, 0 to leave it to the kernel")
		kernelNumWarps = flag.Int("model-host-kernel-num-warps",
			envInt("FABRIC_OPERATOR_KERNEL_NUM_WARPS", 0),
			"warps per program for the Fabric kernel, 0 to leave it to the kernel")
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

		routerStatusURL = flag.String("router-status-url",
			envOr("FABRIC_OPERATOR_ROUTER_STATUS_URL", ""),
			"private data-plane router status base URL used for acknowledged drain")

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
	var nodeSelectorEntries, tolerationEntries, pullSecretEntries, toolCallParserEntries multiFlag
	flag.Var(&toolCallParserEntries, "model-host-tool-call-parser",
		"exact model-id=parser tool calling profile; repeat for several models")
	flag.Var(&pullSecretEntries, "model-host-image-pull-secret", "existing image pull Secret; repeat for several")
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
		TextOnly:             *hostTextOnly,
		CPURequest:           *hostCPURequest,
		MemoryRequest:        *hostMemoryRequest,
		ServiceAccountName:   *hostServiceAccount,
		ImagePullSecrets:     pullSecretEntries,
		HuggingFaceSecret:    *hostHFSecret,
		HuggingFaceTokenKey:  *hostHFTokenKey,
		Port:                 *hostPort,
		KernelBlockV:         *kernelBlockV,
		KernelNumWarps:       *kernelNumWarps,
		CacheMode:            *hostCacheMode,
		CacheHostPath:        *hostCachePath,
		CacheClaim:           *hostCache,
		RuntimeClassName:     *hostRuntimeClass,
		SpreadAcrossNodes:    *hostSpread,
	}
	for _, model := range strings.Split(*hostTextOnlyModels, ",") {
		if model = strings.TrimSpace(model); model != "" {
			host.TextOnlyModels = append(host.TextOnlyModels, model)
		}
	}
	host.DisableToolCallingModelDefaults = !*hostToolCallingModelDefaults
	toolCallParsers, err := operator.ParseToolCallParsers(toolCallParserEntries)
	if err != nil {
		log.Error("invalid model tool calling profile", "error", err)
		os.Exit(1)
	}
	host.ToolCallParsers = toolCallParsers

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
		// An enrolled stamp can start before any model is chosen. Each placement's
		// release supplies the loaded repository and served name; optional configured
		// values retain support for an existing baked-in model path.
		if *routerStatusURL == "" {
			log.Error("--model-host-image needs --router-status-url for acknowledged drain")
			os.Exit(1)
		}
		log.Info("managing the model host",
			"image", host.Image, "served_name", host.ServedName, "gpus", host.GPUs)
	}

	reconciler := operator.New(client, operator.Options{
		Namespace:       *namespace,
		ConfigMapName:   *configMap,
		ConfigKey:       *configKey,
		Log:             log,
		ModelHost:       host,
		RouterStatusURL: *routerStatusURL,
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
