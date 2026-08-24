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
  #
  # 代價要講清楚:這支檢查通過**只**代表 process 活著、port 開著、event loop 沒被
  # 卡死、而且還排得出一個 worker 來回答。它不代表這台機器下得了單。
  #
  # 「活著但服務不了」由 HTTPCode_Target_5XX_Count 告警偵測(monitoring.tf),但要
  # 講清楚偵測到之後會發生什麼:**不會通知任何人**。這個 stack 沒有通知通道
  # (monitoring.tf 的 local.alarm_actions = [],刻意的 —— 不收告警信),所以那個
  # 告警只會在 CloudWatch console 裡變紅,等人去看。這是一個明確的取捨,不是遺漏:
  # 這裡的偵測是自動的,回應是人工的。哪天想讓它自己叫,唯一要動的是那個 local。
  #
  # 深度檢查在 /health/deps,由 deploy.yml 的部署後煙霧測試呼叫 —— 那條路徑是自動的,
  # 而且它擋的是「這次部署把服務推壞了」,跟上面那個「跑一跑才壞」是不同的失效。
  #
  # 時間參數的判準是「一個死掉的 target 會吃掉多久的流量」= interval ×
  # unhealthy_threshold。原本 30 × 3 = 90 秒;搶票的尖峰常常撐不到 90 秒,那等於
  # 整段開賣有一台在丟請求。10 × 2 = 20 秒,代價只是健康檢查請求變三倍 —— 而
  # /health 不碰任何依賴,那個成本可以忽略。healthy_threshold 同樣是 2,新任務 20 秒
  # 就進池,滾動部署跟著快。timeout 必須小於 interval(5 < 10)。
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  # 明確寫出來,因為預設值(300 秒)跟這個 app 的 SSE 剛好撞在一起。
  #
  # 先分清楚:deregistration_delay 管的是「**刻意**下線一個 target 時(部署、縮容)
  # 等多久讓還在飛的請求做完」,跟上面的失敗偵測是兩回事 —— target 一旦被判
  # unhealthy,ALB 立刻停送新請求,不會等這個延遲。
  #
  # /v1/events/{id}/queue/stream 是 SSE,單條連線上限 300 秒(_SSE_MAX_SECONDS),
  # 正好等於預設值:預設會讓每次部署為了排空長連線等滿五分鐘。60 秒的取捨是部署
  # 快得多,代價是活超過 60 秒的 SSE 會被切斷 —— 那是安全的:瀏覽器的 EventSource
  # 會自動重連,而重連後的第一個 frame 就是 queue_status 的權威狀態,排隊位置與入場
  # 判定都不靠連線本身保存(admission 是時間的純函數)。
  deregistration_delay = 60

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
