# --- Application Load Balancer (public entry point) --------------------------
# HTTP only for now (no domain/cert). HTTPS is a later add: ACM cert + Route 53
# DNS validation + a 443 listener — needs a domain you control.

resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id] # public 80/443 in
  subnets            = aws_subnet.public[*].id      # ALB spans the 2 public subnets
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

  health_check {
    path                = "/"       # "/" 307-redirects to /docs...
    matcher             = "200-399" # ...so accept redirects as healthy
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
