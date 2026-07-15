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

resource "aws_elasticache_cluster" "main" {
  cluster_id           = "${var.project}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  tags = { Name = "${var.project}-redis" }
}
