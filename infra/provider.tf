# The AWS provider picks up credentials from the environment / ~/.aws the same
# way the AWS CLI does (so `aws sts get-caller-identity` working == this works).
provider "aws" {
  region = var.region

  # Tag everything Terraform creates, so it's easy to find (and spot leftovers
  # you forgot to destroy) in the console / cost explorer.
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
