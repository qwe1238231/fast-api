# --- Application Load Balancer (public entry point) --------------------------
# HTTP only for now (no domain/cert). HTTPS is a later add: ACM cert + Route 53
# DNS validation + a 443 listener — needs a domain you control.

resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id] # public 80/443 in
  subnets            = aws_subnet.public[*].id     # ALB spans the 2 public subnets
  tags               = { Name = "${var.project}-alb" }
}

# Target group the API tasks register into. Fargate uses awsvpc networking, so
# targets are IPs (target_type = "ip"), not instance ids. The ECS service keeps
# this group's membership in sync as tasks come and go.
resource "aws_lb_target_group" "api" {
  name        = "${var.project}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  # 專屬的存活端點,matcher 收緊成 200。舊設定是 path="/" + matcher="200-399" ——
  # "/" 回 307 轉去 /docs,所以「健康」的實際判準是「那個轉址還在」。那不是契約而是
  # 巧合:有人改掉根路徑,健康檢查的語意就默默跟著變。
  #
  # /health **刻意不碰 DB 與 Redis**(理由寫在 app/main.py 的 health()):DB 是共用的,
  # 深度檢查會在資料庫一抖時讓所有 target 同時 unhealthy —— ALB 沒有後端可送(完全
  # 中斷),而 ECS 把所有任務殺掉重啟,重啟再去捶正在恢復的資料庫。
  # 「活著但服務不了」由 HTTPCode_Target_5XX_Count 告警負責;深度檢查在 /health/deps,
  # 由 deploy.yml 的部署後煙霧測試呼叫。
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "${var.project}-api" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
