# --- ElastiCache Redis (dev, single node) ------------------------------------
# COST: cache.t4g.micro ~$12/mo if left up. DESTROY at session end.
#
# Single node = NO high availability (if the node dies, Redis is down). That
# leaves the 搶票 "Redis single point of failure" gap OPEN — to close it, swap
# this aws_elasticache_cluster for an aws_elasticache_replication_group with a
# replica + automatic_failover + multi_az_enabled (~2x cost). Deferred by choice.
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

resource "aws_elasticache_cluster" "main" {
  cluster_id           = "${var.project}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_nodes      = 1
  parameter_group_name = aws_elasticache_parameter_group.main.name
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  tags = { Name = "${var.project}-redis" }
}
