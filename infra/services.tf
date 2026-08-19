# --- ECS services (keep the tasks running) -----------------------------------
# All three run in the PUBLIC subnets with a public IP (assign_public_ip=true):
# that's how they reach ECR + Stripe without a NAT Gateway. Inbound is still
# gated by the app SG (only the ALB may hit :8000), so a public IP != exposed.

resource "aws_ecs_service" "api" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  launch_type     = "FARGATE"

  # **2 是 HA 的下限,不是效能調校。** 一個任務的時候,那個任務所在的 AZ 出問題、
  # 或它自己被回收,站台就是全黑;而部署期間也只有「舊的停掉、新的還沒健康」這一種
  # 狀態。Fargate 會把任務分散到 networkConfiguration 裡的兩個子網(= 兩個 AZ)。
  #
  # 這個值在 autoscaling 打開之後由 autoscaling 接管(min_capacity 也是 2),但它必須
  # 寫在這裡:HA **不應該**取決於 autoscaling 那個開關有沒有打開。
  desired_count = 2

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

  # CI 會註冊「釘住 commit SHA」的新 task def 修訂版並把服務指過去。不忽略這個欄位
  # 的話,下一次 terraform apply 會把服務打回 TF 管的那一版(image 是 :latest)——
  # 也就是把剛部署的東西默默換掉,而 plan 上只會顯示一行 task_definition 變更。
  lifecycle {
    # desired_count 由 autoscaling.tf 的 appautoscaling target 管(預熱會把它拉到
    # max)。不忽略的話,下一次 apply 會在**開賣中間**把它打回 2 —— 而 plan 上只有
    # 一行 desired_count 變更。跟 consumer 是同一個坑,這是它的第三個實例。
    ignore_changes = [task_definition, desired_count]
  }

  # 部署自己會回滾。少了它,新版本一直起不來時 ECS 會永遠重試,服務停在「舊的還在跑
  # 但新的一直死」的半吊子狀態 —— 而 CI 那邊 wait services-stable 只會逾時,沒有人
  # 把它推回去。有了它,wait 失敗的意思變成「已經回滾了」,是可以安心讀的訊號。
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = { Name = "${var.project}-api" }
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


  # CI 會註冊「釘住 commit SHA」的新 task def 修訂版並把服務指過去。不忽略這個欄位
  # 的話,下一次 terraform apply 會把服務打回 TF 管的那一版(image 是 :latest)——
  # 也就是把剛部署的東西默默換掉,而 plan 上只會顯示一行 task_definition 變更。
  lifecycle {
    # desired_count 由 autoscaling.tf 的 appautoscaling target 管。不忽略的話,下一次
    # terraform apply 會把擴出去的實例數打回 1 —— 而 plan 上只有一行 desired_count。
    ignore_changes = [task_definition, desired_count]
  }

  # 部署自己會回滾。少了它,新版本一直起不來時 ECS 會永遠重試,服務停在「舊的還在跑
  # 但新的一直死」的半吊子狀態 —— 而 CI 那邊 wait services-stable 只會逾時,沒有人
  # 把它推回去。有了它,wait 失敗的意思變成「已經回滾了」,是可以安心讀的訊號。
  deployment_circuit_breaker {
    enable   = true
    rollback = true
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


  # CI 會註冊「釘住 commit SHA」的新 task def 修訂版並把服務指過去。不忽略這個欄位
  # 的話,下一次 terraform apply 會把服務打回 TF 管的那一版(image 是 :latest)——
  # 也就是把剛部署的東西默默換掉,而 plan 上只會顯示一行 task_definition 變更。
  lifecycle {
    ignore_changes = [task_definition]
  }

  # 部署自己會回滾。少了它,新版本一直起不來時 ECS 會永遠重試,服務停在「舊的還在跑
  # 但新的一直死」的半吊子狀態 —— 而 CI 那邊 wait services-stable 只會逾時,沒有人
  # 把它推回去。有了它,wait 失敗的意思變成「已經回滾了」,是可以安心讀的訊號。
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  tags = { Name = "${var.project}-worker" }
}
