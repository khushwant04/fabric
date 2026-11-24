variable "project_id" {
  description = "Google Cloud project that owns the VM and its subnetwork."
  type        = string
}

variable "region" {
  description = "Region of the subnetwork. G4 (RTX PRO 6000) capacity is limited to a few regions."
  type        = string
  default     = "us-east5"
}

variable "zone" {
  description = "Zone for the VM; it must offer g4-standard machines."
  type        = string
  default     = "us-east5-b"
}

variable "name" {
  description = "VM name, also used as the boot disk's device name."
  type        = string
  default     = "blackwell-rtx-pro-6000"
}

variable "machine_type" {
  description = "G4 machine type. g4-standard-48 carries one NVIDIA RTX PRO 6000 (96 GB)."
  type        = string
  default     = "g4-standard-48"
}

variable "gpu_count" {
  description = "Number of RTX PRO 6000 GPUs; must match what the machine type carries."
  type        = number
  default     = 1
}

variable "boot_image" {
  description = "Boot image. The Fabric bootstrap expects Ubuntu with an NVIDIA driver that supports Blackwell (CUDA 12.8 or newer)."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size. Model weights are cached on the node; a 35B FP8 model is about 37.5 GB."
  type        = number
  default     = 300
}

variable "boot_disk_type" {
  description = "Boot disk type."
  type        = string
  default     = "hyperdisk-balanced"
}

variable "subnetwork" {
  description = "Subnetwork name in var.region."
  type        = string
  default     = "default"
}

variable "spot" {
  description = "Run on Spot capacity (much cheaper, can be stopped by Google at any time)."
  type        = bool
  default     = true
}

variable "service_account_email" {
  description = "Service account the VM runs as, usually the project's default compute service account."
  type        = string
}

variable "network_tags" {
  description = "Network tags; http-server and https-server open ports 80 and 443 for the stamp gateway and its TLS certificate."
  type        = list(string)
  default     = ["http-server", "https-server"]
}
