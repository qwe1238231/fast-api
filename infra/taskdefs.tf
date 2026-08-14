# --- ECS task definitions (the container blueprints) -------------------------
# Three services share almost everything (image, secrets, Redis URL, log group);
# they differ only in command + whether they expose a port. So the common bits
# live in locals, and each task def is a thin wrapper.

locals {
  image = "${aws_ecr_repository.app.repository_url}:latest"

  # Non-sensitive env: Redis has no password here, so plain value is fine.
  redis_env = {
    name  = "REDIS_URL"
    value = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:6379/0"
  }

  # 這裡的每個請求都經過恰好一層我們自己的代理(ALB),所以 X-Forwarded-For 最右邊
  # 那一跳才是 ALB 親眼看到的客戶端。不設的話預設是 0 = 不看 XFF,於是所有請求的
  # 來源都變成 ALB 的內網 IP —— 每 IP 的限流就退化成全站共用一個桶。
  proxy_env = {
    name  = "TRUSTED_PROXY_COUNT"
    value = "1"
  }

  # consumer 是**序列**處理的(run_order_consumer_loop → _consume_batch 一個 for 迴圈,
  # 一次一筆),所以它同時只用到一條 DB 連線。給它跟 api 一樣的 5+10 池子純粹是浪費
  # 連線預算 —— 而連線預算正是 autoscaling.tf 裡 max_capacity 的限制因素。縮成 2+3
  # 讓擴容上限從 3 個實例變成 6 個。
  consumer_pool_env = [
    { name = "DB_POOL_SIZE", value = "2" },
    { name = "DB_MAX_OVERFLOW", value = "3" },
  ]

  # Sensitive env: pull each key out of the ONE JSON secret. The `:KEY::` suffix
  # selects a json field (the two trailing colons = default version-stage/id).
  # ECS (via the execution role) reads these at task start and injects them as
  # env vars — the values never appear in the task def or console.
  app_secrets = [
    for k in ["SECRET_KEY", "PII_KEK_BASE64", "PII_LOOKUP_KEY_BASE64", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "DATABASE_URL"] : {
      name      = k
      valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::"
    }
  ]

  # Shared Fargate sizing (smallest valid combo — dev).
  task_cpu    = 256 # 0.25 vCPU
  task_memory = 512 # MB
}

# Reusable log config — one per stream prefix.
locals {
  log_options = {
    api      = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "api" }
    consumer = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "consumer" }
    worker   = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "worker" }
  }
}

# --- API (uvicorn, behind the ALB) -------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.task_cpu
  memory                   = local.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64" # CI builds native x86 on the GitHub runner
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }] # only the API exposes a port
    environment  = [local.redis_env, local.proxy_env]
    secrets      = local.app_secrets
    # command omitted → uses the image's default CMD (uvicorn app.main:app ... :8000)
    logConfiguration = { logDriver = "awslogs", options = local.log_options.api }
  }])

  tags = { Name = "${var.project}-api" }
}

# --- order-consumer (drains the Redis stream to Postgres; can scale out) -----
resource "aws_ecs_task_definition" "consumer" {
  family                   = "${var.project}-consumer"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.task_cpu
  memory                   = local.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name             = "consumer"
    image            = local.image
    essential        = true
    command          = ["python", "-m", "app.order_consumer"] # override CMD; no port
    environment      = concat([local.redis_env], local.consumer_pool_env)
    secrets          = local.app_secrets
    logConfiguration = { logDriver = "awslogs", options = local.log_options.consumer }
  }])

  tags = { Name = "${var.project}-consumer" }
}

# --- ARQ worker (cron jobs; MUST stay a singleton — desired_count=1 in service) ---
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.task_cpu
  memory                   = local.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["arq", "app.worker.WorkerSettings"] # override CMD; no port
    # report_queue_depth publishes pipeline gauges via boto3 PutMetricData:
    #  - AWS_REGION: Fargate does NOT auto-set it; boto3 needs a region.
    #  - PIPELINE_METRIC_NAMESPACE: keeps the project-scoped namespace out of app
    #    code (must match the IAM condition + the alarm namespace).
    environment = [
      local.redis_env,
      { name = "AWS_REGION", value = var.region },
      { name = "PIPELINE_METRIC_NAMESPACE", value = "${var.project}/pipeline" },
    ]
    secrets          = local.app_secrets
    logConfiguration = { logDriver = "awslogs", options = local.log_options.worker }
  }])

  tags = { Name = "${var.project}-worker" }
}
