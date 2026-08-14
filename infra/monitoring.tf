# --- Monitoring: alarm notification target ----------------------------------
# SNS was removed. The alarms below still EVALUATE state (visible in the
# CloudWatch console); they just push no notifications. Every alarm sets
# alarm_actions/ok_actions = local.alarm_actions, so re-enabling later is a
# one-line change here: create an aws_sns_topic (+ subscription) and point the
# local at [aws_sns_topic.<your-topic>.arn].
locals {
  alarm_actions = [] # empty = notify nobody; set to a topic ARN list to re-enable
}

# --- ECS: per-service CPU / memory utilization ------------------------------
# AWS/ECS CPUUtilization & MemoryUtilization are a % OF THE TASK'S RESERVATION
# (256 CPU units = 0.25 vCPU, 512 MB), NOT of a host — so 80/85 are directly
# meaningful on these tiny tasks. NOTE: none of these services autoscale
# (all desired_count=1), so a sustained-high alarm is an EARLY-WARNING /
# capacity-planning signal ("this box is hot, resize it or shed load"), not a
# scale-out trigger. 6 near-identical alarms => one for_each over a map.
locals {
  ecs_util_alarms = {
    api_cpu         = { service = aws_ecs_service.api.name, metric = "CPUUtilization", threshold = 80 }
    api_memory      = { service = aws_ecs_service.api.name, metric = "MemoryUtilization", threshold = 85 }
    consumer_cpu    = { service = aws_ecs_service.consumer.name, metric = "CPUUtilization", threshold = 80 }
    consumer_memory = { service = aws_ecs_service.consumer.name, metric = "MemoryUtilization", threshold = 85 }
    worker_cpu      = { service = aws_ecs_service.worker.name, metric = "CPUUtilization", threshold = 80 }
    worker_memory   = { service = aws_ecs_service.worker.name, metric = "MemoryUtilization", threshold = 85 }
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_util" {
  for_each = local.ecs_util_alarms

  alarm_name          = "${var.project}-${replace(each.key, "_", "-")}-high"
  alarm_description   = "ECS ${each.value.service} ${each.value.metric} > ${each.value.threshold}% of task reservation for 15 min"
  namespace           = "AWS/ECS"
  metric_name         = each.value.metric
  statistic           = "Average" # service metric already aggregates tasks; Average ignores per-request spikes
  period              = 300       # free standard 5-min metrics
  evaluation_periods  = 3         # 3×5min = 15 min sustained → rides deploy churn & single bursts
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching" # a metric gap during a deploy shouldn't page

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value.service
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-${replace(each.key, "_", "-")}-high" }
}

# --- ECS: task-count alarms (need Container Insights, enabled on the cluster) ---
# RunningTaskCount lives ONLY in the ECS/ContainerInsights namespace. The
# utilization alarms above go BLIND when a service is fully down: no task => no
# CPU/mem datapoints => notBreaching => silence. These catch "service is down"
# and "worker singleton violated".
locals {
  ecs_services = {
    api      = aws_ecs_service.api.name
    consumer = aws_ecs_service.consumer.name
    worker   = aws_ecs_service.worker.name
  }
}

# "< 1 running task for 5 min" = the service is down. treat_missing_data =
# breaching is deliberate: if the metric stops publishing ENTIRELY, that absence
# is itself the failure and must page. (A service that exists but has 0 tasks
# still publishes RunningTaskCount=0, which trips this on the datapoint.)
# ⚠️ FIRST-APPLY: Container Insights takes a few minutes to start publishing, so
# right after apply these sit in "breaching" and email once, then auto-resolve.
resource "aws_cloudwatch_metric_alarm" "ecs_service_down" {
  for_each = local.ecs_services

  alarm_name          = "${var.project}-${each.key}-no-running-tasks"
  alarm_description   = "ECS ${each.value} has < 1 running task for 5 min — service is down"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-${each.key}-no-running-tasks" }
}

# "> 1 running task" = the worker singleton is violated → ARQ crons double-fire
# (double seat release → oversell). The services.tf deploy config (max 100%)
# prevents the common cause (deploy overlap); this is the BACKSTOP for edge
# cases (e.g. a task stuck terminating). notBreaching: absence ≠ duplication —
# the down alarm above owns the absence case.
resource "aws_cloudwatch_metric_alarm" "worker_duplicate_tasks" {
  alarm_name          = "${var.project}-worker-duplicate-tasks"
  alarm_description   = "worker running > 1 task — singleton violated, crons may double-fire (oversell risk)"
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  statistic           = "Maximum" # catch the peak; even a brief 2 is dangerous
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.worker.name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-worker-duplicate-tasks" }
}

# --- RDS Postgres (db.t4g.micro: 2 vCPU burstable, ~1GiB RAM, 20GB gp3) ------
# Thresholds are sized to THIS tiny instance. Two units traps to remember:
# FreeableMemory/FreeStorageSpace are in BYTES ("low = bad"); latency is in
# SECONDS. All AWS/RDS, all keyed on DBInstanceIdentifier, all publish
# continuously (DB is always up) so treat_missing_data is notBreaching for all.
locals {
  rds_alarms = {
    cpu_high = {
      metric    = "CPUUtilization", stat = "Average", op = "GreaterThanThreshold",
      threshold = 80, period = 300, eval = 3,
      desc      = "CPU > 80% for 15m — DB is the bottleneck (only 2 vCPU)"
    }
    # The classic SILENT t4g failure: credits drain → throttled to ~10% baseline
    # → queries crawl while CPUUtilization looks fine (it's capped). Early warning.
    cpu_credit_low = {
      metric    = "CPUCreditBalance", stat = "Average", op = "LessThanThreshold",
      threshold = 30, period = 300, eval = 2,
      desc      = "burstable CPU credits < 30 — throttle to baseline imminent (enable T-Unlimited for spiky load)"
    }
    # max_connections ≈ LEAST(mem/9531392, 5000) ≈ 100-110 on 1GiB. Past it =
    # 'too many connections' 5xx cliff. Maximum: a momentary peak already rejects.
    connections_high = {
      metric    = "DatabaseConnections", stat = "Maximum", op = "GreaterThanThreshold",
      threshold = 80, period = 60, eval = 5,
      desc      = "connections > 80 — nearing the ~100-110 ceiling"
    }
    # 157286400 = 150 MiB. BYTES, not %. Below this the 1GiB box starts swapping.
    freeable_memory_low = {
      metric    = "FreeableMemory", stat = "Average", op = "LessThanThreshold",
      threshold = 157286400, period = 60, eval = 5,
      desc      = "freeable memory < 150MiB — swap/OOM risk"
    }
    # 2147483648 = 2 GiB. Disk does NOT autoscale (no max_allocated_storage), so
    # 0 = read-only outage. eval=1: storage fills monotonically, one low sample is real.
    free_storage_low = {
      metric    = "FreeStorageSpace", stat = "Average", op = "LessThanThreshold",
      threshold = 2147483648, period = 300, eval = 1,
      desc      = "free storage < 2GiB — 20GB disk near full; DB goes READ-ONLY at 0"
    }
    # 0.05 = 50ms. SECONDS. Storage I/O bottleneck / working set no longer in RAM.
    read_latency_high = {
      metric    = "ReadLatency", stat = "Average", op = "GreaterThanThreshold",
      threshold = 0.05, period = 300, eval = 3,
      desc      = "read latency > 50ms for 15m — I/O bound / cache too small"
    }
    # Write path = the order INSERT hot path. Slow writes → consumer backs up →
    # Redis stream backlog grows (ties to the layer-3 pipeline alarms).
    write_latency_high = {
      metric    = "WriteLatency", stat = "Average", op = "GreaterThanThreshold",
      threshold = 0.05, period = 300, eval = 3,
      desc      = "write latency > 50ms for 15m — WAL/checkpoint/IOPS saturated; order INSERTs stall"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "rds" {
  for_each = local.rds_alarms

  alarm_name          = "${var.project}-rds-${replace(each.key, "_", "-")}"
  alarm_description   = "RDS ${aws_db_instance.main.identifier}: ${each.value.desc}"
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  statistic           = each.value.stat
  period              = each.value.period
  evaluation_periods  = each.value.eval
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-rds-${replace(each.key, "_", "-")}" }
}

# --- ElastiCache Redis (single node cache.t4g.micro, ~0.5GiB) ---------------
# This Redis is NOT a cache — it holds the order stream, inventory reserves, and
# waiting-room state, so memory pressure = correctness risk, not a cache miss.
# The memory tripwire CHAIN (the real backstop, fires regardless of eviction
# policy): DatabaseMemoryUsagePercentage>75 (early) → FreeableMemory<50MiB /
# SwapUsage>50MiB (host OOM). All AWS/ElastiCache, keyed on CacheClusterId.
# NOTE on evictions_present: it is NOT a universal data-loss backstop — see the
# teaching note. default.redis7's policy is volatile-lru (only evicts TTL keys),
# and under noeviction Evictions is 0 by definition, so this counter can stay
# silent during real write-rejection. The memory-% chain is the true backstop.
# We use EngineCPUUtilization (the single busy thread), NOT CPUUtilization
# (diluted across 2 vCPUs → reads ~50% while the engine is already pegged).
locals {
  redis_alarms = {
    engine_cpu_high = {
      metric    = "EngineCPUUtilization", stat = "Average", op = "GreaterThanThreshold",
      threshold = 90, period = 60, eval = 5,
      desc      = "single-threaded engine CPU > 90% for 5m — command latency climbing"
    }
    memory_usage_high = {
      metric    = "DatabaseMemoryUsagePercentage", stat = "Average", op = "GreaterThanThreshold",
      threshold = 75, period = 60, eval = 5,
      desc      = "memory > 75% of maxmemory — approaching eviction/OOM (leading indicator)"
    }
    # Sum (count-per-minute), threshold 0, eval 1: ANY eviction is an incident.
    evictions_present = {
      metric    = "Evictions", stat = "Sum", op = "GreaterThanThreshold",
      threshold = 0, period = 60, eval = 1,
      desc      = "ANY eviction — dropping stream/reserve/waiting-room keys (NOT a universal backstop; see memory-% chain)"
    }
    connections_high = {
      metric    = "CurrConnections", stat = "Average", op = "GreaterThanThreshold",
      threshold = 500, period = 60, eval = 5,
      desc      = "connections > 500 for 5m — likely a pool/connection leak (tune down after a real sale)"
    }
    swap_high = {
      metric    = "SwapUsage", stat = "Average", op = "GreaterThanThreshold",
      threshold = 52428800, period = 60, eval = 5,
      desc      = "swap > 50MiB — host out of RAM, in-memory latency guarantees broken"
    }
    freeable_memory_low = {
      metric    = "FreeableMemory", stat = "Average", op = "LessThanThreshold",
      threshold = 52428800, period = 60, eval = 5,
      desc      = "freeable host RAM < 50MiB — OOM/swap imminent"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "redis" {
  for_each = local.redis_alarms

  alarm_name          = "${var.project}-redis-${replace(each.key, "_", "-")}"
  alarm_description   = "Redis ${aws_elasticache_cluster.main.cluster_id}: ${each.value.desc}"
  namespace           = "AWS/ElastiCache"
  metric_name         = each.value.metric
  statistic           = each.value.stat
  period              = each.value.period
  evaluation_periods  = each.value.eval
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    CacheClusterId = aws_elasticache_cluster.main.cluster_id
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-redis-${replace(each.key, "_", "-")}" }
}

# --- ALB (aws_lb.main) + target group (aws_lb_target_group.api) -------------
# All AWS/ApplicationELB (emitted natively, no Container Insights). The CloudWatch
# dimension for a load balancer is its .arn_suffix (app/<name>/<id>), NOT .arn —
# passing .arn silently matches nothing. Written as explicit blocks (not for_each)
# so the two gotchas stay visible: the p95 extended_statistic, and the
# BOTH-dimensions rule on UnHealthyHostCount. treat_missing_data=notBreaching on
# all four — a healthy LB emits no datapoints for these, so "missing" is normal.

# (1) LB-level 5xx — failures the LB generates itself (502/503/504: no healthy
# target, backend reset, idle timeout). The client sees an error your app never
# logged. LoadBalancer dimension ONLY (this metric is LB-wide).
resource "aws_cloudwatch_metric_alarm" "alb_elb_5xx_high" {
  alarm_name          = "${var.project}-alb-elb-5xx-high"
  alarm_description   = "ALB > 5 LB-level 5xx/min for 2 min — no healthy targets or backend connection failures"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  statistic           = "Sum" # count metric → total per window, not an average
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"

  dimensions = { LoadBalancer = aws_lb.main.arn_suffix }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-alb-elb-5xx-high" }
}

# (2) Target 5xx — 5xx your APP produced (unhandled exception, DB/Redis error).
# Same symptom as (1), totally different root cause → separate alarm. Scoped to
# the one target group. More patient (eval 3) since app 5xx are noisier.
resource "aws_cloudwatch_metric_alarm" "alb_target_5xx_high" {
  alarm_name          = "${var.project}-alb-target-5xx-high"
  alarm_description   = "App returned > 10 5xx/min for 3 min — unhandled exceptions or failing downstream (RDS/ElastiCache)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-alb-target-5xx-high" }
}

# (3) p95 latency — the early-warning before slow turns into timeouts/5xx.
# GOTCHA: percentiles use extended_statistic, and you must NOT also set
# statistic (both in one alarm = a plan error). TargetResponseTime is in SECONDS
# → 1.5 = 1.5s. p95 (not p99, which swings wildly at low request volume).
resource "aws_cloudwatch_metric_alarm" "alb_target_p95_latency_high" {
  alarm_name          = "${var.project}-alb-target-p95-latency-high"
  alarm_description   = "App p95 response time > 1.5s for 5 min — saturation/slow downstream; early warning before 5xx"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95" # NOT statistic — setting both is a validate error
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 1.5
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-alb-target-p95-latency-high" }
}

# (4) Unhealthy targets — the most direct "is my app actually up". THE #1 ALB
# alarm mistake: UnHealthyHostCount is published per target group, so it exists
# ONLY at the LoadBalancer∩TargetGroup intersection — with one dimension it
# matches no data and sits in INSUFFICIENT_DATA forever. Maximum (worst point in
# the minute), threshold 0 (one unhealthy host = your whole capacity on this
# tiny stack). eval 3 rides a normal rolling deploy (~90s to flip unhealthy).
resource "aws_cloudwatch_metric_alarm" "alb_unhealthy_hosts" {
  alarm_name          = "${var.project}-alb-unhealthy-hosts"
  alarm_description   = "≥ 1 unhealthy target for 3 min — task crashing, unresponsive, or failing health checks"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-alb-unhealthy-hosts" }
}

# =============================================================================
# LAYER 3 — pipeline / meta alarms (what the stock resource metrics can't see)
# =============================================================================

# (A) [removed] SNS delivery-failed meta-alarm — dropped together with the SNS
# channel. It only made sense while notifications went to an SNS topic.

# (B) Pipeline alarms via LOG METRIC FILTERS. These failure modes have NO stock
# CloudWatch metric — but the worker already prints a distinct line for each. A
# log_metric_filter turns "this phrase appeared in the logs" into a metric we can
# alarm on. Two-step: (1) filter counts matches on the shared /ecs log group,
# (2) an alarm fires on Sum > 0.
# KEY GOTCHA: default_value = "0" on the transformation makes the metric emit 0
# for non-matching log events, so it's CONTINUOUS and the alarm returns to OK
# cleanly. Without it the metric only publishes on a match and the alarm can't
# resolve. Patterns match lines the code prints today (verified in app/worker.py).
# (backlog + dead-letter depth are covered by the numeric gauges in section C
# below — richer than a log-line boolean — so only these two live here:)
#   line 341  "CIRCUIT-BREAKER admission paused"  (cron, every min while paused)
#   line 362  "INVENTORY DRIFT event"             (cron, every 5 min per drift)
locals {
  log_metric_namespace = "${var.project}/pipeline"
  pipeline_log_alarms = {
    admission_paused = {
      pattern = "\"CIRCUIT-BREAKER admission paused\""
      period  = 300
      desc    = "circuit breaker tripped: waiting room stopped admitting buyers (business outage, looks 'up')"
    }
    inventory_drift = {
      pattern = "\"INVENTORY DRIFT event\""
      period  = 600 # drift cron runs every 5 min; 10-min window always catches a persistent drift
      desc    = "Redis vs Postgres stock disagree — oversell / phantom sold-out risk"
    }
    # 限購額度的漂移**比庫存漂移更難發現**:超賣會撞到總量、等候室的 sold_out 會叫;
    # 超買不會撞到任何東西 —— 那個人就是多買了幾張,而所有計數器都自洽。這條 log
    # 是唯一會看到它的地方,所以它必須有自己的告警,不能只靠 needs_a_human 那條總開關。
    quota_drift = {
      pattern = "\"QUOTA DRIFT event\""
      period  = 600
      desc    = "per-user purchase quotas in Redis disagree with Postgres — someone can exceed the cap, and nothing else will notice"
    }
    # 「有人要來看一眼」的總開關。ALERT 與 REFUND 是程式碼裡兩個刻意保留的字首,標的是
    # 沒有任何自動修復路徑的事:
    #   ALERT allocator bug             — 配位算出界外區間 → 使用者吃 500,是我們的 bug
    #   ALERT dead-letter intent ...    — 死信欄位自相矛盾,座位還不回去
    #   ALERT order N expired but ...   — 座位釋放失敗,要人工跑 rebuild_seat_runs
    #   REFUND payment_intent ...       — 真的有錢退出去了
    # 這些各自都很罕見,罕見到不值得一個一個做告警;但「其中任何一個發生了」是絕對
    # 要知道的。用一條 OR 的過濾器把它們全部收進來,總比讓每一條新的 ALERT 都靜靜
    # 躺在 log 裡等人去 grep 好。
    needs_a_human = {
      pattern = "?\"ALERT \" ?\"REFUND \""
      period  = 300
      desc    = "an ALERT/REFUND line was logged — no automatic remedy exists for these; read the log group"
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "pipeline" {
  for_each       = local.pipeline_log_alarms
  name           = "${var.project}-${replace(each.key, "_", "-")}"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = each.value.pattern

  metric_transformation {
    name          = each.key
    namespace     = local.log_metric_namespace
    value         = "1"
    default_value = "0" # emit 0 on non-matching events → metric is continuous, alarm resolves cleanly
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_log" {
  for_each = local.pipeline_log_alarms

  alarm_name          = "${var.project}-${replace(each.key, "_", "-")}"
  alarm_description   = each.value.desc
  namespace           = local.log_metric_namespace
  metric_name         = each.key # matches the filter's metric_transformation.name
  statistic           = "Sum"
  period              = each.value.period
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching" # worker silent (down) → covered by ecs_service_down

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-${replace(each.key, "_", "-")}" }
}

# (C) Numeric depth gauges. The worker's report_queue_depth cron already computes
# backlog + dead_letter every minute; the app publishes them via PutMetricData to
# the ${var.project}/pipeline namespace (IAM: aws_iam_role_policy.ecs_task_metrics;
# AWS_REGION injected into the worker task def). Unlike the log-line booleans these
# are real numbers — trendable, and the threshold lives HERE in CloudWatch, not
# baked into the app.
# ⚠️ These sit in INSUFFICIENT_DATA until the app change ships (harmless — no data
# yet). Published once/min → period 60.
locals {
  pipeline_gauge_alarms = {
    order_stream_backlog = {
      threshold = 1000, eval = 5,
      desc      = "order stream backlog > 1000 for 5 min — consumer not keeping up; 202'd orders not persisting"
    }
    # 掛在「每分鐘新增」而不是累計深度。累計值只增不減(沒有人會去清死信),門檻 0
    # 的告警一旦響過就永遠在 ALARM —— 而一個永遠在響的告警等於沒有告警。用新增值
    # 才會在事故結束後自己 OK,下次再響時是真的又出事了。
    order_dead_letter_new = {
      threshold = 0, eval = 1,
      desc      = "new order dead-letters in the last minute — orders permanently failing right now"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_gauge" {
  for_each = local.pipeline_gauge_alarms

  alarm_name          = "${var.project}-${replace(each.key, "_", "-")}"
  alarm_description   = each.value.desc
  namespace           = local.log_metric_namespace
  metric_name         = each.key
  statistic           = "Average" # gauge sampled once/min → Average == the sample
  period              = 60
  evaluation_periods  = each.value.eval
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching" # no data until the app publishes; worker-down covered elsewhere

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = { Name = "${var.project}-${replace(each.key, "_", "-")}" }
}
