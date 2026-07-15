# --- ECS services (keep the tasks running) -----------------------------------
# All three run in the PUBLIC subnets with a public IP (assign_public_ip=true):
# that's how they reach ECR + Stripe without a NAT Gateway. Inbound is still
# gated by the app SG (only the ALB may hit :8000), so a public IP != exposed.

resource "aws_ecs_service" "api" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1 # dev; bump to 2+ for HA across AZs
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # give the app time to boot before health checks can kill it
  health_check_grace_period_seconds = 60

  depends_on = [aws_lb_listener.http]
  tags       = { Name = "${var.project}-api" }
}

resource "aws_ecs_service" "consumer" {
  name            = "${var.project}-consumer"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.consumer.arn
  desired_count   = 1 # can scale out — the consumer group load-balances the stream
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  tags = { Name = "${var.project}-consumer" }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1 # SINGLETON — the ARQ crons must not double-fire
  launch_type     = "FARGATE"

  # Enforce the singleton ACROSS DEPLOYS. The Fargate default (max 200% / min
  # 100%) would start the new task before stopping the old one → 2 workers run
  # briefly → crons double-fire (double seat release → oversell). max 100% / min
  # 0% flips it to stop-old-then-start-new: a brief gap with 0 workers (fine for
  # periodic crons) instead of an overlap. The duplicate-task alarm backstops
  # the rare edge cases this can't prevent (e.g. a task stuck terminating).
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }

  tags = { Name = "${var.project}-worker" }
}
