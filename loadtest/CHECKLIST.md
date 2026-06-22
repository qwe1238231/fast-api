# 上線前壓測 Checklist(搶票系統)

目標情境:**10 萬人搶 5 萬張票**,尖峰到達率約 **20000 RPS**。

核心原則:**每個測試只驗一個瓶頸,解掉再進下一個。** 瓶頸疊在一起會分不出是誰先倒。
方法論:**先量,再改;別預先優化。**

---

## 0. 前置(沒做這些,後面數字全部無效)

- [ ] **壓力產生器與系統分開機器** —— 不要 localhost 同機跑,k6 會跟系統搶 CPU,數字嚴重失真
- [ ] **類 production 環境**:ALB + N 台 Fargate + RDS + ElastiCache(Redis),規格盡量對齊正式環境
- [ ] **四層觀測就位**:
  - API:CPU
  - RDS:連線數 / CPU / write latency / `pg_stat_activity`
  - Redis:ops/s / CPU
  - ALB:p99 / 5xx 率
- [ ] **每輪測試前重置乾淨**:清 order、Redis 與 DB 庫存對齊
- [ ] **帳號 / token 離線預先 seed**,k6 用 `SharedArray` 載入(避免每個 VU 複製整份 → OOM)

---

## 1. 測試序列(照順序跑,每關解一個瓶頸)

| # | 測試 | 盯哪個數字 | 預期先倒在哪 | 對應解法 |
|---|---|---|---|---|
| 1 | **單台基準** | 單台穩定 RPS(p99 爆掉前) | API CPU | 這是橫向擴張的「單位容量」,記下來 |
| 2 | **連線數**(放大到 N 台) | RDS 連線數逼近 `max_connections` | Postgres 連線爆(`too many clients`) | RDS Proxy / PgBouncer transaction-mode;調小 `pool_size`;**asyncpg 關 statement cache** |
| 3 | **讀熱點**(尖峰持續打) | RDS read CPU / QPS | `db.get(Event)` 2 萬讀/s 同一列 | **快取 event 設定**(Redis 或 process 內 TTL)→ 讀負載趨近 0 |
| 4 | **寫入吞吐** | RDS write latency / WAL / commit/s | 搶到者的 INSERT(上限 5 萬筆,可批次) | 通常還好;真撐不住才上批次寫 / async offload 到 arq |
| 5 | **拐點 / 尖峰**(open-model 衝到 20000 RPS) | p99、5xx 率、dropped_iterations | 上面沒解完的那層 | 回到對應關;或加 API 台數 |
| 6 | **規模下的正確性** | Redis 剩餘、守恆律 | (理論上不會倒) | 重跑不超賣三鐵律,在滿載下驗 |
| 7 | **韌性 / 故障注入** | 過載行為、failover | 過載噴 5xx?Redis failover 掉資料? | 確認過載優雅回 409 而非 500;殺一台 task / 觸發 Redis failover 看恢復 |

---

## 2. 不超賣三鐵律(測試 #6 用)

重置乾淨後跑滿載,跑完**立刻**量測(別讓過期 worker 攪動數字):

```bash
# Redis 剩餘庫存(必須 >= 0)
docker exec <redis> redis-cli GET event:1:available

# Postgres 各狀態 order 數
docker exec <db> psql -U <user> -d <db> -c \
  "SELECT status, count(*), COALESCE(sum(quantity),0) AS qty FROM orders WHERE event_id=1 GROUP BY status;"
```

| 鐵律 | 判定 |
|---|---|
| ① Redis 剩餘 ≥ 0 | 負數 = 超賣 |
| ② 守恆 | `Redis 剩餘 + 持有庫存訂單(pending+paid+confirmed)的 qty == 總庫存` |
| ③ 賣出 ≤ 總庫存 | 持有庫存訂單 qty 加總 ≤ 庫存 |

> 已在本機驗過一次:71691 個請求 / 50000 張票 → 正好賣出 50000、Redis 剩 0、零超賣。
> 正確性靠 Redis 單執行緒原子 `DECRBY`,與壓力大小無關。

---

## 3. 上線 Go / No-Go 閘門

全部達標才放行:

- [ ] **容量**:目標尖峰到達率下,p99 < SLA(例如 < 500ms),`dropped_iterations` ≈ 0
- [ ] **正確性**:三鐵律全過(**不可妥協**)
- [ ] **優雅降級**:超過容量時回 409 / 429,**不是 500**(寧可擋客,不可崩)
- [ ] **可恢復**:尖峰過後自動回穩;殺單台能自動接管

---

## 4. 兩個容易被忽略的提醒

- **計算題串起整條鏈**:`需要台數 ≈ 目標 RPS ÷ 單台容量(測試 #1)`,再用這個台數去跑 #2 —— 兩者連動,別各測各的。
- **#3 讀熱點是 CP 值最高的一刀**:cache 掉 `db.get(Event)` 通常比任何擴容都有效、改動又小。**先做這個再加機器。**

---

## 附:庫存持久性與一致性(Redis 遺失的處理)

**問題**:庫存唯一真相在 Redis(`event:{id}:available`),而 Redis 不持久。重啟 / failover / 清空時有三種壞法:

1. **誤判賣完(outage)**:key 不見 → `DECRBY` 當 0 → 全場回 409,票還有卻搶不到(預設會發生)
2. **超賣(最危險)**:有人用 `set_initial_stock` 重設回滿庫存,忽略已售 → 憑空多賣
3. **飄移**:RDB 還原成舊值,跟現實對不上

**心智模型**:Postgres 才是真相,Redis 只是高速衍生快取。任何時刻:

```
剩餘 = total_seats − SUM(quantity) WHERE status IN (pending, paid, confirmed)
```

**解法(四件一起做)**:

- [ ] **reconcile 函式**:Redis 遺失後,從 Postgres 用上面式子重算、`SET` 覆蓋寫回(**不是** `set_initial_stock`,那會設回滿庫存)。冪等,可重跑。
- [ ] **安全重建流程**:① 暫停開賣(`sale_paused` 旗標,reserve 開頭檢查)→ ② reconcile 重算 → ③ 恢復開賣。短暫拒服遠比超賣安全。
- [ ] **縮小遺失視窗**:dev 已開 AOF(`--appendonly yes`);ElastiCache 開 **Multi-AZ + 自動 failover**。但無法保證零遺失,所以 reconcile 永遠是最後防線。
- [ ] **飄移偵測**:低頻背景 job 比對「Postgres 應有剩餘」vs「Redis 實際值」,不一致就告警(可抓到長期飄移,如曾出現的 `total_seats=10` vs Redis `41879`)。

**另一條路(取捨)**:讓 Postgres 當庫存真相,用原子條件更新
`UPDATE events SET available = available - :q WHERE id=:id AND available >= :q RETURNING available;`
—— 又持久又原子又不超賣,不用 Redis;**但**10 萬人搶同一列會卡 row lock,吞吐受限。

| | Redis 計數(現行) | Postgres 條件 UPDATE |
|---|---|---|
| 吞吐 | 高 | 受限於單列 row lock |
| 持久性 | 弱,需 reconcile 補 | 天生持久 |
| 複雜度 | 要自己處理重建 | 簡單但慢 |

極端尖峰選 Redis(吞吐優先)是對的,代價就是要把上面 reconcile + 重建 + 飄移偵測補齊。
