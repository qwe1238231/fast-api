# infra/ — AWS deployment (Terraform)

Infrastructure-as-code for deploying the ticket system to AWS (Seoul,
`ap-northeast-2`). Built phase by phase; **low-cost / ephemeral** by design.

## 先做一次(一輩子一次):bootstrap

```bash
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap apply
```

`infra/bootstrap/` 有**自己的 state**,裡面只有 ECR repository。它刻意不在主設定裡:
映像倉庫是「重建環境的**輸入**」,不是環境的一部分,所以它不該跟著每天的 destroy
一起消失。理由與實測見 [bootstrap/main.tf](bootstrap/main.tf) 開頭。

沒跑這一步的話,主設定會在 `terraform plan` 就停下來(`RepositoryNotFoundException`)。
那是刻意的 —— 在 plan 停,比環境建完才發現沒有映像可拉好得多。

## Golden rule: apply → learn → destroy

Always-on this stack costs ~$60–145/month. You don't need it always on — bring
it up to practice, then tear it down:

```bash
cd infra
terraform apply     # bring resources up
# ... do your thing ...

# 資料庫有刪除保護,所以 destroy 是兩步。先解開那一個資源(-target 讓它只動 RDS,
# 大約 20 秒),再照常 destroy:
terraform apply -var db_deletion_protection=false -target=aws_db_instance.main
terraform destroy   # tear them ALL down — pay only for the hours used
```

**為什麼多這一步:** 保護擋的不是「手滑打了 destroy」,是一份寫著
`# forces replacement` 的 plan 被草率核准 —— RDS 的重建是「先刪再建」,結果會是
最終快照有拍到、但新實例是空的。多打一行指令換掉那個風險是划算的;細節見
[variables.tf](variables.tf) 的 `db_deletion_protection`。

The biggest cost risk is **forgetting to destroy** on a shared account. When in
doubt, `terraform destroy`.

**`destroy` 不再是「資料就沒了」。** RDS 設了 `skip_final_snapshot = false`,所以每次
destroy 都會留下一張 `<project>-db-final-<隨機碼>` 的快照,而它不會跟著環境消失。
要把資料拿回來(或還原到某個時間點、或回退一支壞掉的 migration)看
**[RUNBOOK.md](RUNBOOK.md)**。

那份程序在 **2026-08-18 演練過了,而且原本是錯的** —— 情境 A 的還原指令是空操作
(會回報 `Apply complete!` 而資料還是壞的)。實測 RTO、七個發現、以及修正後的程序
都在 RUNBOOK 裡。

## Cost notes (dev sizing)

- Skipping the **NAT Gateway** (~$33/mo) on purpose — Fargate tasks will sit in
  public subnets for dev.
- ALB (~$20/mo) only added when we practice HTTPS.
- RDS / ElastiCache use the smallest `t4g.micro` sizes.

## Phases

- **B (current): ECR** — `ecr.tf`. Blocked until the IAM user has
  `AmazonEC2ContainerRegistryFullAccess` (or `PowerUserAccess`).
- C: VPC · D: RDS + ElastiCache · E: ECS + ALB · F: Secrets + CloudWatch + CI/CD

## Usage

```bash
terraform init       # one-time: download the AWS provider
terraform validate   # check the config is well-formed (no AWS calls)
terraform plan        # preview what will change (read-only AWS calls)
terraform apply       # create/update resources
terraform output      # show outputs (e.g. the ECR repo URL)
```

State is local (`terraform.tfstate`) — fine for solo work; don't commit it
(it can contain secrets). See `.gitignore`.
