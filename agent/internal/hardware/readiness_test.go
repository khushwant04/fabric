package hardware

import (
	"context"
	"encoding/json"
	"testing"
)

func TestUnschedulableAndUnreadyNodesCannotOfferPlacementCapacity(t *testing.T) {
	cluster := &clusterStub{nodes: `{"items":[
 {"metadata":{"name":"ready","labels":{"node.kubernetes.io/instance-type":"Standard_NC8as_T4_v3"}},"status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"nvidia.com/gpu":"1"}}},
 {"metadata":{"name":"down"},"status":{"conditions":[{"type":"Ready","status":"False"}],"allocatable":{"nvidia.com/gpu":"4"}}},
 {"metadata":{"name":"cordoned"},"spec":{"unschedulable":true},"status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"nvidia.com/gpu":"8"}}},
 {"metadata":{"name":"unknown"},"status":{"allocatable":{"nvidia.com/gpu":"16"}}}
 ]}`, pods: `{"items":[]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 1 || capacity.MaxFreeGPUsPerNode != 1 || len(capacity.Profiles) != 1 || capacity.Profiles[0].Node != "ready" {
		t.Fatalf("unusable nodes inflated placement capacity: %+v", capacity)
	}
	if len(capacity.GPUs) != 1 || capacity.GPUs[0].Source != "machine type Standard_NC8as_T4_v3" {
		t.Fatalf("inferred hardware must retain its provenance: %+v", capacity.GPUs)
	}
}

func TestMixedHardwareSourcesRemainExplicit(t *testing.T) {
	profiles := []Profile{{Model: "Tesla T4", Count: 1, Source: "node label"}, {Model: "Tesla T4", Count: 1, Source: "machine type Standard_NC8as_T4_v3"}}
	groups := groupProfiles(profiles)
	if len(groups) != 1 || groups[0].Source != "mixed node labels and inferred hardware" {
		t.Fatalf("mixed provenance was silently promoted to device measurement: %+v", groups)
	}
}

func TestDeviceDiscoveryComputeLabelsDescribeUnknownHardware(t *testing.T) {
	cases := []struct {
		name       string
		labels     map[string]string
		capability ComputeCapability
	}{
		{
			name: "gpu feature discovery",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "12", "nvidia.com/gpu.compute.minor": "0",
			},
			capability: ComputeCapability{Major: 12, Minor: 0},
		},
		{
			name: "device labels take precedence over legacy labels",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "7", "nvidia.com/gpu.compute.minor": "5",
				"nvidia.com/cuda.compute-capability.major": "9", "nvidia.com/cuda.compute-capability.minor": "0",
			},
			capability: ComputeCapability{Major: 7, Minor: 5},
		},
		{
			name: "legacy installation",
			labels: map[string]string{
				"nvidia.com/cuda.compute-capability.major": "8", "nvidia.com/cuda.compute-capability.minor": "6",
			},
			capability: ComputeCapability{Major: 8, Minor: 6},
		},
		{
			name: "malformed device label with complete legacy fallback",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "broken", "nvidia.com/gpu.compute.minor": "0",
				"nvidia.com/cuda.compute-capability.major": "7", "nvidia.com/cuda.compute-capability.minor": "5",
			},
			capability: ComputeCapability{Major: 7, Minor: 5},
		},
		{
			name: "incomplete label pair remains unknown",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "9",
			},
		},
		{
			name: "negative minor remains unknown",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "9", "nvidia.com/gpu.compute.minor": "-1",
			},
		},
		{
			name: "zero major remains unknown",
			labels: map[string]string{
				"nvidia.com/gpu.compute.major": "0", "nvidia.com/gpu.compute.minor": "0",
			},
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			test.labels["nvidia.com/gpu.product"] = "NVIDIA-New-Device"
			test.labels["nvidia.com/gpu.memory"] = "8192"
			nodes, err := json.Marshal(map[string]any{"items": []map[string]any{{
				"metadata": map[string]any{"name": "gpu", "labels": test.labels},
				"status": map[string]any{
					"conditions":  []map[string]string{{"type": "Ready", "status": "True"}},
					"allocatable": map[string]string{GPUResource: "1"},
				},
			}}})
			if err != nil {
				t.Fatal(err)
			}
			capacity, err := Measure(context.Background(), &clusterStub{nodes: string(nodes), pods: `{"items":[]}`}, nil)
			if err != nil {
				t.Fatal(err)
			}
			if len(capacity.Profiles) != 1 || capacity.Profiles[0].Capability != test.capability || capacity.Profiles[0].MemoryMiB != 8192 {
				t.Fatalf("device labels produced incorrect hardware: %+v", capacity)
			}
			if test.capability.Known() {
				if capacity.GPUs[0].ComputeCapability != test.capability.String() || capacity.GPUs[0].Source != "node label" {
					t.Fatalf("device capability was not forwarded with its source: %+v", capacity.GPUs)
				}
			} else if capacity.GPUs[0].ComputeCapability != "" {
				t.Fatalf("invalid labels advertised a capability: %+v", capacity.GPUs)
			}
		})
	}
}
