package hostreport

import (
	"strings"
	"testing"
	"time"
)

func TestDiagnoseNamesTheCauseFromTheLog(t *testing.T) {
	cases := map[string]struct {
		pod    Pod
		reason string
		hint   string
	}{
		"missing GPU runtime": {
			Pod{ContainerReason: "CrashLoopBackOff", RestartCount: 3, Log: "RuntimeError: Failed to infer device type"},
			"GPURuntimeMissing", "nvidia RuntimeClass",
		},
		"GPU out of memory": {
			Pod{ContainerReason: "Error", ContainerState: "terminated", Log: "torch.OutOfMemoryError: CUDA out of memory."},
			"GPUOutOfMemory", "max_model_len",
		},
		"missing TPU runtime": {
			Pod{ContainerReason: "CrashLoopBackOff", RestartCount: 1, Log: "RuntimeError: TPU initialization failed: No ba16c7433 device found."},
			"TPURuntimeMissing", "google.com/tpu",
		},
		"TPU out of memory": {
			Pod{ContainerReason: "Error", ContainerState: "terminated", Log: "RESOURCE_EXHAUSTED: XLA:TPU compile permanent error. Ran out of memory in memory space hbm."},
			"TPUOutOfMemory", "more chips",
		},
		"image pull": {
			Pod{ContainerState: "waiting", ContainerReason: "ImagePullBackOff"},
			"ImagePullBackOff", "registry",
		},
		"host memory": {
			Pod{ContainerState: "terminated", ContainerReason: "OOMKilled", RestartCount: 1},
			"OOMKilled", "memory limit",
		},
	}
	for name, item := range cases {
		diagnosis, failing := Diagnose(item.pod)
		if !failing || diagnosis.Reason != item.reason || !strings.Contains(diagnosis.Hint, item.hint) {
			t.Errorf("%s: %+v (failing=%v)", name, diagnosis, failing)
		}
	}
	if _, failing := Diagnose(Pod{ContainerState: "running", Log: "INFO Loading weights"}); failing {
		t.Error("a starting host is not failing")
	}
}

func TestImagePullsAreReadFromEvents(t *testing.T) {
	at := time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC)
	pulls := ImagePulls(Pod{Events: []Event{
		{Reason: "Scheduled", Message: "Successfully assigned", At: at},
		{Reason: "Pulling", Message: `Pulling image "public.ecr.aws/fabric/model-host:git-abc"`, At: at},
		{Reason: "Pulled", Message: `Successfully pulled image "public.ecr.aws/fabric/model-host:git-abc" in 4m34.5s (4m40s including waiting). Image size: 8912345678 bytes.`, At: at.Add(5 * time.Minute)},
		{Reason: "Pulled", Message: `Container image "x" already present on machine`, At: at},
		{Reason: "Failed", Message: `Failed to pull image "y": not found`, At: at},
	}})
	if len(pulls) != 4 {
		t.Fatalf("pulls = %+v", pulls)
	}
	if pulls[0].Phase != "started" || pulls[0].Image != "public.ecr.aws/fabric/model-host:git-abc" {
		t.Errorf("started = %+v", pulls[0])
	}
	if pulls[1].Phase != "finished" || pulls[1].Duration != 274500*time.Millisecond || pulls[1].SizeBytes != 8912345678 {
		t.Errorf("finished = %+v", pulls[1])
	}
	if pulls[2].Phase != "present" || pulls[3].Phase != "failed" {
		t.Errorf("present/failed = %+v %+v", pulls[2], pulls[3])
	}
}

func TestFabricCredentialsAreRedactedFromLogs(t *testing.T) {
	cleaned, _ := Sanitize("ok line\nusing fab_agent_0123456789abcdef_secretpart\n", MaxLogBytes)
	if strings.Contains(cleaned, "fab_agent_") || !strings.Contains(cleaned, RedactedLine) {
		t.Fatalf("cleaned = %q", cleaned)
	}
}
