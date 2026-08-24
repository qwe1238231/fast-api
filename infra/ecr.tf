# --- ECR:引用,不擁有 --------------------------------------------------------
#
# 這個 repository 由 `infra/bootstrap/` 建立並持有,**故意不在這份設定裡**。
#
# 理由是分類:映像倉庫是「重建環境的輸入」,不是環境的一部分。它以前待在這裡、
# 而且設了 `force_delete = true`,所以每次 `terraform destroy` 都會把它連同裡面的
# 映像一起刪掉 —— 下一次 apply 建出一個空的 repo,而 task def 釘的是 `:latest`。
#
# 2026-08-18 還原演練實測到的結果:資料庫還原成功、apply 全綠、ALB 一直回 503,
# 因為沒有東西可以拉。詳見 RUNBOOK.md 的發現 ③。
#
# 跟 cicd.tf 對 GitHub OIDC provider 的處理是同一個判斷:引用而不是擁有。
#
# **第一次使用這份設定之前**,先跑一次(一輩子一次):
#
#     terraform -chdir=infra/bootstrap init && terraform -chdir=infra/bootstrap apply
#
# 沒跑的話這裡會在 plan 階段就失敗,錯誤訊息大致是
# `RepositoryNotFoundException: The repository with name 'justin-test' does not exist`
# —— 那是刻意的:在 plan 就停下來,比建到一半才發現沒有映像好得多。
data "aws_ecr_repository" "app" {
  name = var.project
}
