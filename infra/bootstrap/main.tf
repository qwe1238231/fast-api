# --- Bootstrap:不隨環境生滅的東西 -------------------------------------------
#
# 這份設定**有自己的 state**,而且刻意跟 `../` 分開。它只放一種東西:**重建環境的
# 前提**。目前只有 ECR。
#
# ## 為什麼 ECR 不能待在主設定裡
#
# 主設定的工作方式是「每次 session 結束就 destroy」(見 ../README.md 的 golden rule),
# 而 `aws_ecr_repository` 原本設了 `force_delete = true` —— 所以 destroy 會把
# repository 連裡面的映像一起刪掉。下一次 apply 建出來的是一個**空的** repo,而
# task def 釘的是 `:latest`。
#
# 2026-08-18 的還原演練實測到這個組合的長相:資料庫從快照還原得漂漂亮亮、
# `terraform apply` 全綠,然後 ALB 一直回 503 —— 因為**沒有東西可以跑**:
#
#     CannotPullContainerError: ... /justin-test:latest: not found
#
# 而 ../RUNBOOK.md 的「destroy 之後怎麼把資料拿回來」從頭到尾沒提映像這件事。
# 也就是說那份還原程序即使每一步都做對,結果仍然是一個不能服務的站台。
#
# 映像倉庫是**重建環境的輸入**,不是環境的一部分。它跟環境同生共死是個分類錯誤。
#
# 這跟 ../cicd.tf 對 GitHub OIDC provider 的判斷是同一個:**引用而不是擁有**。
# 那裡的理由是「destroy 會刪掉別的專案也在用的東西」;這裡的理由是「destroy 會刪掉
# 你下一次 apply 需要的東西」。兩者都指向同一個結論。
#
# ## 用法(一輩子只跑一次)
#
#     terraform -chdir=infra/bootstrap init
#     terraform -chdir=infra/bootstrap apply
#
# 之後主設定用 `data "aws_ecr_repository"` 引用它,destroy 不會碰到。
#
# state 不見了也不要緊 —— 這裡沒有任何機密,而且 repository 是可以 import 回來的:
#
#     terraform -chdir=infra/bootstrap import aws_ecr_repository.app justin-test

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Lifecycle = "bootstrap" # 這個標籤就是「destroy 不該碰我」的線索
    }
  }
}

variable "region" {
  description = "AWS region. Must match the main config's var.region."
  type        = string
  default     = "ap-northeast-2"
}

variable "project" {
  description = "Project name. Must match the main config's var.project — it IS the repository name."
  type        = string
  default     = "justin-test"
}

# **`force_delete` 刻意是 false(預設值)。** 倉庫裡有映像時 destroy 會失敗,而那正是
# 想要的:刪掉這個 repository 是一個需要先清空它的、刻意的動作,不是某次 destroy 的
# 副作用。這是它從主設定搬過來的**全部理由**,不要為了「destroy 順一點」改回 true。
resource "aws_ecr_repository" "app" {
  name = var.project

  image_scanning_configuration {
    scan_on_push = true # free vulnerability scan on each push
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  # 保留最後 5 個。它跟著 repository 搬過來 —— 留在主設定的話,每次 destroy 都會把
  # 策略刪掉,於是倉庫會在「有策略」與「沒策略」之間來回,而沒有人會注意到。
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

output "repository_url" {
  description = "Feed this to `docker build -t <url>:latest`. The main config reads it via a data source."
  value       = aws_ecr_repository.app.repository_url
}
