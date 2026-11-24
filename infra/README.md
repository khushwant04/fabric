# Infrastructure as code

Terraform for the machines Fabric runs on. The platform itself (control plane, edge, stamps)
is installed with Helm and the bootstrap scripts; this folder only creates the VMs.

| Path | What it creates |
|---|---|
| [`gcp/rtx-pro-6000/`](gcp/rtx-pro-6000/) | A Google Cloud G4 VM with one NVIDIA RTX PRO 6000 (Blackwell, 96 GB) on Spot capacity, used as a GPU stamp |

## Create the RTX PRO 6000 stamp VM

```bash
cd infra/gcp/rtx-pro-6000
cp terraform.tfvars.example terraform.tfvars   # set project_id and service_account_email
terraform init
terraform plan
terraform apply
```

Then:

1. Point the stamp's DNS name at the `external_ip` output.
2. SSH in with the `ssh_command` output.
3. In the console, open **Inference stamps → Enroll stamp**, and run the setup command it gives you on the VM.

Spot VMs are much cheaper but Google can stop them at any time; set `spot = false` for a
demo that must not be interrupted. Stop the VM when you are not using it
(`gcloud compute instances stop <name> --zone <zone>`), or remove it with `terraform destroy`.

The project needs G4 quota in the chosen region (`NVIDIA_RTX_PRO_6000_GPUS` or the Spot
equivalent). `terraform.tfvars` and state files are git-ignored: they hold project details.
