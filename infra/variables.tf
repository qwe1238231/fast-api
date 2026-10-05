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

# --- 資料耐久性 / 可回溯 -------------------------------------------------------

variable "db_backup_retention_days" {
  description = "RDS automated backup retention. >0 enables point-in-time recovery; the validation forbids disabling it."
  type        = number
  default     = 7

  # **不能設成 0。** 0 = 關閉自動備份 = 關閉 PITR,而 PITR 是「壞掉的 migration 已經
  # 跑完了」唯一的解法(schema 可以 alembic downgrade,資料回不來)。這條 validation
  # 存在的理由很具體:這個值原本就是 0,註解寫著「dev: no backups (cheap, clean
  # destroy)」—— 一個為了方便而關掉的東西,如果沒有硬性限制,下一次想要「快一點
  # destroy」的人會再把它關掉。
  validation {
    condition     = var.db_backup_retention_days >= 7
    error_message = "Backups are not optional: keep at least 7 days of PITR window."
  }
}

# **它真正擋的不是手滑打 destroy,是一份寫著 `# forces replacement` 的 plan 被草率核准。**
#
# RDS 有好幾個屬性改動會強制重建(`storage_encrypted`、`db_name`、換 VPC……),而
# Terraform 的 RDS plan 又長又雜,那一行很容易被滑過去。重建是「先刪再建」,所以結果是:
# 最終快照有拍到,但**新實例是空的** —— 站台起來了,而資料要走一次還原流程。
# 打開這個旗標會讓那個 apply 在刪除那一步失敗,把一次資料可用性事故降級成一個錯誤訊息。
#
# 精確一點:它是**apply 期間**失敗而不是 plan 期間(deletion_protection 是 AWS 端的
# 檢查,Terraform 要真的呼叫 DeleteDBInstance 才會被拒)。所以同一次 apply 裡的其他
# 資源可能已經改掉了 —— 但資料庫還在,而那是這個旗標唯一要保證的事。
#
# 代價是每次 session 結束的 destroy 變成兩步。`-target` 讓那一步只動這個資源,約 20 秒
# —— 指令寫在 infra/README.md 的 golden rule 旁邊,有測試確保它留在那裡。
# 「摩擦要小到不值得繞過」是這個決定能不能活下來的關鍵:一個每天擋路的保護,最後
# 一定會被永久關掉,而那時它還留在程式碼裡,看起來像有保護。
#
# 環境拆分(staging/prod)之後這個變數就會有真正的用途:短命的練習環境設 false,
# 常駐環境保持 true。現在只有一個環境,所以預設值必須服務比較危險的那個情境。
variable "db_deletion_protection" {
  description = "Block deletion of the DB (including the delete half of a replacement). On by default; session-end destroy needs the two-step recipe in infra/README.md."
  type        = bool
  default     = true
}

variable "restore_from_snapshot_identifier" {
  description = "Create the DB FROM this snapshot instead of empty. This is the rollback lever — see infra/RUNBOOK.md."
  type        = string
  default     = ""
}

# **這個變數只有一個用途:還原的時候把它關掉。** 常態一律 true。
#
# 2026-08-18 還原演練的實測:從快照還原的那次 `terraform apply` 花了 28m23s,而 RDS
# 的事件紀錄把它拆開之後是——
#
#   05:58:12  Restored from snapshot ...          ← 還原本身只花約 3 分鐘
#   05:58:22  Applying modification to convert to a Multi-AZ DB Instance
#   06:19:12  Finished ...                        ← **20m50s,佔整段 74%**
#
# RDS 的行為是「先建單 AZ 實例,再當成一次修改轉成 Multi-AZ」。而 Terraform 要等整個
# apply 結束才回來,ECS 又要等 Terraform 才能重推 —— 所以服務為了一個**跟資料無關**
# 的可用性功能多停了 21 分鐘。
#
# 交叉驗證:同一天的 PITR(工作量更大 —— 還原基礎備份**加上**重放交易日誌)在單 AZ
# 下只花 10m05s。所以慢的不是還原,是 Multi-AZ 轉換。
#
# 而 Multi-AZ 轉換是**線上操作**,不需要在服務恢復之前完成。還原時 `-var db_multi_az=false`
# 讓 RTO 從 49m33s 降到約 18 分鐘,服務起來之後再跑一次不帶那個 var 的 apply 把冗餘
# 補回去。原則:**先恢復服務,再恢復冗餘。** 代價是中間約 20 分鐘沒有 HA —— 事故當下
# 這個取捨很明確,但它必須是一個明確的選擇,不是預設值。
variable "db_multi_az" {
  description = "Synchronous standby in a second AZ. Keep true; set false ONLY during a restore — see infra/RUNBOOK.md 情境 A 步驟 4."
  type        = bool
  default     = true
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

# api 的自動擴容 + 開賣預熱。**IAM 需求跟上面那個開關完全相同**(同一組
# application-autoscaling 動作),所以要打開就是兩個一起打開。
#
# 分成兩個變數而不是一個,是因為它們的風險不同:consumer 擴容只影響落帳速度,而 api
# 擴容會動到**對外服務的容量**與資料庫連線用量。分開讓「先開一個觀察」成為可能。
#
# 注意:api 的 HA(desired_count = 2)**不**取決於這個開關 —— 它寫在 services.tf,
# 關著這個開關仍然有兩個任務跨 AZ。這個開關只決定「會不會為了開賣而自動長到 5 個」。
variable "enable_api_autoscaling" {
  description = "Autoscale the api on the sale_imminent pre-warm signal + CPU. Same IAM needs as enable_consumer_autoscaling."
  type        = bool
  default     = false
}
