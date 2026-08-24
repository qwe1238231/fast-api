# RUNBOOK — 資料還原與回溯

> **這份程序演練過了(2026-08-18),而且原本是錯的。**
>
> 情境 A 寫的那條還原指令是**空操作** —— 它會回報 `Apply complete!` 而資料還是壞的
> (見下面「發現 ④」)。這就是「沒跑過的程序是假設,不是能力」的具體長相:那一節
> 讀起來完全合理,而且錯了大半年沒人知道。
>
> | 演練項目 | 最後一次跑 | 實測 RTO | 結果 |
> |---|---|---|---|
> | 情境 A(從快照還原) | 2026-08-18 | **49m33s**(可達 ~18m) | ✅ 通過(程序已修正) |
> | 情境 B(PITR 到某一秒) | 2026-08-18 | **10m05s** | ✅ 通過(只還原到臨時實例,未切換) |
> | 情境 C(destroy 之後取回) | 2026-08-18 **前半段** | — | ⚠️ 最終快照已產生並確認存在;**從它建回來的後半段還沒驗** |
> | 情境 D(AZ 故障自動切換) | 尚未 | — | — |

---

## 演練找到的七個問題(2026-08-18)

前三個跟還原無關 —— 它們是**把環境架起來**的時候踩到的,而那正是還原的第一步。

| # | 問題 | 狀態 |
|---|---|---|
| ① | `terraform apply` 成功但整個應用層是死的:秘密版本要等 RDS 位址(十幾分鐘),而 task def 只依賴秘密的**殼**,所以 ECS 服務提早建好、任務讀不到值、部署熔斷器**在值出現前 69 秒**放棄 | 已修(task def 補 `depends_on`) |
| ② | ①是**賽跑**不是必然 —— RDS 快一點就會過。不可重現的「環境開起來是死的」比每次都死難修 | 同上 |
| ③ | `terraform destroy` 連 ECR repository 一起刪掉(`force_delete = true`),所以重建的環境**沒有映像可以跑**。情境 C 原本完全沒提這件事 | 已修(ECR 移到 `infra/bootstrap/`,主設定用 data source 引用)+ 情境 C 步驟 0 |
| ④ | **情境 A 的還原指令是空操作。** `lifecycle { ignore_changes = [snapshot_identifier] }` 只在資源「已存在」時抑制差異 → 資源還在 state 裡,`-var restore_from_snapshot_identifier=X` 被整個忽略 → `No changes` | 已修(程序加 `state rm`) |
| ⑤ | 情境 A 的 28 分鐘 apply 裡有 **20m50s(74%)是 Multi-AZ 轉換**,跟把資料弄回來無關,而它發生在服務恢復**之前** | 已修(新增 `db_multi_az` 變數) |
| ⑥ | `rds.tf` 的 `deletion_protection` 註解寫「預設關閉」,但 `variables.tf` 是 `default = true` —— 註解與事實相反 | 已修 |
| ⑦ | `terraform state rm` 留下一台 **Terraform 看不見的孤兒實例**,`destroy` 不會清它,而且沒有任何症狀 | 見情境 A 步驟 6 |

---

## 先讀這一段:還原的真正陷阱不是還原本身

RDS 的還原**永遠會建出一個新的實例**(PITR 與快照還原都是),舊的不會被覆蓋。所以
危險的一步不是「把資料弄回來」,而是**「讓應用指向弄回來的那一份」**。

應用讀的 `DATABASE_URL` 放在 Secrets Manager,而它的值是 **Terraform 從
`aws_db_instance.main.address` 推導**出來的。也就是說:

- 用 `aws rds restore-...` 手動還原 → 新實例是 Terraform 不認識的東西 → 下一次
  `terraform apply` 會把 secret 指回**舊的**(可能已經不存在的)端點,而 plan 上
  只有一行看起來無害的 secret 變更。
- 這個專案已經被同一類問題咬過三次(`task_definition`、`desired_count`、
  `min_capacity`),所以下面的程序**一律走 Terraform**,不用手打的 restore 指令。

**而 2026-08-18 的演練加了第二層陷阱:走 Terraform 也可能什麼都沒做**(發現 ④)。
所以每一個情境的最後一步都是**驗資料**,不是「指令跑完了」。

---

## 情境 A — 回到某一張快照(最常見:壞掉的 migration)

適用:部署帶了 migration,跑完之後才發現資料被寫壞了。

CD 在跑 migration **之前**會拍一張以 commit SHA 命名的快照(deploy.yml 的
`Snapshot the database before migrating`),所以還原點是現成的。

**實測 RTO 49m33s**,拆解在最後一節。照下面的程序(含 `-var db_multi_az=false`)
應該落在 **15-20 分鐘**。

### 0. 決定要不要追 schema —— 這一步用想的,不要用打的

還原到快照之後,schema 是**那個時間點**的 schema,而 ECS 上跑的是最新的映像。

- 純新增的 migration(expand/contract 規則下的常態)→ 新程式碼在舊 schema 上仍能跑,
  只是用不到新欄位。**什麼都不用做。**
- 破壞性的 migration → 必須**同時**把服務回滾到上一個 task def 修訂版(見步驟 5)。
- **絕對不要在這個情境跑 `alembic upgrade head`。** 你剛剛才逃離那支 migration,
  追上去等於再跑它一次。

判斷指令:`alembic current`(資料庫停在哪)對 `alembic heads`(程式碼要哪一版)。

### 1. 找到還原點

```bash
aws rds describe-db-snapshots \
  --snapshot-type manual \
  --query 'reverse(sort_by(DBSnapshots,&SnapshotCreateTime))[].[DBSnapshotIdentifier,SnapshotCreateTime,Status]' \
  --output table
```

premigration 快照的名字帶著那次部署的 commit 前 8 碼。

### 2. 把壞掉的實例改名讓位

不要刪 —— 它是最後的證據,而且你可能需要比對資料。**改名之後端點的 DNS 名稱會跟著
變,所以應用會斷 —— 這一步就是停機的開始,計時從這裡算。**

```bash
date -u +'T0 停機開始 %H:%M:%S UTC'

aws rds modify-db-instance \
  --db-instance-identifier justin-test-db \
  --new-db-instance-identifier justin-test-db-broken \
  --apply-immediately --no-cli-pager \
  --query 'DBInstance.DBInstanceIdentifier' --output text

# 用**新名字**輪詢。實測 1m36s。
until s=$(aws rds describe-db-instances --db-instance-identifier justin-test-db-broken \
            --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null) && [ "$s" = available ]; do
  echo "$(date -u +%H:%M:%S)  ${s:-還沒出現}"; sleep 15
done
date -u +'T1 改名完成 %H:%M:%S UTC'
```

> **不要用 `aws rds wait db-instance-available --db-instance-identifier justin-test-db`。**
> 改名是非同步的:指令回來的當下實例還叫舊名字、狀態還是 `available`,waiter 會
> **立刻回 0**,而你以為改完了。用新名字輪詢不會短路 —— 那個名字在改名完成前不存在。
> (同一個坑的第一個實例是 CD 的 `wait services-stable`,見 deploy.yml。)

### 3. 把它移出 state ← **原本缺的就是這一步**

```bash
cd infra
terraform state rm aws_db_instance.main
```

**為什麼非要這一步:** `rds.tf` 有 `lifecycle { ignore_changes = [snapshot_identifier] }`,
而 `ignore_changes` 只管**更新**、不管**建立**。資源還在 state 裡的話,
`-var restore_from_snapshot_identifier=X` 會被完全忽略,`terraform plan` 回
`No changes.` —— 你會看到 `Apply complete!` 然後以為資料回來了。

那道 `ignore_changes` 不能拿掉:少了它,下一次**不帶** var 的 apply 會把
`snapshot_identifier` 從有值變回 null,而那是 ForceNew → **一個空資料庫取代剛還原好
的資料**。所以正確做法是繞過它,不是移除它。

(`terraform state rm` **不是冪等的**。重跑會報 `No matching objects found`,那是
「這個位址已經沒東西了」,無害。)

### 4. 從快照建立

```bash
terraform plan -no-color -out=tfplan \
  -var restore_from_snapshot_identifier=<步驟 1 的快照名> \
  -var db_multi_az=false

# plan 上必須看到這兩行,否則不要 apply:
#   # aws_db_instance.main will be created
#       + snapshot_identifier = "<你指定的快照>"
terraform show -no-color tfplan | grep -E '^Plan:|will be created|snapshot_identifier'

date -u +'T2 apply 開始 %H:%M:%S UTC'
terraform apply tfplan 2>&1 | tee /tmp/restore-apply.txt | tail -5
date -u +'T3 apply 完成 %H:%M:%S UTC'
```

> **`-var db_multi_az=false` 是這一步最重要的旗標。** 2026-08-18 實測:帶 Multi-AZ 的
> 還原 apply 花 28m23s,其中 **20m50s 是「還原完成之後」的 Multi-AZ 轉換** ——
> RDS 先建單 AZ 實例,再當成一次修改轉成 Multi-AZ。而那 21 分鐘裡你的服務是停的,
> 為的是一個**跟資料無關**的可用性功能。
>
> Multi-AZ 轉換是**線上操作**,服務恢復之後再補:
> ```bash
> terraform apply -var restore_from_snapshot_identifier=<同一張快照>   # multi_az 回到預設 true
> ```
> 原則:**先恢復服務,再恢復冗餘。** 代價是中間約 20 分鐘沒有 HA,寫下來讓人選,
> 但事故當下這個取捨很明確。

`aws_secretsmanager_secret_version.app` 會被一起重建(它引用
`aws_db_instance.main.address`)。這是對的**而且是必要的** —— 但跑著的任務是在啟動時
讀秘密的,所以下一步不能跳過。

### 5. 讓任務拿到新的秘密

```bash
date -u +'T4 重推服務 %H:%M:%S UTC'
for s in api consumer worker; do
  aws ecs update-service --cluster justin-test --service justin-test-$s \
    --force-new-deployment --no-cli-pager --query 'service.serviceName' --output text
done
```

破壞性 migration 的情況要**同時**回滾程式碼:

```bash
aws ecs update-service --cluster justin-test --service justin-test-api \
  --task-definition justin-test-api:<上一個修訂版號>
```

等它翻過來(實測 6m29s):

```bash
alb=$(terraform output -raw alb_url)
for i in $(seq 1 20); do
  h=$(curl -s "$alb/health/deps"); echo "$(date -u +%H:%M:%S)  $h"
  echo "$h" | grep -q '"status": *"ok"' && break
  sleep 15
done
date -u +'T5 服務恢復 %H:%M:%S UTC'
```

### 6. 清掉孤兒 ← **不做的話它會永遠計費**

`justin-test-db-broken` 在步驟 3 被移出 state,所以 **`terraform destroy` 不會碰它**。
它會在你以為環境已經全毀、帳單應該歸零之後,繼續以 Multi-AZ 的價格跑下去 —— 而且
沒有任何症狀:destroy 成功、plan 乾淨、控制台上那台安安靜靜。

正式事故:先拍一張快照保存證據再刪。演練:直接刪。

```bash
# 它繼承了 deletion_protection = true,所以是兩步
aws rds modify-db-instance --db-instance-identifier justin-test-db-broken \
  --no-deletion-protection --apply-immediately \
  --no-cli-pager --query 'DBInstance.[DBInstanceIdentifier,DeletionProtection]' --output text

aws rds delete-db-instance --db-instance-identifier justin-test-db-broken \
  --final-db-snapshot-identifier justin-test-db-evidence-$(date -u +%Y%m%d%H%M) \
  --no-cli-pager --query 'DBInstance.DBInstanceStatus' --output text
```

> 刪除保護擋不到還原(改名不是刪除、從快照建立也不是),但它**會擋到清理**。
> 這是刻意的:事故當下最不該發生的事,就是不小心把唯一的證據刪了。

### 7. 驗資料 —— 沒有這一步前面都不算完成

見最後一節「還原之後一定要檢查的四件事」。演練環境用:

```bash
export DATABASE_URL="$(terraform output -raw database_url)"
python infra/drill.py verify      # exit 0 才算過
```

---

## 情境 B — 回到某一個時間點(PITR)

適用:知道「幾點幾分之前是好的」,但沒有剛好在那個點的快照。例如一支跑了半小時
才被發現的 UPDATE,或有人手動改了資料。

保留期是 `db_backup_retention_days`(預設 7 天),粒度是**秒**(RDS 每 5 分鐘備份
交易日誌,還原時會把日誌重放到你指定的那一秒)。

**實測 10m05s**(單 AZ),階段拆解:`creating` 5m56s(還原基礎備份 + 重放日誌)→
`configuring-enhanced-monitoring` 1m02s → `backing-up` 3m07s。

```bash
# 1. 可還原的時間範圍。LatestRestorableTime 會落後現在最多 5 分鐘。
aws rds describe-db-instances --db-instance-identifier justin-test-db \
  --query 'DBInstances[0].[EarliestRestorableTime,LatestRestorableTime]' --output table

# 2. 目標時間必須落在上面那個範圍內。等日誌追上(會自己停):
export T_TARGET=2026-08-18T06:40:41Z
while true; do
  latest=$(aws rds describe-db-instances --db-instance-identifier justin-test-db \
             --query 'DBInstances[0].LatestRestorableTime' --output text)
  python3 -c "import sys,datetime as d; sys.exit(0 if d.datetime.fromisoformat('$latest') > d.datetime.fromisoformat('$T_TARGET') else 1)" \
    && { echo "✓ $latest > $T_TARGET"; break; }
  echo "$(date -u +%H:%M:%S)  Latest=$latest 還沒超過"; sleep 30
done

# 3. 還原到臨時實例
DBSG=$(cd infra && terraform output -raw db_security_group_id)
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier justin-test-db \
  --target-db-instance-identifier justin-test-db-pitr \
  --restore-time "$T_TARGET" \
  --db-subnet-group-name justin-test-db \
  --vpc-security-group-ids "$DBSG" \
  --no-multi-az \
  --publicly-accessible \
  --no-deletion-protection \
  --no-cli-pager --query 'DBInstance.[DBInstanceIdentifier,DBInstanceStatus]' --output text

until s=$(aws rds describe-db-instances --db-instance-identifier justin-test-db-pitr \
            --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null) && [ "$s" = available ]; do
  echo "$(date -u +%H:%M:%S)  $s"; sleep 20
done
```

四個旗標都是刻意的:

- `--no-multi-az` —— 還原是為了**看資料**,不需要冗餘。省下的時間見發現 ⑤。
- `--publicly-accessible` —— 不加你的筆電連不上,而這個情境的重點就是**先驗再切**。
- `--no-deletion-protection` —— 臨時實例用完就丟,不要給自己製造多一步 `modify`。
- `--restore-time` —— 每次部署的 log 裡都有 `pre-migration UTC timestamp: ...`,
  那是「這次 migration 開始之前」的座標。

### 4. **先驗資料再切**

連上臨時實例看那幾張表(`orders` 的最後一筆、`stripe_events` 的最後一筆),不要先切
再驗。同一個查詢打兩個端點、答案不同,才排除了「我其實一直在看同一個資料庫」:

```bash
export PITR_HOST=$(aws rds describe-db-instances --db-instance-identifier justin-test-db-pitr \
  --query 'DBInstances[0].Endpoint.Address' --output text)

python - <<'PY'
import asyncio, os, re, asyncpg
url  = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
pitr = re.sub(r"@[^:]+:", f"@{os.environ['PITR_HOST']}:", url)

async def q(dsn, label):
    c = await asyncpg.connect(dsn, timeout=15)
    r = await c.fetchrow("select count(*) n, coalesce(max(id),0) mx from orders")
    print(f"{label:12} orders={r['n']}  max_id={r['mx']}")
    await c.close()

async def main():
    await q(url,  "現在")
    await q(pitr, "PITR")

asyncio.run(main())
PY
```

### 5. 確認之後才切

把舊的改名讓位、`terraform state rm`、然後**照情境 A 步驟 4 的方式**把 PITR 那台的
資料接手 —— 也就是先從它拍一張快照,再用 `restore_from_snapshot_identifier` 建。

> **不要**只是把 PITR 實例改名成 `justin-test-db` 就了事。Terraform 是用
> **DBI resource id**(`db-XXXX...`)追蹤實例的,不是 identifier —— 改名之後它仍然是
> 一個 TF 不認識的物件,下一次 apply 會想把它換掉。

---

## 情境 C — 環境 destroy 之後想把資料拿回來

`skip_final_snapshot = false`,所以每次 `terraform destroy` 都會留下一張
`justin-test-db-final-<隨機8碼>` 的快照。它**不會**跟著 destroy 消失。

> **演練狀態(2026-08-18):前半段驗過了** —— destroy 確實產生了
> `justin-test-db-final-3b2168ef` 並確認存在。**後半段(從它建回來)還沒驗。**
> 下次要開環境時順手做:反正都要 apply,多帶一個 `-var` 就是完整的情境 C。

### 0. 確認 ECR 裡有映像 ← **原本漏掉的一步**

```bash
aws ecr list-images --repository-name justin-test --query 'imageIds[].imageTag' --output json
```

有 `latest` 就跳到步驟 1。

**為什麼要問這件事:** 少了映像,你會把資料庫還原得漂漂亮亮,然後對著 ALB 的 503
發呆 —— 因為沒有東西可以跑。症狀是 ALB 自己的錯誤頁(你的 app 回 JSON,不是 HTML):

```
CannotPullContainerError: ... /justin-test:latest: not found
```

2026-08-18 演練踩到這個,當時 `ecr.tf` 有 `force_delete = true`,所以 destroy 把
repository 連映像一起刪了。**現在 ECR 由 `infra/bootstrap/` 持有,destroy 不會碰它**
—— 所以正常情況這一步只是確認。回 `[]` 的話有兩種可能:第一次用(bootstrap 剛建好、
還沒推過)、或生命週期策略把舊映像清掉了。

最快的路是推到 `main` 讓 CD 原生建(x86 runner,`on: push: branches: [main]`)。
不能推 main 的話本機建:

```bash
repo=$(cd infra && terraform output -raw ecr_repository_url)
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin "$repo"
docker buildx build --platform linux/amd64 -t "${repo}:latest" --push .
docker buildx imagetools inspect "${repo}:latest" --format '{{.Image.Os}}/{{.Image.Architecture}}'
```

三件事不能省:

- **`--platform linux/amd64`** —— task def 釘 `X86_64`(CI 在 x86 runner 上原生建)。
  Mac 原生建出來是 arm64,在 Fargate 上以 `exec format error` 死掉,而那**一樣顯示成
  503**。
- **`${repo}` 的大括號** —— zsh 會把 `$repo:latest` 的 `:l` 當成「轉小寫」修飾符,
  剩下 `atest` 黏在後面 → 推到不存在的 `justin-testatest`。**雙引號擋不住,而且不報錯。**
  (CI 那兩行一樣寫 `"$ECR_REPO:latest"` 但沒事 —— GitHub Actions 的 `run:` 用 bash。)
- **驗架構** —— 不驗的話 arm64 的失敗會在三分鐘後以另一個 503 回來,而你會以為是別的
  問題。`imagetools inspect` 不帶 `--format` 只會給你 manifest 摘要,看不到平台。

### 1. 找到最終快照並還原

```bash
aws rds describe-db-snapshots --snapshot-type manual \
  --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier,`justin-test-db-final`)].[DBSnapshotIdentifier,SnapshotCreateTime]' \
  --output table

cd infra
terraform apply -var restore_from_snapshot_identifier=justin-test-db-final-3b2168ef
```

這裡**不需要** `terraform state rm` —— destroy 已經把資源從 state 清掉了,所以
`snapshot_identifier` 會正常被採用(發現 ④ 只影響資源還在 state 的情況)。

`restore_from_snapshot_identifier` 有 `ignore_changes`,所以**還原完成後不必**把那個
var 拿掉(拿掉也不會觸發重建)。

### ⚠️ 未驗證的風險:主密碼

`destroy` 連 `random_password.db` 一起毀掉,所以下一次 apply 會產生**新的**主密碼,
而快照裡是**舊的**。Terraform 靠還原後那次 `Reset master credentials` 把它改過來
(情境 A 的事件紀錄裡確實有這條)。

**但情境 A 沒能驗證這一步** —— 那次 `random_password.db` 還在 state 裡、值沒變,
reset 等於空操作。**密碼真的不同時它會不會成功,目前是假設。**

如果 apply 之後 `DATABASE_URL` 連不上,先看 RDS 事件確認 credentials reset 有沒有跑:

```bash
aws rds describe-events --source-identifier justin-test-db --source-type db-instance \
  --duration 60 --query 'Events[].[Date,Message]' --output table
```

沒跑的話手動補:

```bash
aws rds modify-db-instance --db-instance-identifier justin-test-db \
  --master-user-password "$(cd infra && terraform output -raw database_url | sed -E 's#.*//[^:]+:([^@]+)@.*#\1#')" \
  --apply-immediately
```

### 2. 之後照情境 A 的步驟 5 重推服務、步驟 7 驗資料

---

## 情境 D — 實例掛了 / AZ 出問題

不用做事。`multi_az = true` 會自動切到另一個 AZ 的待命實例,端點名稱不變,分鐘級。
應用會斷線一次(連線池裡的連線失效),`pool_pre_ping = true` 會把死連線換掉。

要確認切換發生過:

```bash
aws rds describe-events --source-identifier justin-test-db --source-type db-instance \
  --duration 60 --query 'Events[].[Date,Message]' --output table
```

**沒演練過。** 可以用 `aws rds reboot-db-instance --force-failover` 主動觸發。

---

## 還原之後一定要檢查的四件事

還原只是把 Postgres 弄回來,而這個系統的狀態**橫跨 Postgres 與 Redis**。Redis 沒有
被還原,所以它現在描述的是「還原點之後」的世界:

1. **庫存與限購額度會不一致。** Redis 記得那些被還原掉的訂單扣過的庫存,Postgres 不記得。
   worker 的 `detect_inventory_drift` 每 5 分鐘會比對,而「值對不上」是**只告警不自動修**
   的那一類 —— 所以要手動對帳:
   ```bash
   # 每一個還在賣的場次都要跑
   python -m app.scripts.reconcile_inventory <event_id>
   ```
2. **座位空段結構要重建**(有座位圖的場次)。同樣的理由,而且它的偏差不會自己好:
   ```bash
   python -m app.scripts.rebuild_seat_runs <event_id>
   ```
   跑之前先確認 `orders:stream` 已經排空(`XLEN orders:stream` 是 0),不然補集檢查
   會把 in-flight 的 intent 誤判成偏差。
3. **`stripe_events` 少掉的紀錄 = 去重失效。** 還原點之後收到的 webhook 紀錄不見了,
   所以 Stripe 若在保留期內重送那些事件,它們會被當成新事件**重新處理一次** ——
   而其中一條路徑會退款。還原之後去 Stripe 儀表板確認那段時間的事件狀態。
4. **等候室的抽籤順序會變。** `queue:{event}:salt` 在 Redis 裡;如果它也遺失了,
   重新產生的 salt 會讓所有人的 rank 重排。開賣中的場次遇到這個要對外說明。

**深度檢查會誠實回報,而且不會連帶殺掉服務** —— 2026-08-18 實測,資料庫改名之後:

```json
{ "status": "degraded",
  "checks": { "postgres": "gaierror: ... Name or service not known", "redis": "ok" } }
```

ALB 仍在服務、任務沒有被殺掉重啟。這驗證了 `alb.tf` 把 `/health`(不碰 DB)與
`/health/deps`(碰 DB)分開的設計:深度檢查掛在 ALB 上的話,資料庫一抖就全體
unhealthy → ECS 殺光所有任務 → 重啟再去捶正在恢復的資料庫。

---

## 實測 RTO 拆解(2026-08-18,情境 A)

| 區間 | 時間 | 內容 | 可壓縮? |
|---|---|---|---|
| T0→T1 | 1m36s | 改名讓位 | 否 |
| T1→T2 | **13m05s** | 人在讀 plan、發現程序是錯的、現場推修法 | **是** —— 那 13 分鐘就是這份 runbook 的價值 |
| T2→T3 | **28m23s** | apply:還原 ~3m + **Multi-AZ 轉換 20m50s** + 備份 1m + 密碼 reset | **是** —— `-var db_multi_az=false` |
| T3→T5 | 6m29s | 重推服務 + 兩次間隔 30 秒的健康檢查 | 否 |
| **合計** | **49m33s** | | **→ 約 18m** |

輔助數字:

- 手動拍一張快照:**3m32s**(不算在 RTO 裡 —— 真實事故中那張快照是 CD 已經拍好的)
- PITR(單 AZ,含日誌重放):**10m05s**

> **交叉驗證發現 ⑤:** PITR 做的事**比較多**(還原 + 重放交易日誌)卻只花了情境 A 的
> 三分之一時間。這排除了「還原本身很慢」—— 慢的是 Multi-AZ 轉換。

**能對外承諾的 RTO:情境 A 約 30 分鐘,情境 B 約 40 分鐘**(PITR 10m + 切換流程)。
不要承諾 15 分鐘 —— 那是最順利的情況,而演練的教訓是最順利的情況不會發生。

---

## 演練檢查表

- [x] 情境 A:建環境、塞測試資料、拍快照、破壞資料、按程序還原、確認資料回來且應用連得上
- [x] 情境 B:記下時間 → 刪掉幾筆訂單 → PITR 到那個時間 → 確認訂單回來(臨時實例)
- [ ] 情境 B 後半:把 PITR 實例真的接手成正式實例
- [x] 情境 C 前半:destroy → 確認最終快照存在
- [ ] **情境 C 後半:從最終快照還原 → 確認 `DATABASE_URL` 是對的**(最容易錯的一項,
      而且是唯一會踩到「新舊主密碼不同」的路徑)。入口:`justin-test-db-final-3b2168ef`
- [ ] 情境 D:`reboot-db-instance --force-failover` 觸發自動切換,量恢復時間
- [x] 記錄**實際花的時間** —— 那個數字就是 RTO
- [ ] 用 `justin-test-db-premigration-drill01`(內容對得上 `.drill_fingerprint.json`
      的 20 筆)重跑一次修正後的情境 A,確認 RTO 真的降到 ~18 分鐘

`infra/drill.py` 是演練工具:`seed` / `fingerprint` / `mutate [--delete-only]` / `verify`。
**哨兵(sentinel)存在的理由:** 只檢查「訂單還在」的話,一個根本沒還原、只是連回原本
那台的結果也會通過 —— 而那正是還原最可能默默失敗的方式。

## 還沒做的事(這份 runbook 涵蓋不到的)

- **RDS 仍然是 `publicly_accessible = true` 並且在 public subnet。** 目前靠 SG 只放行
  `admin_cidr`,但這是 dev 的設定;正式做法是私有子網 + 走 SSM/bastion,而那會連帶
  改變 migration 怎麼跑(CI 現在從 GitHub runner 直連)。
- **跨區域備份沒有。** 快照留在 ap-northeast-2;整個區域出事就沒有了。
  `aws rds copy-db-snapshot --source-region` 到另一區是下一步。
- **沒有自動驗證備份可用。** 業界會定期自動還原一份到臨時實例、跑幾條檢查、再刪掉。
  現在只有人工演練 —— 而人工演練的間隔就是「程序默默壞掉」的窗口,這次的發現 ④
  就是在那個窗口裡長出來的。
- **ECR 屬於可拋棄的環境。** 映像倉庫是重建環境的**前提**,不該跟環境同生共死
  (同一個判斷已經套用在 GitHub OIDC provider 上:用 data source 引用而不是擁有它)。
  情境 C 步驟 0 是繞道,不是解法。
- **帳號裡有別的專案**(`jh-finance-postgresql`、`jh-postgresql`、`jh-postgresql-dev`)。
  清快照時**必須**用 `justin-test-` 前綴過濾 —— 快照刪掉沒有回收桶。
