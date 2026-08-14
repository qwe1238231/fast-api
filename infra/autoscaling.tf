# --- order-consumer 自動擴容 --------------------------------------------------
#
# 為什麼只有 consumer 可以擴:
#   api      —— 可以擴,但目前的瓶頸不在它(ALB 前面有等候室在節流),先不動。
#   worker   —— **絕對不可以**。它跑 ARQ 的 cron,兩個實例會讓每一條定時任務重複觸發
#               (座位重複釋放 → 超賣)。services.tf 已經用 max 100% / min 0% 把它鎖成
#               跨部署的單例,這裡也不給它 autoscaling target。
#   consumer —— 消費者群組會把 stream 的 entry 分配給多個實例,所以它天生可以水平擴。
#
# 擴容的訊號是**佇列深度**而不是 CPU:consumer 大部分時間在 XREADGROUP 上阻塞等待,
# 落後的時候 CPU 也不會高 —— 用 CPU 當訊號會永遠不觸發。backlog 是 worker 的
# report_queue_depth 每分鐘用 PutMetricData 送上來的那個數字。
#
# **這帶來一個依賴:consumer 的擴容取決於 worker 活著**(沒有 worker 就沒有 metric,
# 告警會變 INSUFFICIENT_DATA 而不擴)。worker 掛掉本身由 ecs_service_down 告警覆蓋。

# desired_count 交給 autoscaling 之後,Terraform 不能再管它 —— 否則下一次 apply 會把
# 擴出去的實例數打回 1(而 plan 上只有一行 desired_count 變更)。跟 task_definition
# 同一個道理。
resource "aws_appautoscaling_target" "consumer" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.consumer.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 1

  # 上限由**資料庫連線預算**決定,不是「感覺夠不夠」。
  #   db.t4g.micro 的 max_connections = LEAST(DBInstanceClassMemory/9531392, 5000)
  #                                   ≈ 112(1 GiB)
  #   api      pool 5 + overflow 10 = 15/task,部署期間 2 個 task → 30
  #   worker   15(單例)
  #   consumer pool 2 + overflow 3  =  5/task(見下面的 consumer_pool_env)→ 6 個 = 30
  #   migration one-off task(只在部署期間)→ 15
  #   最壞情況 30 + 15 + 30 + 15 = 90,留 22 給 superuser 與監控連線。
  #
  # 要調高這個數字,必須先做一件事:縮某個 pool 或換大一號的 RDS。直接加會在下一次
  # 搶票尖峰把資料庫的連線用光 —— 而那個症狀是「全站 500」,看起來跟 consumer 無關。
  max_capacity = 6
}

resource "aws_cloudwatch_metric_alarm" "consumer_backlog_high" {
  alarm_name        = "${var.project}-consumer-backlog-high"
  alarm_description = "orders:stream backlog is growing — the consumer is falling behind; scaling out"

  namespace   = local.log_metric_namespace # 與 worker 的 PutMetricData 同一個 namespace
  metric_name = "order_stream_backlog"
  statistic   = "Average" # 每分鐘一個樣本 → Average == 那個樣本
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 500
  evaluation_periods  = 1 # 搶票的尖峰是秒級的,不能等三分鐘才決定要擴

  # 沒有資料**不算超標**:worker 掛掉(沒人送 metric)不該被誤判成 backlog 爆掉而狂擴。
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_appautoscaling_policy.consumer_out.arn]
  tags          = { Name = "${var.project}-consumer-backlog-high" }
}

resource "aws_cloudwatch_metric_alarm" "consumer_backlog_low" {
  alarm_name        = "${var.project}-consumer-backlog-low"
  alarm_description = "orders:stream backlog is drained — scaling the consumer back in"

  namespace   = local.log_metric_namespace
  metric_name = "order_stream_backlog"
  statistic   = "Average"
  period      = 60

  comparison_operator = "LessThanThreshold"
  threshold           = 50
  # 縮回去要比擴出去**慢得多**:擴錯的代價是幾分錢,縮錯的代價是在尖峰中間把消費者
  # 拿掉。10 個週期 = 連續 10 分鐘真的沒事了。
  evaluation_periods = 10
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_appautoscaling_policy.consumer_in.arn]
  tags          = { Name = "${var.project}-consumer-backlog-low" }
}

# 用 step scaling 而不是 target tracking:target tracking 要一個「每個實例分攤多少
# backlog」的比值 metric,而我們只有總量。step 也更好推理 —— 門檻與級距都寫在這裡。
resource "aws_appautoscaling_policy" "consumer_out" {
  name               = "${var.project}-consumer-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.consumer.service_namespace
  resource_id        = aws_appautoscaling_target.consumer.resource_id
  scalable_dimension = aws_appautoscaling_target.consumer.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    # 冷卻只有 60 秒:落後的時候每一分鐘都在累積未落帳的訂單(使用者已經拿到 202)。
    cooldown                = 60
    metric_aggregation_type = "Average"

    # 級距是相對於告警門檻(500)的。
    step_adjustment {
      metric_interval_lower_bound = 0    # backlog 500 ~ 5000
      metric_interval_upper_bound = 4500 #
      scaling_adjustment          = 2
    }
    step_adjustment {
      metric_interval_lower_bound = 4500 # backlog 5000 以上 → 直接加滿
      scaling_adjustment          = 4
    }
  }
}

resource "aws_appautoscaling_policy" "consumer_in" {
  name               = "${var.project}-consumer-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.consumer.service_namespace
  resource_id        = aws_appautoscaling_target.consumer.resource_id
  scalable_dimension = aws_appautoscaling_target.consumer.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    # 縮回去的冷卻是擴出去的 5 倍。ECS 縮容會挑一個任務送 SIGTERM,而 consumer 正在
    # 處理的那一筆 entry 要靠 reclaim 才會被別人接手 —— 那是分鐘級的延遲。
    cooldown                = 300
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_upper_bound = 0 # 一次只縮一個,慢慢退
      scaling_adjustment          = -1
    }
  }
}
