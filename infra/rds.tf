# --- RDS Postgres -------------------------------------------------------------
# COST: db.t4g.micro Multi-AZ ~$25-30/mo if left up (單 AZ 的兩倍)。備份本身幾乎免費
# (保留期內的自動備份不另計費,只有超過資料量的部分算錢)。DESTROY IT at session end
# —— 而 destroy 現在**會留下一張最終快照**,所以毀掉環境不等於毀掉資料。
#
# **這是全系統唯一的權威儲存。** Redis 的庫存、限購額度、座位空段全部可以從這裡重建
# (worker 的 detect_inventory_drift 會自動做),所以這裡的耐久性是整條復原論證的地板。
# 之前這個地板是:沒有備份、沒有 multi-AZ、destroy 不留快照 —— 也就是任何一次誤刪、
# 壞掉的 migration、或 AZ 故障都是**永久**資料遺失。
#
# dev connectivity: publicly_accessible + placed in PUBLIC subnets + the db SG
# opens 5432 to your laptop IP (security.tf) — so you can run alembic from the
# laptop. Production would flip this to private + migrate via an in-VPC task.
# (**這一項還沒改**,見 infra/RUNBOOK.md 的「還沒做的事」。)

# Master password: generated, kept in local tfstate (gitignored). Phase F moves
# secrets into Secrets Manager. `special = false` avoids URL-encoding pain in
# the DATABASE_URL.
resource "random_password" "db" {
  length  = 24
  special = false
}

# 最終快照的名字必須**每次都不一樣**:快照在 destroy 之後留著,而同名快照已存在時
# 下一次 destroy 會直接失敗(而且是在「已經開始拆」的中途失敗)。random_id 存在
# tfstate 裡,所以同一個環境的生命週期內名字穩定;destroy 連帶清掉 state,下一次
# apply 就換一個新的 —— 剛好符合「一次 session 一個環境」的用法。
resource "random_id" "db_final_snapshot" {
  byte_length = 4
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

  # **靜態加密。** 開在建立時是免費的;之後要開就得「快照 → 用加密還原 → 換端點」,
  # 沒有原地切換的辦法。所以這是一個「現在不開,以後就很貴」的選項。
  #
  # PII 那一層(pii.py 的 envelope encryption)保護的是身分證號那幾個欄位;其餘所有
  # 東西 —— 姓名、訂單、稽核紀錄 —— 在磁碟上都是明文。加密也順帶讓**快照**是加密的,
  # 而快照是最容易被複製到別處的東西。
  storage_encrypted = true

  db_name  = "ticketdb"
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true # dev only — see header

  # ── 可用性
  # Multi-AZ:同步複寫到另一個 AZ 的待命實例,故障時自動切換(分鐘級,端點名稱不變)。
  # 附帶好處:**快照與備份改由待命實例產生**,所以備份不再對正在服務的實例造成 I/O
  # 暫停 —— 這讓「部署前拍一張快照」在開賣期間也是安全的。
  #
  # 走變數是為了**還原時可以先關掉**:實測 Multi-AZ 轉換佔還原 apply 的 74%(20m50s),
  # 而它跟把資料弄回來無關。理由與數字見 variables.tf 的 `db_multi_az`。
  multi_az = var.db_multi_az

  # ── 可回溯(rollback)
  # 保留期 > 0 就打開了 PITR:可以還原到保留期內**任何一秒**(RDS 每 5 分鐘備份一次
  # 交易日誌)。這是「壞掉的 migration 跑完了才發現」唯一的解法 —— schema 可以用
  # alembic downgrade 退回去,但**資料回不來**,只能靠 PITR。
  # 下限由 variables.tf 的 validation 釘死,不能設成 0。
  backup_retention_period = var.db_backup_retention_days

  # 備份與維護的時間窗:UTC 17:00-18:00 = 首爾 02:00-03:00,離開賣尖峰最遠,而且跟
  # ElastiCache 的快照窗對齊。維護排在備份之後(先有備份再動它)。
  backup_window      = "17:00-18:00"
  maintenance_window = "sun:18:30-sun:19:30"

  # 快照要帶著實例的 tag,不然一堆快照躺在帳單裡看不出是誰的。
  copy_tags_to_snapshot = true

  # 小版本升級由我們自己決定時機。維護窗只說「什麼時候可以動」,不代表我們想要它
  # 在某個週末自己升版然後行為變了。
  auto_minor_version_upgrade = false

  # **destroy 會留下一張最終快照。** 這是這個檔案裡最重要的一行:團隊的工作方式是
  # 每次 session 結束就 destroy(為了省錢),而在此之前那個動作等於「永久丟掉所有
  # 資料」。現在它變成「環境沒了,資料還在一張快照裡」,而 restore_from_snapshot_identifier
  # 就是把它拿回來的路。
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-db-final-${random_id.db_final_snapshot.hex}"

  # 誤刪保護。**預設開啟**(2026-08-17 的決定,完整理由在 variables.tf 的
  # `db_deletion_protection`)。它擋的不是手滑打 destroy —— 那個風險已經被上面的最終
  # 快照降級成「要花時間還原」;它擋的是一份寫著 `# forces replacement` 的 plan 被草率
  # 核准,因為 RDS 的重建是「先刪再建」,結果會是最終快照有拍到、而**新實例是空的**。
  #
  # 代價是 destroy 兩步(指令在 infra/README.md 的 golden rule 旁邊)。2026-08-18 演練
  # 實測那一步約 40 秒,而且它在還原流程裡**一次都沒擋到路** —— 改名不是刪除、從快照
  # 建立也不是。唯一被它擋到的是**事後清理**那台改名保留的壞實例,而那正是想要的行為:
  # 事故當下最不該發生的事,就是不小心把唯一的證據刪了。
  deletion_protection = var.db_deletion_protection

  # ── 還原(rollback 的執行面)
  # 設了這個變數,實例就**從指定的快照建立**而不是空的。這是把「還原」變成
  # Terraform 內的一級操作,而不是一串手打的 aws CLI:
  #
  #     terraform apply -var restore_from_snapshot_identifier=justin-test-db-final-a1b2c3d4
  #
  # 為什麼這樣做比手動 restore 好:手動 restore 出來的是一個 Terraform 不認識的新
  # 實例,而 DATABASE_URL 這個 secret 是 TF 從 `aws_db_instance.main.address` 推導的
  # —— 下一次 apply 會把 secret 指回舊的(已經不存在的)端點。這個專案已經在
  # task_definition / desired_count / min_capacity 上被「TF 覆蓋掉手動改動」咬過三次。
  snapshot_identifier = var.restore_from_snapshot_identifier != "" ? var.restore_from_snapshot_identifier : null

  apply_immediately = true

  lifecycle {
    # 還原完成之後就不要再管這個欄位。少了這行,下一次**不帶**那個 var 的 apply 會
    # 認為「應該從空的建立」→ 重建實例 → 剛還原回來的資料再一次消失。
    ignore_changes = [snapshot_identifier]
  }

  tags = { Name = "${var.project}-db" }
}
