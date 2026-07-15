# Pin Terraform + the AWS provider so everyone (and CI) builds the same thing.
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is kept locally (terraform.tfstate) for now — fine for a solo learning
  # project. A shared/team setup would use an S3 backend + DynamoDB lock instead.
}
