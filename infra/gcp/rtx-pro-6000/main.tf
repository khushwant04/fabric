# A Google Cloud G4 VM with one NVIDIA RTX PRO 6000 (Blackwell, 96 GB) on Spot capacity,
# used as a Fabric GPU stamp. After it boots, run the one-command stamp bootstrap from the
# console's "Enroll stamp" dialog on it (see docs/getting-started.md).

resource "google_compute_instance" "rtx_pro_6000" {
  name         = var.name
  description  = "Fabric GPU stamp: NVIDIA RTX PRO 6000"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = var.network_tags

  # Selects this VM for the Ops Agent policy below.
  labels = var.enable_ops_agent ? { goog-ops-agent-policy = "v2-template-1-7-0" } : {}

  boot_disk {
    device_name = var.name

    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_size_gb
      type  = var.boot_disk_type
    }
  }

  guest_accelerator {
    count = var.gpu_count
    type  = "nvidia-rtx-pro-6000"
  }

  confidential_instance_config {
    enable_confidential_compute = false
  }

  key_revocation_action_type = "NONE"

  network_interface {
    subnetwork         = "projects/${var.project_id}/regions/${var.region}/subnetworks/${var.subnetwork}"
    subnetwork_project = var.project_id
    stack_type         = "IPV4_ONLY"

    access_config {
      network_tier = "PREMIUM"
    }
  }

  reservation_affinity {
    type = "NO_RESERVATION"
  }

  scheduling {
    # GPU VMs cannot live-migrate; Spot VMs are stopped, not deleted, when reclaimed.
    automatic_restart           = false
    on_host_maintenance         = "TERMINATE"
    provisioning_model          = var.spot ? "SPOT" : "STANDARD"
    instance_termination_action = var.spot ? "STOP" : null
  }

  service_account {
    email = var.service_account_email
    scopes = [
      "https://www.googleapis.com/auth/devstorage.read_only",
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring.write",
      "https://www.googleapis.com/auth/service.management.readonly",
      "https://www.googleapis.com/auth/servicecontrol",
      "https://www.googleapis.com/auth/trace.append",
    ]
  }
}

# Google Cloud Ops Agent on every VM in the zone carrying the label above, as the
# console's "Install Ops Agent" option sets up.
module "ops_agent_policy" {
  count = var.enable_ops_agent ? 1 : 0

  source        = "github.com/terraform-google-modules/terraform-google-cloud-operations/modules/ops-agent-policy"
  project       = var.project_id
  zone          = var.zone
  assignment_id = "goog-ops-agent-v2-template-1-7-0-${var.zone}"

  agents_rule = {
    package_state = "installed"
    version       = "latest"
  }

  instance_filter = {
    all = false
    inclusion_labels = [{
      labels = {
        goog-ops-agent-policy = "v2-template-1-7-0"
      }
    }]
  }
}
