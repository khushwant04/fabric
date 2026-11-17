package hostreport

import (
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Diagnosis turns a failing model host into one sentence and a fix. The console derives the
// same from the snapshot for its Logs tab; the agent uses this to put the failure in the
// stamp's activity log, where an owner who is not watching a deployment still sees it.
type Diagnosis struct {
	// Reason is a short stable label, for de-duplication and the timeline.
	Reason  string
	Message string
	Hint    string
}

type logPattern struct {
	match   *regexp.Regexp
	reason  string
	message string
	hint    string
}

// Ordered: the first match wins, so the most specific causes come first.
var logPatterns = []logPattern{
	{
		regexp.MustCompile(`(?i)TPU initialization failed|No TPU devices? (were )?found|Unable to initialize backend 'tpu'|Failed to (open|get) (the )?TPU|TPU platform.*not (found|available)|The TPU is already in use`),
		"TPURuntimeMissing",
		"The model server cannot open the TPU chips inside its container.",
		"On a Cloud TPU VM the model host must run privileged with the TPU device nodes " +
			"(/dev/accel* or /dev/vfio) and request google.com/tpu: re-run the bootstrap command, " +
			"which detects the TPU and sets the chart's TPU values. Only one process can hold the " +
			"chips, so stop anything else using them on the VM.",
	},
	{
		regexp.MustCompile(`(?i)Ran out of memory in memory space hbm|RESOURCE_EXHAUSTED.*(hbm|TPU)`),
		"TPUOutOfMemory",
		"The model does not fit in TPU HBM with these settings.",
		"Lower max_model_len or max_num_seqs, use more chips (tensor parallelism spans them), or " +
			"deploy on a TPU with more HBM per chip.",
	},
	{
		regexp.MustCompile(`(?i)fp8.*not supported.*tpu|tpu.*does not support.*fp8`),
		"QuantizationUnsupported",
		"This checkpoint's FP8 quantization is not supported on TPU.",
		"Deploy the model's bf16 checkpoint on a TPU stamp; FP8 checkpoints need an NVIDIA GPU.",
	},
	{
		regexp.MustCompile(`(?i)No neuron devices? (were |was )?found|Failed to (open|find|discover) (the )?neuron device|nrt_init failed|Neuron runtime (initialization|init) failed|/dev/neuron[0-9]*: No such file|NEURON_RT_VISIBLE_CORES.*(invalid|out of range)|Logical Neuron Core.*not available`),
		"NeuronRuntimeMissing",
		"The model server cannot open the Neuron devices inside its container.",
		"The container must request aws.amazon.com/neuron so the Neuron device plugin injects " +
			"/dev/neuron*: check the neuron-device-plugin pods in kube-system and the Neuron driver " +
			"on the instance (ls /dev/neuron*, neuron-ls); on k3s, re-run the bootstrap command, " +
			"which installs the device plugin and sets the chart's Neuron values. Only one process " +
			"can hold a NeuronCore, so stop anything else using the devices.",
	},
	{
		regexp.MustCompile(`(?i)neuronx-cc.*(failed|error|returned non-zero)|NEFF.*(compil|generation).*fail|neuronx_distributed.*compil.*(fail|error)|neuron.*compil(er|ation).*(failed|crash|exited|error)`),
		"NeuronCompilationFailed",
		"The Neuron compiler could not compile this model for the instance.",
		"Neuron compiles every model ahead of time on first start. Check that the architecture " +
			"is supported by vLLM on Neuron, lower max_model_len or max_num_seqs, and make sure the " +
			"instance has enough host memory and disk for compilation. A corrupt compile cache under " +
			"the node's model cache (neuron/) can be deleted to force a clean compile.",
	},
	{
		regexp.MustCompile(`(?i)NRT_RESOURCE|nrt.*(failed to allocate|out of memory|insufficient memory)|neuron.*(out of (device )?memory|failed to allocate|insufficient (device )?memory)`),
		"NeuronOutOfMemory",
		"The model does not fit in Neuron device HBM with these settings.",
		"Lower max_model_len or max_num_seqs, use more Neuron devices (tensor parallelism spans " +
			"their NeuronCores), or deploy on an instance with more HBM per device.",
	},
	{
		regexp.MustCompile(`(?i)fp8.*not supported.*neuron|neuron.*does not support.*fp8`),
		"QuantizationUnsupported",
		"This checkpoint's FP8 quantization is not supported on AWS Neuron.",
		"Deploy the model's bf16 checkpoint on a Neuron stamp; FP8 checkpoints need an NVIDIA GPU.",
	},
	{
		regexp.MustCompile(`(?i)failed to infer device type|no CUDA GPUs are available|Found no NVIDIA driver|libcuda\.so.*(cannot open|not found)`),
		"GPURuntimeMissing",
		"The model server cannot see a GPU inside its container.",
		"The NVIDIA runtime is missing for this pod: on k3s, re-run the bootstrap command, which " +
			"installs the nvidia RuntimeClass and device plugin; elsewhere check gpu.runtimeClassName " +
			"and that the node advertises nvidia.com/gpu.",
	},
	{
		regexp.MustCompile(`(?i)CUDA out of memory|OutOfMemoryError|No available memory for the cache blocks|not enough (GPU|KV cache) memory|larger than the maximum number of tokens that can be stored in KV cache|KV cache is needed, which is larger than the available KV cache memory`),
		"GPUOutOfMemory",
		"The model does not fit in GPU memory with these settings.",
		"Lower max_model_len or max_num_seqs, raise gpu_memory_utilization slightly, or deploy on a GPU with more memory.",
	},
	{
		regexp.MustCompile(`(?i)(401|403).*(huggingface|hf\.co)|gated repo|Access to model .* is restricted|Repository Not Found`),
		"WeightsUnavailable",
		"The model's weights could not be downloaded from Hugging Face.",
		"Check the repository name; gated models need a Hugging Face token (managedModelHost.huggingFace.existingSecret).",
	},
	{
		regexp.MustCompile(`(?i)bfloat16 is only supported|Bfloat16 is only supported on GPUs with compute capability`),
		"DtypeUnsupported",
		"This GPU does not support bfloat16.",
		"Set the model dtype to float16 (operator.managedModelHost.dtype), or re-run the bootstrap command, which picks it from the GPU.",
	},
	{
		regexp.MustCompile(`(?i)No space left on device`),
		"DiskFull",
		"The node ran out of disk while downloading or caching.",
		"Free space under the model cache path, or point managedModelHost.cache at a larger disk.",
	},
}

var runFailures = map[string]bool{"CrashLoopBackOff": true, "Error": true, "OOMKilled": true}
var pullFailures = map[string]bool{
	"ErrImagePull": true, "ImagePullBackOff": true, "InvalidImageName": true,
	"ErrImageNeverPull": true, "RegistryUnavailable": true,
}

// Diagnose reports why a host is failing, if its snapshot shows a failure rather than slow
// progress. The log is consulted first because it names the cause; the container state
// only says that it crashed.
func Diagnose(pod Pod) (Diagnosis, bool) {
	reason := pod.ContainerReason
	failing := runFailures[reason] || pullFailures[reason] ||
		(pod.ContainerState == "terminated" && reason != "" && reason != "Completed") ||
		pod.RestartCount > 0
	if !failing {
		return Diagnosis{}, false
	}
	for _, pattern := range logPatterns {
		if pattern.match.MatchString(pod.Log) {
			return Diagnosis{Reason: pattern.reason, Message: pattern.message, Hint: pattern.hint}, true
		}
	}
	switch {
	case pullFailures[reason]:
		return Diagnosis{
			Reason:  reason,
			Message: "The model runtime image could not be pulled.",
			Hint:    "Check that the node can reach the registry and that the image tag exists.",
		}, true
	case reason == "OOMKilled":
		return Diagnosis{
			Reason:  reason,
			Message: "The model server ran out of host memory and was killed.",
			Hint:    "Give the model host a higher memory limit, or choose a smaller model.",
		}, true
	case runFailures[reason] || pod.ContainerState == "terminated":
		return Diagnosis{
			Reason:  orDefault(reason, "Crashed"),
			Message: "The model server exited during start-up.",
			Hint:    "Open the deployment's Logs tab for the server's traceback.",
		}, true
	}
	return Diagnosis{}, false
}

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

// ImagePull is what a pod's events say about pulling its image.
type ImagePull struct {
	// Phase is "started", "finished", "present" (already on the node) or "failed".
	Phase     string
	Image     string
	Duration  time.Duration
	SizeBytes int64
	Message   string
	At        time.Time
}

var (
	quotedImage = regexp.MustCompile(`image "([^"]+)"`)
	pullTook    = regexp.MustCompile(`\bin ([0-9hmsµn.]+)`)
	pullSize    = regexp.MustCompile(`Image size: ([0-9]+) bytes`)
)

// ImagePulls reads the kubelet's pull events for a pod: "Pulling image ...", "Successfully
// pulled image ... in 4m34s ... Image size: N bytes", "Container image ... already present
// on machine", and pull failures.
func ImagePulls(pod Pod) []ImagePull {
	pulls := []ImagePull{}
	for _, event := range pod.Events {
		pull := ImagePull{Message: event.Message, At: event.At}
		if match := quotedImage.FindStringSubmatch(event.Message); match != nil {
			pull.Image = match[1]
		}
		switch {
		case event.Reason == "Pulling":
			pull.Phase = "started"
		case event.Reason == "Pulled" && strings.Contains(event.Message, "already present"):
			pull.Phase = "present"
		case event.Reason == "Pulled":
			pull.Phase = "finished"
			if match := pullTook.FindStringSubmatch(event.Message); match != nil {
				pull.Duration, _ = time.ParseDuration(match[1])
			}
			if match := pullSize.FindStringSubmatch(event.Message); match != nil {
				pull.SizeBytes, _ = strconv.ParseInt(match[1], 10, 64)
			}
		case (event.Reason == "Failed" || event.Reason == "BackOff") &&
			strings.Contains(strings.ToLower(event.Message), "pull"):
			pull.Phase = "failed"
		default:
			continue
		}
		pulls = append(pulls, pull)
	}
	return pulls
}
