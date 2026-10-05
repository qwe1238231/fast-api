# --- ElastiCache Redis (primary + replica, Multi-AZ) --------------------------
# COST: 2 × cache.t4g.micro ~$24/mo if left up. DESTROY at session end.
#
# **這個 Redis 不是快取,是庫存與訂單意圖的真相來源**(order stream、庫存計數、
# 限購額度、等候室的入場券)。所以它的可用性直接等於「能不能賣票」,而它的耐久性
# 直接等於「已經回了 202 的訂單會不會人間蒸發」。
#
# 單節點版本(這裡原本的樣子)有兩個不同的問題,不要混在一起看:
#
#   可用性 —— 節點死掉就是整站賣不了票,而且要等 AWS 換一台新的。
#             **這個由 replication group + automatic_failover + Multi-AZ 解決**,
#             故障切換是秒到分鐘級,而且不需要人。
#
#   耐久性 —— 節點死掉時,還沒被 consumer 落帳的 order intent 會跟著消失。副本
#             **不能完全**解決這個:複寫是非同步的,故障切換會丟掉最後那幾百毫秒的
#             寫入。所以副本把「一定會丟一整段」降級成「可能丟最後一瞬間」,而剩下的
#             殘餘風險由 app 那一側收尾(見 worker 的 detect_lost_redis_state 與
#             order_stream_oldest_age 指標)。
#
#   ElastiCache 沒有 AOF 可以開(新版引擎已經移除),所以「提高耐久性」在這個平台上
#   就是這條路:副本 + 自動故障切換 + 每日快照。
#
# Sharding (cluster mode) is a separate, larger change: the reserve Lua touches
# 3 keys (stock/claim/stream) that must share a hash slot, so it needs app-side
# hash tags (e.g. "{event:5}:...") before cluster mode would work.
#
# Lives in the PRIVATE subnets — only the app reaches it, never the internet.
resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project}-redis"
  subnet_ids = aws_subnet.private[*].id
}

# This Redis holds durable-ish 搶票 state (order stream, inventory reserves,
# waiting-room tokens), so on OOM we want writes to FAIL LOUDLY — the app's
# circuit breaker + admission pause are built to shed that load — instead of the
# default volatile-lru SILENTLY evicting TTL-bearing keys (a lost reserve/token
# = a correctness bug). maxmemory-policy is a dynamic parameter (no reboot).
resource "aws_elasticache_parameter_group" "main" {
  name   = "${var.project}-redis7"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }

  tags = { Name = "${var.project}-redis7" }
}

locals {
  #: primary + replica。改這個數字就要一起改告警(下面的 redis_nodes 由它推導)。
  redis_node_count = 2

  #: 成員節點的 id。**ElastiCache 的指標是掛在節點上,不是掛在 replication group 上**
  #: —— `CacheClusterId` 的值是 `<group-id>-001` / `-002`,而不是 group 本身。
  #: 這件事很容易搞錯而且錯得很安靜:拿 group id 當 dimension 的告警**永遠拿不到資料**,
  #: 而 `treat_missing_data = "notBreaching"` 會讓它永遠停在 OK。
  #:
  #: 不用 `member_clusters`(那是 known-after-apply,會讓 for_each 在 plan 階段就失敗)
  #: 而是按 AWS 的命名規則推導出來。
  redis_nodes = {
    for i in range(1, local.redis_node_count + 1) :
    format("%03d", i) => "${var.project}-redis-${format("%03d", i)}"
  }
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${var.project}-redis"
  description          = "ticket inventory + order stream (source of truth, not a cache)"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = "cache.t4g.micro"

  # 1 個 primary + 1 個 replica。cluster mode 關著(單一 shard),所以應用端不需要
  # hash tag —— reserve 的 Lua 一次碰三個 key,cluster mode 下它們必須同 slot。
  num_cache_clusters = local.redis_node_count

  # 這兩個必須**一起**開:automatic_failover 決定「primary 死了會不會自動換人」,
  # multi_az 決定「副本會不會放在另一個 AZ」。只開前者的話,一個 AZ 出問題會同時帶走
  # primary 與副本 —— 那個設定看起來有 HA,實際上沒有。
  automatic_failover_enabled = true
  multi_az_enabled           = true

  parameter_group_name = aws_elasticache_parameter_group.main.name
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  # 每日快照。它**不是**給故障切換用的(那是副本的工作),是給「有人跑錯腳本把
  # keyspace 清了」這種事用的 —— 那種情況副本會忠實地把刪除複寫過去。
  snapshot_retention_limit = 1
  snapshot_window          = "17:00-18:00" # UTC = 首爾 02:00-03:00,離開賣尖峰最遠

  # 維護窗定在同一段離峰時間。不設的話 AWS 會替我們挑一個,而它可能挑在開賣當下。
  maintenance_window = "sun:18:00-sun:19:00"

  # 版本升級由我們自己決定時機(維護窗只是「什麼時候可以動」)。搶票系統最怕的是
  # 「某個週末自己升版然後行為變了」。
  auto_minor_version_upgrade = false

  # **應用端連 primary_endpoint_address**(見 taskdefs.tf)。故障切換之後那個 DNS
  # 名稱會指向新的 primary,所以應用不必知道發生了什麼 —— 但它會**斷線一次**,
  # 而 redis-py 會自動重連。
  tags = { Name = "${var.project}-redis" }
}
