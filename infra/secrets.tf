# --- App secrets (Secrets Manager) -------------------------------------------
# One JSON secret holding everything sensitive the containers need. ECS injects
# each key as an env var at task start (see the task defs' `secrets` field), so
# the values never appear in the task definition or the ECS console.
#
# recovery_window_in_days = 0 → on destroy the secret is deleted immediately
# (default is a 7-30 day recovery hold, which would block re-apply with the same
# name — annoying in the apply→destroy dev loop).

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.project}/app"
  recovery_window_in_days = 0
  tags                    = { Name = "${var.project}-app" }
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    SECRET_KEY            = var.app_secret_key
    PII_KEK_BASE64        = var.pii_kek_base64
    PII_LOOKUP_KEY_BASE64 = var.pii_lookup_key_base64
    STRIPE_SECRET_KEY     = var.stripe_secret_key
    STRIPE_WEBHOOK_SECRET = var.stripe_webhook_secret
    # Built from the RDS resource — includes the generated master password.
    DATABASE_URL = "postgresql+asyncpg://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}"
  })
}

# The EXECUTION role reads the secret at task start (not the task role — the
# injection is done by the ECS agent, not your app code).
data "aws_iam_policy_document" "read_app_secret" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "${var.project}-read-app-secret"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.read_app_secret.json
}
