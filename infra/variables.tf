# Inputs — override in terraform.tfvars or with -var if you want different values.

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-northeast-2" # Seoul
}

variable "project" {
  description = "Project name, used to name/tag resources."
  type        = string
  default     = "justin-test"
}

variable "db_username" {
  description = "RDS master username."
  type        = string
  default     = "ticket_admin"
}

variable "admin_cidr" {
  description = "Your laptop's public IP as a /32, allowed to reach RDS to run migrations (dev only). Set in terraform.tfvars."
  type        = string
}

# App secrets — set these in the gitignored terraform.tfvars (copy from your .env).
# Terraform bundles them into one Secrets Manager secret; ECS injects them at task start.
variable "app_secret_key" {
  description = "JWT signing key (SECRET_KEY)."
  type        = string
  sensitive   = true
}

variable "pii_kek_base64" {
  description = "PII key-encryption key, base64 of 32 bytes (PII_KEK_BASE64)."
  type        = string
  sensitive   = true
}

variable "pii_lookup_key_base64" {
  description = "PII blind-index key, base64 of 32 bytes (PII_LOOKUP_KEY_BASE64)."
  type        = string
  sensitive   = true
}

variable "stripe_secret_key" {
  description = "Stripe secret API key (STRIPE_SECRET_KEY)."
  type        = string
  sensitive   = true
}
