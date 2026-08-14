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

# 只給 aws_appautoscaling_target 用的別名 —— **刻意沒有 default_tags**。
#
# 帶 tag 的話 provider 會在 RegisterScalableTarget 裡送 Tags,而那需要
# `application-autoscaling:TagResource`;實測(2026-08-14)本機的 IAM user 沒有那條
# 權限,apply 會 AccessDeniedException 而四個 autoscaling 資源全部建不起來 ——
# 而 `terraform validate` 與 `terraform plan` 都是綠的,只有 apply 才會發現。
#
# 取捨:這一個資源不會被 Project/ManagedBy 標到。可接受,因為 scalable target 不計費,
# 而 default_tags 的用途(在 cost explorer 裡找忘記關的東西)本來就只針對計費資源。
# 想要標它就得補那條 IAM 權限 —— 那是帳號層級的授權決定,不該由 Terraform 偷偷繞過。
provider "aws" {
  alias  = "untagged"
  region = var.region
}
