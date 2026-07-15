# --- IAM roles for ECS tasks -------------------------------------------------
# Two distinct roles (ECS's standard split):
#   - execution role: used by the ECS agent to START the task — pull the image
#     from ECR and ship logs to CloudWatch. (Not your app's credentials.)
#   - task role: assumed by the CONTAINER itself for any AWS API calls your code
#     makes. Empty for now; Phase F attaches Secrets Manager read here.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role + the AWS-managed policy that grants ECR pull + CloudWatch logs.
resource "aws_iam_role" "ecs_execution" {
  name               = "${var.project}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = { Name = "${var.project}-ecs-execution" }
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role — the app's own identity inside the container, for AWS API calls the
# CODE makes at runtime. (App secrets are injected via the EXECUTION role's
# valueFrom, not here.) The worker's report_queue_depth cron publishes pipeline
# depth gauges to CloudWatch, so it needs PutMetricData.
resource "aws_iam_role" "ecs_task" {
  name               = "${var.project}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = { Name = "${var.project}-ecs-task" }
}

# PutMetricData has NO resource-level ARN (resources must be "*"), so we scope it
# with a condition on the metric namespace instead of granting blanket access.
data "aws_iam_policy_document" "ecs_task_metrics" {
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["${var.project}/pipeline"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_task_metrics" {
  name   = "${var.project}-ecs-task-metrics"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_metrics.json
}
