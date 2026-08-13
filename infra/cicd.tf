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
    resources = [aws_ecr_repository.app.arn]
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
