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

# No default on purpose. An empty webhook secret is a KNOWN secret: Stripe's
# signature check degenerates into "anyone who can reach /webhooks/stripe can
# mark any order paid". The app refuses to boot without it (Settings guard), so
# a missing value must fail at `terraform plan`, not at task start.
variable "stripe_webhook_secret" {
  description = "Stripe webhook signing secret, whsec_... (STRIPE_WEBHOOK_SECRET)."
  type        = string
  sensitive   = true

  validation {
    condition     = startswith(var.stripe_webhook_secret, "whsec_")
    error_message = "Must be the signing secret from the Stripe webhook endpoint (starts with whsec_), not the API key."
  }
}

# order-consumer 的自動擴容。**預設關閉,而且理由是 IAM 而不是「還沒做好」。**
#
# 實測 2026-08-14:`terraform validate` 與 `plan` 都是綠的,但 `apply` 會失敗 ——
# AccessDenied on `application-autoscaling:TagResource`。而且**繞不掉**:試過用不帶
# default_tags 的 provider 別名讓它不寫 tag,target 是建起來了,但 provider 讀回狀態時
# 仍然呼叫 `ListTagsForResource`,一樣被拒 —— 而那讓整個 state 變成無法操作(連 plan
# 與 destroy 都卡在同一個 refresh 上)。`AmazonECS_FullAccess` **不含**這幾個動作。
#
# 也就是說真正的硬性需求是**讀**:即使一個 tag 都沒有,provider 還是要讀得到。
#
# 要打開它,先給執行 terraform 的身分補這三條(其餘動作 ECS_FullAccess 已涵蓋):
#
#   application-autoscaling:ListTagsForResource   ← 一定要,即使不打算加 tag
#   application-autoscaling:TagResource
#   application-autoscaling:UntagResource
#
# 補完之後實測(2026-08-17)整套通:apply 建得起來、五個資源都進 state、
# `terraform plan -detailed-exitcode` 回 0(No changes),target 也正常被 default_tags
# 標上 Project/ManagedBy。
#
# 為什麼用開關而不是直接留著:留著的話任何人的 apply 都會在這裡炸,而錯誤訊息
# (AccessDenied on TagResource)跟「自動擴容」看起來毫無關係 —— 那是一顆地雷。
# 開關讓「還沒打開」是一個明確的狀態,而不是一個待踩的意外。
variable "enable_consumer_autoscaling" {
  description = "Autoscale the order-consumer on queue depth. Needs application-autoscaling Tag/ListTags/UntagResource — see the comment above."
  type        = bool
  default     = false
}
