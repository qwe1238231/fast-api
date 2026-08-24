# --- ECS task definitions (the container blueprints) -------------------------
# Three services share almost everything (image, secrets, Redis URL, log group);
# they differ only in command + whether they expose a port. So the common bits
# live in locals, and each task def is a thin wrapper.

locals {
  image = "${data.aws_ecr_repository.app.repository_url}:latest"

  # Non-sensitive env: Redis has no password here, so plain value is fine.
  redis_env = {
    name  = "REDIS_URL"
    value = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
  }

  # 這裡的每個請求都經過恰好一層我們自己的代理(ALB),所以 X-Forwarded-For 最右邊
  # 那一跳才是 ALB 親眼看到的客戶端。不設的話預設是 0 = 不看 XFF,於是所有請求的
  # 來源都變成 ALB 的內網 IP —— 每 IP 的限流就退化成全站共用一個桶。
  proxy_env = {
    name  = "TRUSTED_PROXY_COUNT"
    value = "1"
  }

  # 這是哪一種 process。兩個消費者:Postgres 的 application_name(查 pg_stat_activity
  # 時分得出是誰佔著連線)與 JSON log 的 component 欄位。
  #
  # **以前完全沒設**,所以三個服務都落在程式碼裡的預設值 "ticket-api" —— 於是
  # `application_name` 對「這條連線是誰開的」這個問題給的答案永遠是同一個,而那正是
  # 它存在的唯一理由。修掉它比加新東西重要。
  component_env = {
    api      = { name = "APP_COMPONENT", value = "ticket-api" }
    consumer = { name = "APP_COMPONENT", value = "ticket-consumer" }
    worker   = { name = "APP_COMPONENT", value = "ticket-worker" }
  }

  # statement_timeout **只給 api**。worker 的對帳/漂移 cron 有合法的長查詢,而
  # `alembic upgrade` 也是用 worker 的 task def 跑的(deploy.yml)—— 一個被砍掉的
  # ALTER TABLE 比一個慢查詢糟得多。
  # 10 秒 < REQUEST_TIMEOUT_SECONDS(15),所以順序是「資料庫先殺掉查詢、連線還回池子」,
  # 而不是「請求先放棄、查詢還在後面跑」。Settings 有 validator 釘住這個關係。
  api_statement_timeout_env = { name = "DB_STATEMENT_TIMEOUT_MS", value = "10000" }

  # consumer 是**序列**處理的(run_order_consumer_loop → _consume_batch 一個 for 迴圈,
  # 一次一筆),所以它同時只用到一條 DB 連線。給它跟 api 一樣的 5+10 池子純粹是浪費
  # 連線預算 —— 而連線預算正是 autoscaling.tf 裡 max_capacity 的限制因素。縮成 2+3
  # 讓擴容上限從 3 個實例變成 6 個。
  consumer_pool_env = [
    { name = "DB_POOL_SIZE", value = "2" },
    { name = "DB_MAX_OVERFLOW", value = "3" },
  ]

  # api 現在會**水平擴容**(autoscaling.tf),所以它的池子從「一個任務用得爽」變成
  # 「乘上任務數還要能塞進 RDS」。3+2 = 5/任務。
  #
  # 為什麼 5 夠:api 的每個請求碰 DB 的次數很少 —— 認證查一次 user、場次設定走
  # Redis 快取、而下單只寫 Redis(DB 的 INSERT 在 consumer)。池子滿的時候請求會**等**,
  # 那是背壓,比把 Postgres 的連線用光好得多(後者的症狀是全站 500)。
  api_pool_env = [
    { name = "DB_POOL_SIZE", value = "3" },
    { name = "DB_MAX_OVERFLOW", value = "2" },
  ]

  # worker 的池子。它是單例,但 ARQ 會**同時**跑多個 cron(app/worker.py 的
  # `max_jobs = 4` 明確定住了幾個),每個 job 開 1~2 條連線 → 3+3 = 6 有餘裕。
  # 這個 task def 也是部署時跑 `alembic upgrade` 用的那個,而 alembic 只用一條連線。
  worker_pool_env = [
    { name = "DB_POOL_SIZE", value = "3" },
    { name = "DB_MAX_OVERFLOW", value = "3" },
  ]

  # Sensitive env: pull each key out of the ONE JSON secret. The `:KEY::` suffix
  # selects a json field (the two trailing colons = default version-stage/id).
  # ECS (via the execution role) reads these at task start and injects them as
  # env vars — the values never appear in the task def or console.
  #
  # **這裡引用的是 `aws_secretsmanager_secret`(殼),不是 `..._secret_version`(值)。**
  # 而那個區別在依賴圖上是致命的,所以三個 task def 都補了
  # `depends_on = [aws_secretsmanager_secret_version.app]`。
  #
  # 2026-08-18 還原演練實測到的事:secret_version 引用了
  # `aws_db_instance.main.address`,所以它必須等 RDS 建好 —— Multi-AZ 要十幾分鐘。
  # 而 task def 只需要殼(`CreateSecret` 不需要有值就會成功),所以圖上允許
  # 「ECS 服務先建好、秘密的值後寫入」。實際發生的順序:
  #
  #   1. 建秘密的殼(瞬間)→ 2. 建 task def → 3. 建 ECS 服務,開始放任務
  #   4. 任務讀值 → `ResourceNotFoundException: ... staging label: AWSCURRENT`
  #   5. deployment_circuit_breaker 判定 tasks failed to start,**放棄且不再重試**
  #   6. 十幾分鐘後 RDS 好了 → 寫入秘密版本 → `terraform apply` 全綠
  #
  # 熔斷器放棄的時間是 02:11:10Z,秘密寫入是 02:12:19Z —— **輸了 69 秒**。之後環境
  # 的每一項設定都正確,而三個服務全死,唯一症狀是 ALB 的 503。
  #
  # 而且它是**賽跑**:RDS 快一點就會過。不可重現的「環境開起來是死的」比每次都死
  # 難修得多 —— 它會被當成「不知道為什麼,重跑一次就好了」而永遠留著。
  #
  # 以前沒被發現,是因為每次驗證都走 CD:CD 會註冊新的 task def 修訂版並
  # `update-service`,那是一次**全新的部署**,順手把開機時失敗的那個蓋掉了。
  # 也就是說「CD 端到端測過」證明的是 CD 能用,不是 `terraform apply` 能把環境帶起來。
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
    environment = concat(local.api_pool_env, [
      local.redis_env,
      local.proxy_env,
      local.component_env.api,
      local.api_statement_timeout_env,
      # CORS_ALLOW_ORIGINS 刻意**不在這裡** —— 還沒有前端,而正確的值就是「關著」
      # (空字串 = 不掛 CORS middleware)。前端上線時在這裡加一行明確的來源清單;
      # 不要為了「先能動」填 `*`,那在 credentialed 請求下根本不會動(且啟動會被
      # Settings 的 validator 擋下來)。
    ])
    secrets = local.app_secrets
    # command omitted → uses the image's default CMD (uvicorn app.main:app ... :8000)
    logConfiguration = { logDriver = "awslogs", options = local.log_options.api }
  }])

  # 秘密要**有值**才能跑,不只是存在 —— 理由與實測時間軸見 local.app_secrets 上方。
  depends_on = [aws_secretsmanager_secret_version.app]

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
    environment      = concat([local.redis_env, local.component_env.consumer], local.consumer_pool_env)
    secrets          = local.app_secrets
    logConfiguration = { logDriver = "awslogs", options = local.log_options.consumer }
  }])

  # 秘密要**有值**才能跑,不只是存在 —— 理由與實測時間軸見 local.app_secrets 上方。
  depends_on = [aws_secretsmanager_secret_version.app]

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
    environment = concat(local.worker_pool_env, [
      local.redis_env,
      local.component_env.worker,
      { name = "AWS_REGION", value = var.region },
      { name = "PIPELINE_METRIC_NAMESPACE", value = "${var.project}/pipeline" },
    ])
    secrets          = local.app_secrets
    logConfiguration = { logDriver = "awslogs", options = local.log_options.worker }
  }])

  # 秘密要**有值**才能跑,不只是存在 —— 理由與實測時間軸見 local.app_secrets 上方。
  depends_on = [aws_secretsmanager_secret_version.app]

  tags = { Name = "${var.project}-worker" }
}
