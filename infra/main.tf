# Terraform for GCP Cloud Run Deployment
provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  type        = string
  description = "The GCP Project ID"
}

variable "region" {
  type    = string
  default = "us-central1"
}

resource "google_cloud_run_service" "agent_api" {
  name     = "agent-swarm-api"
  location = var.region

  template {
    spec {
      containers {
        image = "gcr.io/${var.project_id}/agent-swarm-api:latest"
        ports {
          container_port = 8000
        }
        env {
          name  = "PHOENIX_COLLECTOR_ENDPOINT"
          value = "http://your-phoenix-instance:6006"
        }
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }
}

output "url" {
  value = google_cloud_run_service.agent_api.status[0].url
}
