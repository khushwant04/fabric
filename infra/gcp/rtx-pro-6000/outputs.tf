output "name" {
  description = "VM name."
  value       = google_compute_instance.rtx_pro_6000.name
}

output "external_ip" {
  description = "Public IP. Point the stamp's DNS name (for example gpu-01.stamps.example.com) at it before running the bootstrap."
  value       = google_compute_instance.rtx_pro_6000.network_interface[0].access_config[0].nat_ip
}

output "ssh_command" {
  description = "SSH into the VM to run the stamp bootstrap."
  value       = "gcloud compute ssh ${google_compute_instance.rtx_pro_6000.name} --zone ${google_compute_instance.rtx_pro_6000.zone} --project ${var.project_id}"
}
