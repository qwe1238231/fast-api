# --- CI/CD: let GitHub Actions deploy without long-lived AWS keys (OIDC) -----
# GitHub Actions presents a short-lived OIDC token proving "I'm a run of
# repo X on branch main"; AWS (trusting GitHub's OIDC provider) swaps it for
# 15-min temporary credentials scoped to the role below. No secrets stored.

variable "github_repo" {
  description = "owner/repo allowed to assume the CI role."
  type        = string
  default     = "qwe1238231/fast-api"
}

# 1) The GitHub OIDC provider is ACCOUNT-GLOBAL (one per URL) and ALREADY EXISTS
#    in this shared account (another project created it), so we REFERENCE it via
#    a data source instead of creating it. Two reasons: (a) creating a duplicate
#    is a 409 error; (b) if we owned it, `terraform destroy` would delete a
#    provider other projects depend on. NOTE: the existing provider must list
#    "sts.amazonaws.com" as an audience (client_id) or the assume will fail at
#    deploy — standard for GitHub↔AWS OIDC, so almost certainly already set.
# 快照的 ARN 要自己組(create-db-snapshot 的資源層級授權需要帳號 id),而帳號 id
# 不該寫死在檔案裡。
data "aws_caller_identity" "current" {}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# 2) Trust policy: ONLY runs of <repo> on refs/heads/main may assume this role.
#    Locking `sub` to the branch is what stops a fork / another branch / PR from
#    getting deploy creds.
data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${var.project}-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
  tags               = { Name = "${var.project}-github-actions" }
}

# 3) What CI may do: push to ECR, run the migration task, roll the ECS services.
data "aws_iam_policy_document" "cicd" {
  # ECR login token is an account-level call -> must be "*"
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  # Push/pull layers to our repo only
  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
    ]
    resources = [data.aws_ecr_repository.app.arn]
  }
  # Roll services + run the one-off migration task + register new task defs
  statement {
    actions = [
      "ecs:UpdateService", "ecs:DescribeServices",
      "ecs:RunTask", "ecs:DescribeTasks", "ecs:RegisterTaskDefinition",
      # 讀現行的 task def 才能「照抄一份、只換 image」重新註冊(把 image 釘在
      # commit SHA 上)。少了它,CI 只能自己重打整份定義 —— 那就是第二份 task def
      # 規格,而它會跟 Terraform 那份慢慢漂開。
      "ecs:DescribeTaskDefinition",
    ]
    resources = ["*"] # dev: broad; could scope to this cluster's ARNs
  }
  # 遷移失敗時把容器的 log 印進 CI 輸出。少了這個,一次失敗的 migration 在 Actions
  # 上就只是一行 "exit code 1",要開 AWS console 才知道是哪一句 SQL 炸了。
  statement {
    actions   = ["logs:GetLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
  # 帶 migration 的部署要先拍一張還原點快照(deploy.yml 的 "Snapshot the database
  # before migrating")。**沒有 AddTagsToResource** 是刻意的:CI 那邊不帶 --tags,
  # 快照的標籤由實例的 copy_tags_to_snapshot 帶過來 —— 這個專案已經被「plan 綠、
  # apply 因為缺 Tag 權限而炸」咬過一次。
  #
  # 只給 Create/Describe,**不給 Delete**:清理舊快照是人的決定(它們是還原點),
  # 而一個能刪快照的 CI 憑證會讓「備份」這件事失去意義。
  statement {
    actions = ["rds:CreateDBSnapshot", "rds:DescribeDBSnapshots"]
    resources = [
      aws_db_instance.main.arn,
      "arn:aws:rds:${var.region}:${data.aws_caller_identity.current.account_id}:snapshot:${var.project}-db-premigration-*",
    ]
  }
  # 部署後的煙霧測試要先問出 ALB 的 DNS 名稱才打得到 /health/deps。
  # DescribeLoadBalancers 不支援資源層級的授權(只能 "*"),但它是純唯讀。
  statement {
    actions   = ["elasticloadbalancing:DescribeLoadBalancers"]
    resources = ["*"]
  }
  # RunTask/UpdateService must be allowed to PASS the task roles to the tasks
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn]
  }
}

resource "aws_iam_role_policy" "cicd" {
  name   = "${var.project}-cicd"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.cicd.json
}

# The workflow needs this ARN (set it as a GitHub Actions variable / in the yml).
output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}
