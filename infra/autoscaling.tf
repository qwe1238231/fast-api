# --- 自動擴容 -----------------------------------------------------------------
#
# 誰可以擴:
#   api      —— 可以,而且**必須**(見下面的 api 區塊)。上一版的註解寫「瓶頸不在它,
#               先不動」,理由是「ALB 前面有等候室在節流」—— 那句話有個漏洞:等候室
#               **自己就跑在 api 上**。`/queue/status` 與那條最長 300 秒的 SSE 都是
#               api 在扛,而等候室節流的是**下游的下單**,不是它自己的連線量。
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
  count = var.enable_consumer_autoscaling ? 1 : 0

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.consumer.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 1

  # 上限由**資料庫連線預算**決定,不是「感覺夠不夠」。
  #   db.t4g.micro 的 max_connections = LEAST(DBInstanceClassMemory/9531392, 5000)
  #                                   ≈ 112(1 GiB)
  #   api      pool 3 + overflow 2 = 5/task;max 5 個,部署期間 200% → 10 個 → 50
  #   worker   pool 3 + overflow 3 = 6(單例;ARQ max_jobs=4 把並發 job 數定住了)
  #   consumer pool 2 + overflow 3 = 5/task → 6 個 = 30
  #   migration one-off task(worker 的 task def,只在部署期間)→ 6
  #   最壞情況 50 + 6 + 30 + 6 = 92,留 20 給 superuser 與監控連線。
  #
  # 這份算術**同時**釘住四個服務的池子大小與兩個 max_capacity,所以它有一條資料驅動的
  # 測試(test_deploy_pipeline.py::test_the_scaling_ceiling_respects_the_connection_budget)
  # —— 每個數字都從 .tf 讀出來,不能只改一邊。
  #
  # 這 92 是**上界**不是預測值。2026-08-14 實測(api 1 + consumer 3 + worker 1 個任務、
  # 無負載):RDS 的 DatabaseConnections = **4**。SQLAlchemy 的 QueuePool 是懶建立的,
  # pool_size 只是「閒置時最多保留幾條」而不是預先配置。所以閒置基線極低,而峰值
  # **這次沒有驗到**(要壓測才會逼出來)。用上界當容量天花板是對的,但別把 92 當成
  # 「一定會用到 92」。
  #
  # 要調高這個數字,必須先做一件事:縮某個 pool 或換大一號的 RDS。直接加會在下一次
  # 搶票尖峰把資料庫的連線用光 —— 而那個症狀是「全站 500」,看起來跟 consumer 無關。
  max_capacity = 6
}

resource "aws_cloudwatch_metric_alarm" "consumer_backlog_high" {
  count = var.enable_consumer_autoscaling ? 1 : 0

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

  alarm_actions = [aws_appautoscaling_policy.consumer_out[0].arn]
  tags          = { Name = "${var.project}-consumer-backlog-high" }
}

resource "aws_cloudwatch_metric_alarm" "consumer_backlog_low" {
  count = var.enable_consumer_autoscaling ? 1 : 0

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

  alarm_actions = [aws_appautoscaling_policy.consumer_in[0].arn]
  tags          = { Name = "${var.project}-consumer-backlog-low" }
}

# 用 step scaling 而不是 target tracking:target tracking 要一個「每個實例分攤多少
# backlog」的比值 metric,而我們只有總量。step 也更好推理 —— 門檻與級距都寫在這裡。
resource "aws_appautoscaling_policy" "consumer_out" {
  count = var.enable_consumer_autoscaling ? 1 : 0

  name               = "${var.project}-consumer-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.consumer[0].service_namespace
  resource_id        = aws_appautoscaling_target.consumer[0].resource_id
  scalable_dimension = aws_appautoscaling_target.consumer[0].scalable_dimension

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
  count = var.enable_consumer_autoscaling ? 1 : 0

  name               = "${var.project}-consumer-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.consumer[0].service_namespace
  resource_id        = aws_appautoscaling_target.consumer[0].resource_id
  scalable_dimension = aws_appautoscaling_target.consumer[0].scalable_dimension

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

# --- api 自動擴容 + 預熱 -------------------------------------------------------
#
# **搶票的尖峰是秒級的,而 ECS 的反應式擴容是分鐘級的。** 拉 image、啟動 uvicorn、
# 等 ALB 健康檢查通過,加起來是分鐘;等 CPU 高了才開始擴,尖峰已經結束了 —— 而它
# 結束的方式是使用者吃 503。所以這裡有兩種擴容,而**主力是前者**:
#
#   1) 預熱(排程性) —— worker 的 publish_prewarm_signal 每分鐘發 `sale_imminent`
#      (0/1,窗是 sale_starts_at ± 設定值)。窗一開就把容量**直接設到上限**。
#      開賣時間只存在資料庫裡,Terraform 看不到,所以只能透過一個指標傳進來。
#   2) 反應式(CPU) —— 預熱窗之外的意外流量(有人上新聞、爬蟲、DDoS 前哨)。
#
# 為什麼是 step scaling 而不是 target tracking:跟 consumer 同一個理由(門檻與級距
# 寫在這裡、好推理),再加上預熱要的是「設成 N」這種動作 —— target tracking 只會
# 追一個比值,表達不了「開賣前先站好」。
#
# 縮容只有一條,而且刻意很慢:見 api_low_load 那條告警裡的 metric math。

resource "aws_appautoscaling_target" "api" {
  count = var.enable_api_autoscaling ? 1 : 0

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"

  # 2 = 跨 AZ 的 HA 下限,跟 services.tf 的 desired_count 一致。**不要設成 1** ——
  # 縮到一個任務等於把「單點故障」當成省錢手段,而省下的是每月幾塊美金。
  min_capacity = 2

  # 5 的來源是上面那份連線預算,不是「感覺夠」。要再高必須先做其中一件:
  #   - 換大一號的 RDS(max_connections 隨記憶體線性成長),或
  #   - 前面擺一個連線池(RDS Proxy / pgbouncer)—— 這才是業界對「app 要擴但資料庫
  #     連線不夠」的標準答案,因為它讓 app 的任務數與資料庫連線數解耦。
  # 直接把這個數字改大會在下一次搶票尖峰把連線用光,而症狀是「全站 500」。
  max_capacity = 5
}

# ── (1) 預熱:開賣窗內直接站到上限
resource "aws_cloudwatch_metric_alarm" "sale_imminent" {
  count = var.enable_api_autoscaling ? 1 : 0

  alarm_name        = "${var.project}-sale-imminent"
  alarm_description = "a sale window is open or about to open — hold pre-warmed api capacity"

  namespace   = local.log_metric_namespace # 與 worker 的 PutMetricData 同一個 namespace
  metric_name = "sale_imminent"
  statistic   = "Maximum" # 0/1 的旗標:窗內任何一個樣本為 1 就算開著
  period      = 60

  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1 # 預熱不能等 —— 這正是「提前」的意思

  # 沒有資料**不算開賣**:worker 掛掉(沒人送指標)不該被誤判成永遠在開賣而一直用滿
  # 容量。worker 掛掉本身由 ecs_service_down 告警覆蓋。
  treat_missing_data = "notBreaching"

  alarm_actions = concat(local.alarm_actions, [aws_appautoscaling_policy.api_prewarm[0].arn])
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-sale-imminent" }
}

resource "aws_appautoscaling_policy" "api_prewarm" {
  count = var.enable_api_autoscaling ? 1 : 0

  name               = "${var.project}-api-prewarm"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.api[0].service_namespace
  resource_id        = aws_appautoscaling_target.api[0].resource_id
  scalable_dimension = aws_appautoscaling_target.api[0].scalable_dimension

  step_scaling_policy_configuration {
    # ExactCapacity 而不是 ChangeInCapacity:預熱要表達的是「站到滿」,而不是
    # 「再加兩個」。用相對調整的話,結果取決於當下有幾個任務 —— 開賣前的容量會變成
    # 一個看運氣的數字。
    adjustment_type = "ExactCapacity"
    cooldown        = 60

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 5 # = max_capacity
    }
  }
}

# ── (2) 反應式:預熱窗之外的意外流量
resource "aws_cloudwatch_metric_alarm" "api_cpu_high" {
  count = var.enable_api_autoscaling ? 1 : 0

  alarm_name        = "${var.project}-api-cpu-high-scaling"
  alarm_description = "api CPU high — scaling out (unplanned traffic outside a sale window)"

  namespace   = "AWS/ECS"
  metric_name = "CPUUtilization"
  dimensions  = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.api.name }
  statistic   = "Average"
  period      = 60

  comparison_operator = "GreaterThanThreshold"
  threshold           = 70 # 低於 monitoring.tf 那條 80% 的**告警**門檻:先擴容,擴不動才叫人
  evaluation_periods  = 1

  treat_missing_data = "notBreaching"

  alarm_actions = [aws_appautoscaling_policy.api_out[0].arn]
  tags          = { Name = "${var.project}-api-cpu-high-scaling" }
}

resource "aws_appautoscaling_policy" "api_out" {
  count = var.enable_api_autoscaling ? 1 : 0

  name               = "${var.project}-api-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.api[0].service_namespace
  resource_id        = aws_appautoscaling_target.api[0].resource_id
  scalable_dimension = aws_appautoscaling_target.api[0].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_lower_bound = 0  # CPU 70~85%
      metric_interval_upper_bound = 15 #
      scaling_adjustment          = 1
    }
    step_adjustment {
      metric_interval_lower_bound = 15 # 85% 以上 —— 已經在掉請求了,一次加滿
      scaling_adjustment          = 3
    }
  }
}

# ── (3) 縮容:慢,而且開賣窗內完全不動
resource "aws_cloudwatch_metric_alarm" "api_low_load" {
  count = var.enable_api_autoscaling ? 1 : 0

  alarm_name        = "${var.project}-api-low-load"
  alarm_description = "api idle for a sustained period AND no sale window open — scaling in"

  comparison_operator = "LessThanThreshold"
  threshold           = 30

  # 縮回去要比擴出去**慢得多**(跟 consumer 同一個原則):擴錯的代價是幾分錢,縮錯的
  # 代價是在尖峰中間把容量拿掉。10 個週期 = 連續 10 分鐘真的沒事了。
  evaluation_periods = 10

  # **metric math 是這條告警的重點。** 預熱期間 CPU 本來就是低的(人還沒到),所以
  # 一條單純看 CPU 的縮容告警會在開賣前 10 分鐘把剛預熱好的容量收回去 —— 預熱因此
  # 完全失效,而且失效得毫無痕跡。這裡讓「開賣窗開著」時回報一個假的 100%,縮容在
  # 窗內就結構性地不可能發生。
  #
  # FILL(...,0) 是必要的:sale_imminent 只在 worker 活著時有資料點,而缺資料會讓整個
  # 運算式變成沒有資料 —— 那時縮容不是「不動」,是**永遠不動**(容量永遠下不來)。
  metric_query {
    id          = "effective_cpu"
    expression  = "IF(FILL(imminent, 0) > 0, 100, cpu)"
    label       = "api CPU, forced to 100% while a sale window is open"
    return_data = true
  }

  metric_query {
    id = "cpu"
    metric {
      namespace   = "AWS/ECS"
      metric_name = "CPUUtilization"
      dimensions  = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.api.name }
      period      = 60
      stat        = "Average"
    }
  }

  metric_query {
    id = "imminent"
    metric {
      namespace   = local.log_metric_namespace
      metric_name = "sale_imminent"
      period      = 60
      stat        = "Maximum"
    }
  }

  alarm_actions = [aws_appautoscaling_policy.api_in[0].arn]
  tags          = { Name = "${var.project}-api-low-load" }
}

resource "aws_appautoscaling_policy" "api_in" {
  count = var.enable_api_autoscaling ? 1 : 0

  name               = "${var.project}-api-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.api[0].service_namespace
  resource_id        = aws_appautoscaling_target.api[0].resource_id
  scalable_dimension = aws_appautoscaling_target.api[0].scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    # 縮容的冷卻是擴容的 5 倍。一個被 SIGTERM 的 api 任務身上可能還掛著幾百條 SSE
    # 連線(等候室),它們會全部重連到別的任務上 —— 那是一波自己造成的尖峰,所以
    # 兩次縮容之間要留足夠時間讓它平息。
    cooldown                = 300
    metric_aggregation_type = "Average"

    step_adjustment {
      metric_interval_upper_bound = 0 # 一次只縮一個,慢慢退
      scaling_adjustment          = -1
    }
  }
}
