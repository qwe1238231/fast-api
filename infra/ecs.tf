# --- ECS cluster + logging ---------------------------------------------------
# Fargate = serverless containers (no EC2 hosts to manage). The cluster itself
# is free; you pay per-task vCPU/memory-seconds while tasks run.

resource "aws_ecs_cluster" "main" {
  name = var.project

  # Publishes the ECS/ContainerInsights namespace (RunningTaskCount etc.) that
  # the task-count alarms need — the default AWS/ECS namespace has no task count.
  # "enabled" is the standard (cheaper) tier; bills per ingested metric/log.
  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Name = "${var.project}" }
}

# One log group for all three services; the log driver in each task definition
# writes to a distinct stream prefix (api / consumer / worker).
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project}"
  retention_in_days = 7 # dev: keep logs short/cheap
  tags              = { Name = "${var.project}" }
}
