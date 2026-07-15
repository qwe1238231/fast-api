# --- Security groups: the who-can-talk-to-whom firewall ----------------------
# Defined up front as the network contract; the RDS/Redis/ECS resources in later
# phases attach to these. Rules reference each other by SG id (not by IP), so
# "only the app can reach the DB" holds no matter what the tasks' IPs are.
#
# Data-tier SGs (db, redis) have NO egress rule on purpose — they only ever
# respond to inbound, never initiate outbound.

# ALB — public web ingress.
resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "ALB: public HTTP/HTTPS in"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-alb" }
}

# App tasks (API; the worker/consumer tasks can share this SG — they just don't
# use the inbound rule). Only the ALB may reach the app port. Egress open so the
# app can reach RDS, Redis, ECR and Stripe.
resource "aws_security_group" "app" {
  name        = "${var.project}-app"
  description = "App tasks: ingress only from ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "app port from ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-app" }
}

# RDS Postgres — only the app may reach 5432.
resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS Postgres: ingress only from app"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "postgres from app only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  ingress {
    description = "dev: postgres from admin laptop (for alembic) - remove for prod"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }
  tags = { Name = "${var.project}-db" }
}

# ElastiCache Redis — only the app may reach 6379.
resource "aws_security_group" "redis" {
  name        = "${var.project}-redis"
  description = "ElastiCache Redis: ingress only from app"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "redis from app only"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  tags = { Name = "${var.project}-redis" }
}
