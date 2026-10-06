# infra/ — AWS deployment (Terraform)

把搶票系統部署到 AWS(Seoul,`ap-northeast-2`)的 infrastructure-as-code。**低成本、可拋棄**是設計原則:
整套環境每個 session 結束就 destroy,資料靠快照、映像靠獨立 state 的 ECR 活下來。
所有元件都已建好並真機驗過:CD 管線端到端、健康檢查、consumer 擴容、快照還原與 PITR。

## 裡面有什麼

```
                 GitHub Actions(OIDC,無長期金鑰)── deploy.yml ──▶ ECR ──┐
                                                                         ▼
  Internet ──▶ ALB(:80 → /health)──▶ ECS Fargate cluster(Container Insights)
                                        ├─ api        ×2  跨兩個 AZ,可依 sale_imminent + CPU 擴容(旗標)
                                        ├─ consumer   ×1  可依 order_stream_backlog 擴容(旗標)
                                        └─ worker     ×1  SINGLETON,cron 不能重複觸發
                                              │
                        ┌─────────────────────┼──────────────────────┐
                        ▼                     ▼                      ▼
              RDS Postgres 16          ElastiCache Redis 7.1     Secrets Manager
              db.t4g.micro / gp3       cache.t4g.micro            一個 secret,task def 以
              Multi-AZ、加密、          replication group +       valueFrom 逐 key 注入
              刪除保護、最終快照        automatic_failover
              random_page_cost=1.1

  日誌:awslogs → CloudWatch log group(7 天)→ metric filter(比對 JSON 的 event 欄位)→ alarm
```

| 檔案 | 內容 |
|---|---|
| `bootstrap/` | **獨立 state**,只有 ECR repository(見下) |
| `vpc.tf` | VPC、兩個 AZ 的 public(ECS task,`assign_public_ip`)與 private(RDS、Redis)子網;**沒有 NAT Gateway** |
| `security.tf` | 四個 security group:ALB → app → db / redis;`admin_cidr` 可直連 RDS 跑 migration |
| `rds.tf` | Postgres 16;`skip_final_snapshot=false`、`deletion_protection`、`restore_from_snapshot_identifier` 還原槓桿;parameter group 把 planner 成本對齊 gp3 |
| `elasticache.tf` | Redis replication group,failover 與 Multi-AZ 一起開 |
| `ecs.tf` / `taskdefs.tf` / `services.tf` | cluster、三份 task def(0.25 vCPU / 512 MB)、三個 service;`statement_timeout` 只給 api;`ignore_changes = [task_definition, desired_count]` 讓 apply 不會打掉部署與擴容結果 |
| `alb.tf` | HTTP listener;health check 打 `/health`、matcher 收緊成 200;HTTPS 要有自己的網域才加 |
| `autoscaling.tf` | consumer 依佇列深度 step scaling;api 依預熱訊號一次拉到 `max_capacity`、再依 CPU;scale-in 慢於 scale-out;兩者都在旗標後面 |
| `monitoring.tf` | CloudWatch alarm 覆蓋 ECS / RDS / Redis / ALB,加上比對 JSON log 的 metric filter(`admission_paused`、`inventory_drift`、`needs_human`…);**SNS 已拿掉**,alarm 只評估狀態不通知,要開是一行 |
| `secrets.tf` / `iam.tf` | app secret 與 ECS execution / task role;task role 只多 `PutMetricData` |
| `cicd.tf` | GitHub Actions 的 deploy role,**引用**帳號既有的 OIDC provider 而非擁有;權限只到 ECR push、ECS 更新/跑一次性 task、建 RDS 快照、讀 log |
| `drill.py` | 還原演練的驗證工具:seed → fingerprint → mutate → verify,用哨兵列而非筆數判斷「真的回到過去」 |
| `RUNBOOK.md` | 資料還原與回溯:快照還原、PITR、destroy 後取回、AZ 故障;實測 RTO 與演練發現 |

## 先做一次(一輩子一次):bootstrap

```bash
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap apply
```

`infra/bootstrap/` 有**自己的 state**,裡面只有 ECR repository。它刻意不在主設定裡:
映像倉庫是「重建環境的**輸入**」,不是環境的一部分,所以它不該跟著每天的 destroy
一起消失。2026-08-18 的還原演練實測過反例:DB 從快照還原成功、apply 全綠、ALB 一直 503,
因為 repo 被上一次 destroy 連映像一起刪了。理由與實測見 [bootstrap/main.tf](bootstrap/main.tf) 開頭。

沒跑這一步的話,主設定會在 `terraform plan` 就停下來(`RepositoryNotFoundException`)。
那是刻意的 —— 在 plan 停,比環境建完才發現沒有映像可拉好得多。

這跟 `cicd.tf` 對 GitHub OIDC provider 的判斷是同一個:**引用而不是擁有**。那裡是因為
destroy 會刪掉別的專案也在用的東西;這裡是因為 destroy 會刪掉你下一次 apply 需要的東西。

## 第一次 apply

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # 填 admin_cidr 與五個 app secret
cd infra
terraform init
terraform apply
terraform output alb_url
```

- `stripe_webhook_secret` 是 Dashboard 的 **Signing secret**,不是 API key;空的話 app 拒絕啟動(fail-closed)。
- 第一次 apply 之後 ECR 可能還是空的;push `main` 讓 `deploy.yml` 建映像並 roll 服務,或先手動推一版。
- `enable_consumer_autoscaling` / `enable_api_autoscaling` 預設 `false`:需要 `application-autoscaling` 的
  `TagResource` / `ListTagsForResource` / `UntagResource`,`AmazonECS_FullAccess` **不含**這三條,
  而這個缺口 validate 與 plan 都看不到、只在 apply 時 AccessDenied。

## 部署怎麼走(`.github/workflows/deploy.yml`)

push `main` 觸發。先跑 `test.yml`(測試是部署的第一關,不是平行工作流)→ OIDC 換 15 分鐘憑證
→ 建映像、以 SHA 與 `latest` 雙標籤推 ECR → 若 `alembic/versions/` 相對上次部署的 SHA 有變,
先拍 RDS 快照 → 以 worker task def 跑一次性 migration task,**失敗即停**
→ 註冊 **SHA 釘版**的 task def、`update-service --task-definition`(不用 `--force-new-deployment`)
→ `wait services-stable` 後再 `describe-services` 核對線上 ARN(waiter 回 0 不等於部署成功)
→ 透過 ALB 打 `/health/deps` smoke test。ECS deployment circuit breaker 開 `rollback`。

這條管線本身有約四十條測試(`test/test_deploy_pipeline.py`)釘住上面每一個「必須」。

## Golden rule: apply → learn → destroy

整套常開每月約 $60–145。不需要常開 —— 要練習就拉起來,練完就拆:

```bash
cd infra
terraform apply
# ... do your thing ...

# 資料庫有刪除保護,所以 destroy 是兩步。先解開那一個資源(-target 讓它只動 RDS,
# 大約 20 秒),再照常 destroy:
terraform apply -var db_deletion_protection=false -target=aws_db_instance.main
terraform destroy
```

**為什麼多這一步:** 保護擋的不是「手滑打了 destroy」,是一份寫著 `# forces replacement`
的 plan 被草率核准 —— RDS 的重建是「先刪再建」,結果會是最終快照有拍到、但新實例是空的。
細節見 [variables.tf](variables.tf) 的 `db_deletion_protection`。

最大的成本風險是在共用帳號上**忘記 destroy**。有疑慮就 `terraform destroy`。

**`destroy` 不再是「資料就沒了」。** 每次 destroy 都留下一張 `<project>-db-final-<隨機碼>` 快照,
它不會跟著環境消失。要拿回資料、還原到某個時間點、或回退一支壞掉的 migration,
用 `restore_from_snapshot_identifier` 並照 **[RUNBOOK.md](RUNBOOK.md)** 走。

那份程序在 **2026-08-18 演練過了,而且原本是錯的** —— 情境 A 的還原指令是空操作
(會回報 `Apply complete!` 而資料還是壞的)。實測 RTO、七個發現、修正後的程序都在 RUNBOOK 裡;
演練前後用 `drill.py` 驗。

## 成本取捨(dev sizing)

- **不開 NAT Gateway**(約 $33/月):ECS task 放 public subnet 帶 public IP,靠這樣拉映像、打 Stripe。
- ALB 已經在(約 $20/月),目前只有 HTTP;HTTPS 等有自己的網域再加 ACM + Route 53。
- RDS / ElastiCache 都是 `t4g.micro`。RDS **Multi-AZ 預設開**,這是 HA 不是備份,還原時才暫時關。
- ElastiCache 開 TLS + auth token + at-rest 加密(建立時屬性,不能事後開);`REDIS_URL` 因此是秘密,走 Secrets Manager。
- 每個 task 多一個 ADOT sidecar 把 OTel span 送 X-Ray(`ecs-xray.yaml`,只做 trace);task 記憶體因此 512 → 1024 MB。X-Ray 每月前 10 萬筆 trace 免費。
- CloudWatch log 只留 7 天;alarm 不接 SNS,省掉通知那一層,狀態看 console。

## 只有 apply 才看得到的坑

- `aws ecs wait services-stable` 回 0 只代表服務穩定,不代表跑的是新版;要再 `describe-services` 核對 task def ARN。
- autoscaling 的三條 IAM 動作缺了,validate / plan 全綠、apply 才炸。
- 連線預算是**上界不是預測**:四個服務的 pool 大小與兩個 `max_capacity` 加總不能超過 `t4g.micro` 的 `max_connections`,`test_deploy_pipeline.py` 有一條算術釘住它。

## 日常指令

```bash
terraform init       # 一次:抓 provider
terraform validate   # 語法與型別(不打 AWS);CI 也跑
terraform plan       # 預覽(唯讀 AWS 呼叫)
terraform apply
terraform output     # alb_url、database_url、redis_url、ecr_repository_url …
```

State 是本機的 `terraform.tfstate`,solo 夠用;它跟 `terraform.tfvars` 都在 `.gitignore`(含 secret)。
