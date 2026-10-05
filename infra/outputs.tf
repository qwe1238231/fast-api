# Printed after `terraform apply` — the repo URL you push the image to.
output "ecr_repository_url" {
  description = "Docker push target, e.g. 992382571445.dkr.ecr.ap-northeast-2.amazonaws.com/justin-test"
  value       = data.aws_ecr_repository.app.repository_url
}

# --- Network (Phase C) — referenced by RDS/ElastiCache/ECS in later phases ---
output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "For the ALB and (low-cost) the Fargate tasks."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "For RDS + ElastiCache subnet groups."
  value       = aws_subnet.private[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "db_security_group_id" {
  value = aws_security_group.db.id
}

output "redis_security_group_id" {
  value = aws_security_group.redis.id
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

# --- Data stores (Phase D) — feed the app's env vars ------------------------
output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "database_url" {
  description = "Run `terraform output -raw database_url` to get it for alembic / the app env."
  sensitive   = true
  value       = "postgresql+asyncpg://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}"
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "redis_url" {
  value = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}

# --- Public entry point (Phase E) -------------------------------------------
output "alb_url" {
  description = "Hit the app here once the API service is healthy."
  value       = "http://${aws_lb.main.dns_name}"
}
