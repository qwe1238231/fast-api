# --- RDS Postgres (dev) ------------------------------------------------------
# COST: db.t4g.micro ~$12-15/mo if left up. skip_final_snapshot + no multi-AZ +
# no backups keep it cheap and quick to destroy. DESTROY IT at session end.
#
# dev connectivity: publicly_accessible + placed in PUBLIC subnets + the db SG
# opens 5432 to your laptop IP (security.tf) — so you can run alembic from the
# laptop. Production would flip this to private + migrate via an in-VPC task.

# Master password: generated, kept in local tfstate (gitignored). Phase F moves
# secrets into Secrets Manager. `special = false` avoids URL-encoding pain in
# the DATABASE_URL.
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.public[*].id # public because publicly_accessible (dev)
  tags       = { Name = "${var.project}-db" }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"

  db_name  = "ticketdb"
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true # dev only — see header

  multi_az                = false
  backup_retention_period = 0    # dev: no backups (cheap, clean destroy)
  skip_final_snapshot     = true # don't block destroy on a final snapshot
  apply_immediately       = true

  tags = { Name = "${var.project}-db" }
}
