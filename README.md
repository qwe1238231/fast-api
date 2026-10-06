# Ticket System API（搶票系統）

高併發售票系統的後端 API。核心目標:**在「十萬人搶五萬張票」的尖峰下,絕不超賣、絕不重複配位、優雅降級**。

技術重點不只在「能跑」,而在**正確性、安全性、可觀測性與可驗證的容量**:等候室抽籤放行、Redis 原子庫存、無孤兒座位配位、下單非同步化、Transactional Outbox 兜底、Stripe-first 的付款生命週期、Rust 密碼學原語、PII 信封加密、Terraform 上 AWS 並真機驗過的 CI/CD 與 DR 演練,以及一套親手驗證過的 k6 壓測方法論。

---

## 目錄

- [技術棧](#技術棧)
- [系統架構](#系統架構)
- [搶票主流程](#搶票主流程)
- [核心設計](#核心設計)
  - [1. 等候室 — 抽籤、定速放行、單次 token](#1-等候室--抽籤定速放行單次-token)
  - [2. 庫存與下單非同步化 — 一支 Lua、202、Stream](#2-庫存與下單非同步化--一支-lua202stream)
  - [3. 座位配位 — 無孤兒座、read-compute-CAS](#3-座位配位--無孤兒座read-compute-cas)
  - [4. 訂單狀態機與釋放兜底 — Outbox](#4-訂單狀態機與釋放兜底--outbox)
  - [5. 付款生命週期 — Stripe](#5-付款生命週期--stripe)
  - [6. 快取 stampede 防護](#6-快取-stampede-防護)
  - [7. 認證與登入防護](#7-認證與登入防護)
  - [8. PII 信封加密、Rust 密碼學模組、GDPR 抹除](#8-pii-信封加密rust-密碼學模組gdpr-抹除)
  - [9. 稽核日誌 — Stream 管線 + 月分區](#9-稽核日誌--stream-管線--月分區)
  - [10. HTTP 護欄與可觀測性](#10-http-護欄與可觀測性)
  - [11. DB 加固與 CI 守門](#11-db-加固與-ci-守門)
  - [12. 背景任務](#12-背景任務)
- [資料模型](#資料模型)
- [API 端點](#api-端點)
- [目錄結構](#目錄結構)
- [本地開發](#本地開發)
- [測試與 CI](#測試與-ci)
- [壓力測試](#壓力測試)
- [監控](#監控)
- [部署](#部署)

---

## 技術棧

| 層 | 技術 |
|---|---|
| Web 框架 | **FastAPI** / Starlette / **Uvicorn**(多 worker);等候室用 **SSE** 推播 |
| 語言 | **Python 3.12**(async/await 全程非同步) |
| 密碼學原語 | **Rust**(Argon2id、AES-256-GCM、HMAC-SHA256),經 **PyO3 + maturin** 編成 Python 套件 |
| 資料庫 | **PostgreSQL 16** + **SQLAlchemy 2.0**(async)+ **asyncpg** + **Alembic**;`btree_gist` EXCLUDE、宣告式分區 |
| 快取 / 庫存 / 佇列 | **Redis 7**(Lua 原子庫存、座位 free-run、等候室 ZSET、Stream、Pub/Sub、限流、快取) |
| 背景任務 | **arq**(cron)+ 獨立的 order consumer 行程 |
| 認證 | **PyJWT**(HS256 access token)+ 自製 refresh token 輪替 + 自製 Redis 固定視窗限流 |
| 金流 | **Stripe**(PaymentIntent + webhook 簽章驗證 + 事件去重) |
| 資料驗證 | **Pydantic v2** / pydantic-settings(fail-closed 啟動驗證) |
| 可觀測性 | JSON 結構化日誌 + trace id、**OpenTelemetry** 分散式追蹤(→ Tempo,本地)、**Prometheus** + **Grafana**(本地)、**CloudWatch**(AWS) |
| 壓測 | **k6**(open-model arrival-rate、thresholds、A/B 模式) |
| 容器 | **Docker** 多階段建置(Rust builder → Python builder → 非 root runtime) |
| 基礎設施 | **Terraform**:ECS Fargate + RDS + ElastiCache + ALB + Secrets Manager(AWS Seoul) |
| CI/CD | **GitHub Actions**:測試 + schema 守門 + terraform validate;OIDC 部署、migration 閘門、SHA 釘版、自動回滾 |

---

## 系統架構

```
                        ┌───────────────────────────────────────────────┐
     HTTP / SSE         │              FastAPI (api)  Uvicorn × N        │
  ──────────▶  ALB ───▶ │  TraceId → CORS → BodySize → Timeout → router  │
                        │  api/ 路由+DI │ services/ 業務 │ crud/ │ core/   │
                        └───────┬──────────────────────────┬────────────┘
                                │                          │
                   ┌────────────▼──────────┐   ┌───────────▼───────────────────┐
                   │     PostgreSQL 16     │   │            Redis 7            │
                   │ orders / seat_holds   │   │ 庫存 Lua │ 座位 free-run (CAS) │
                   │ outbox / stripe_events│   │ 等候室 ZSET │ admission SETNX  │
                   │ audit_logs (月分區)   │   │ orders:stream │ audit:events  │
                   │ users / buyer_info    │   │ Pub/Sub(SSE poke)│ 快取+lock │
                   └───────────▲──────────┘   └──────┬──────────────┬─────────┘
                               │                     │ XREADGROUP   │ XREADGROUP
                               │            ┌────────▼───────┐ ┌────▼─────────────────────┐
                               └────────────┤ order-consumer │ │ arq worker(cron)          │
                                 INSERT     │ orders INSERT  │ │ 逾時/T2/reclaim/outbox     │
                                            │ + XACK         │ │ 稽核/drift/分區/清理/告警  │
                                            └────────────────┘ └──────────────────────────┘

  可觀測性:/metrics → Prometheus → Grafana(本地);JSON log → CloudWatch metric filter → alarm(AWS)
              OTel span → otel-collector → Tempo → Grafana Explore(本地;正式環境改接 ADOT sidecar)
```

**分層原則**:`core/` 不依賴 FastAPI;`api/` 負責所有框架轉接(`Depends`、`HTTPException`、middleware);`services/` 放業務規則(狀態機、庫存、配位、付款);`crud/` 只做純資料存取。

---

## 搶票主流程

一位買家從排隊到拿到座位,系統實際走的路:

1. **排隊**:`POST /events/{id}/queue` 登記;位置由 `sha256(event:user:salt)` 決定,不看誰先按。
2. **等放行**:`GET …/queue/stream`(SSE)或輪詢 `…/queue/status`。放行時間是算出來的,不是搶出來的。
3. **下單**:帶 `Admission-Token` + `Idempotency-Key` 打 `POST /orders/`,一支 Lua 完成去重、限購、扣庫存(或 CAS 配位)、入列,回 **202**。
4. **落地**:order-consumer 從 `orders:stream` 消費,寫 `orders`(與 `seat_holds`);client 用 `GET /orders/by-key/{key}` 拿結果。
5. **付款**:`POST /orders/{id}/payment-intent` 拿 `client_secret`;Stripe webhook 驗簽、去重、核對金額後把訂單轉 CONFIRMED。
6. **看座位**:`GET /orders/{id}/seats` 只在 CONFIRMED 後揭露座號。
7. **沒付**:第一道逾時轉 EXPIRED;有 PaymentIntent 的走第二道、先問 Stripe 再收。釋放失敗由 outbox relay 兜底。

---

## 核心設計

### 1. 等候室 — 抽籤、定速放行、單次 token

搶票尖峰的第一道閘不是庫存,是**人數**。等候室把「誰可以進來下單」變成可計算、可限速的事。

- **抽籤而非先到先得**:登記寫入 `ZADD queue:{event}:draw`,score 是 `sha256(event:user:salt)`。salt 在 publish 時固定,所以結果可重現、不能重抽、與到達時間無關。
- **放行是算出來的**:`已放行人數 = min(經過秒數 × QUEUE_ADMISSION_RATE, 總人數)`,`ZRANK < 已放行人數` 即放行。沒有 counter、沒有鎖,每個人的放行時刻可以直接算給他看。
- **單次 token**:放行後拿到短效 JWT(含 `jti`);`POST /orders` 先 `SET admission_used:{jti} NX` 再做任何庫存動作。售完、區不合等**業務拒絕**會退還 token,其他例外不退。不變式:token 消耗 ⟺ 訂單意圖被接受。
- **SSE 推播**:每個 process 一條 `PSUBSCRIBE queue:*:events`,poke 進每條連線的 `asyncio.Queue(maxsize=1)` 合併;醒來重算狀態。連線上限 300s 讓 EventSource 自動重連,狀態一律從 Redis 重算,不需 replay。
- **斷路器**:死信增速或未落地積壓超標 → `admission:paused`(120s TTL,fail-open 自動恢復),買家保留名次、看到 `paused=true`。售完同樣擋放行並結束串流。
- **預熱訊號**:`sale_starts_at` 前 20 分鐘起每分鐘發 CloudWatch gauge `sale_imminent`,讓 ECS 在尖峰前就擴容。用 metric 而非直接改 `min_capacity`,Terraform 仍是唯一 owner。

### 2. 庫存與下單非同步化 — 一支 Lua、202、Stream

搶票主路徑**不在請求內寫 DB**。

- **一支原子 Lua** 一口氣完成:`Idempotency-Key` 去重、每人每場限購(`event:{id}:purchased`,含 PENDING)、扣庫存、`XADD orders:stream`、寫 claim。Redis 單執行緒天生序列化,十萬個請求不可能兩人同時搶到最後一張。
- **202 Accepted**:真正的 `orders` INSERT 由 order-consumer 消費後寫入;`orders.idempotency_key` UNIQUE 讓「重抄無害」。
- **查詢**:`GET /orders/by-key/{key}` → `processing` / `ready`(附訂單)/ `failed`(放棄並已退票)。
- **可靠性**:消費者崩潰時 `XPENDING` + `XCLAIM` 重領;超過投遞上限進死信 stream、退回庫存、claim 標 `FAILED`。死信數同時餵等候室斷路器。
- **Redis 不持久的代價**:每 5 分鐘 `detect_inventory_drift` 比對 Redis 與 Postgres;遺失時可自動修復(`AUTO_HEAL_LOST_REDIS_STATE`)或手動 `python -m app.scripts.reconcile_inventory <event_id>`。

**已實測驗證**:71,691 個請求搶 50,000 張票 → 正好賣出 50,000、Redis 剩 0、零超賣(見 [`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md))。

### 3. 座位配位 — 無孤兒座、read-compute-CAS

對號座的難點不是「有沒有位子」,是「四個人要坐一起,而且不能留下賣不掉的單座」。

- **資料模型**:`seat_blocks` 是兩條走道之間的一段連號座,是配位的原子單位;`seat_holds` 是一張訂單佔的區間 `[start_pos, start_pos+length)`。`pos` 是稠密索引、`label` 是門牌(會跳 4、13,會單雙號分邊),兩者刻意分開。
- **無孤兒規則用一個潛勢函數表達**:φ(1) 很低、其他取面值,加上切割成本;任何會留下長度 1 殘段的切法成本為正,補上既有孤兒的切法成本為負。程式裡沒有任何 `if quantity == 1`。中切預設關閉:實測它用庫存換品質,比例約 1:1。
- **收尾期 ratchet**:連續三次嚴格策略配不到,該區單向切到寬鬆策略並在同一次請求內重試,避免最後幾張卡死。
- **Redis free-run + CAS 而非 Lua 配位**:`runs`/`ends` 兩個 HASH 構成 boundary tags,左右合併 O(1)。流程是 pipeline 讀 → Python 算合法錨點 → 短 Lua 比對 `(start, length)` 後切分。放行速率下 N = λ×T ≈ 0.5,碰撞罕見;`TOP_K` 隨機化把高負載下的重試放大從 3.08× 壓到 1.03×。
- **DB 是最後一道網**:`seat_holds` 上 GiST `EXCLUDE (event_id =, block_id =, int4range &&)` 讓重複配位在資料庫層不可能;生成欄位 `last_pos` 加複合 FK 保證「不賣不存在的座位」;trigger 核對 hold 與 order 的 event/zone/length 一致。
- **drift 偵測**:每 5 分鐘檢查 index 一致、counter 守恆、runs 等於 DB holds 的補集、event 總量等於各區總和;不一致只告警並指出修復命令,`rebuild_seat_runs` 在 stream 未排空時拒絕執行。
- **可觀測性**:`seat_cas_window_seconds`、`seat_cas_attempts_total`、`seat_contention_total` 三個指標與 [`monitoring/alerts.yml`](monitoring/alerts.yml) 四條告警,其中 p99 CAS 視窗超過 10ms 是「該把配位搬進 Lua」的訊號。

三支模擬器([`app/scripts/simulate_*.py`](app/scripts/))分別量 bin-packing 效率、CAS 碰撞與 compaction 收益;compaction 的結果推翻了原設計。

### 4. 訂單狀態機與釋放兜底 — Outbox

```
                       webhook(驗簽/去重/核金額)
  PENDING ────────────────────────────────▶ PAID → CONFIRMED   (終態)
     │
     ├─ T1 逾時(10 分鐘未付)────────────▶ EXPIRED              (終態,釋放)
     ├─ T2 逾時(15 分鐘,有 intent)──▶ Stripe cancel 確認 canceled 才 EXPIRED
     └─ 取消 / payment_intent.canceled ──▶ CANCELLED / EXPIRED (終態,釋放)
```

- 所有轉換在 service 層用 **CAS**(`UPDATE … WHERE status = 期望值`)完成,非法轉換一律 `409`。
- **釋放有兩層**:轉終態的同一個交易寫一筆 `outbox`,commit 後走 fast path 釋放 Redis;fast path 死掉,`process_outbox` 每分鐘用 `FOR UPDATE SKIP LOCKED` 租約重放,指數退避、八次後告警。
- **釋放是冪等的**:DB 側 `seat_holds` 已刪則略過;Redis 側 Lua 內 `released:{order}` SETNX 擋重放。
- payload 刻意為空:relay 重讀已是終態的訂單,不會讀到過期資料。處理過的列保留七天,是告警時的第一現場。

### 5. 付款生命週期 — Stripe

- **webhook 形狀**:驗簽 → `stripe_events` 以 Stripe event id 為主鍵 `INSERT … ON CONFLICT DO NOTHING` 去重 → 只做 DB 決策 → commit → commit 後才做退款、釋放等外部副作用。去重與狀態變更同一交易,失敗一起回滾,靠 Stripe 重送。
- **核對金額**:`amount_received` 不等於 `total_price_cents`、訂單已非 PENDING、或 CAS 輸了,一律**全額退款、絕不確認**。
- **`payment_failed` 只記 log**:刷卡被拒不作廢訂單,買家可換卡重試;`payment_intent.canceled` 才收單。
- **T2 是 Stripe-first**:有 PaymentIntent 的逾時訂單先對 Stripe cancel,確知 `canceled` 才轉 EXPIRED;回 `succeeded` 留給 webhook;沒有任何 DB session 跨越 Stripe 往返。
- **`/cancel` 刻意相反,是 local-first**:先 CAS 取消並 commit,再 best-effort cancel intent;晚到的扣款由 succeeded webhook 退款。
- **fail-closed 啟動驗證**:空的 webhook secret、模擬付款、略過等候室,任一在非 `DEBUG` 下設定都拒絕啟動。「空的 secret 是一個已知的 secret。」

### 6. 快取 stampede 防護

熱路徑每次都要讀 event 設定(狀態、售票窗、票價表)。快取 60 秒,但 key 一冷,四個 worker × 數百個併發請求會同時打 DB。

- **第一層(process 內)singleflight**:同 key 只起一個 `asyncio.Task`,其他人 `shield` 等待,請求逾時被取消不會殺掉 flight。
- **第二層(跨 process)Redis lock**:`SET NX PX` 加隨機 token,拿不到鎖的人**輪詢快取 key 而非鎖**,逾時就自己讀 DB,永不讓請求失敗;釋放用 `GET == token → DEL` 的 Lua 圍欄。
- **獨立 bulkhead pool**:等 flight 的請求手上都握著主 pool 連線,loader 若也用主 pool 會死鎖。實測 300 個冷 key 併發請求產出 149 個 504、零 DB 讀,之後 loader 改走 `pool_size=1` 的獨立 engine。
- **量得到**:`event_meta_cache_requests_total{hit|miss}` 與 `event_meta_cache_flights_total{loaded|already_filled|waited|fallback}`,DB 讀數 = `loaded + fallback`。量測 harness 見 [`loadtest/measure_cache_stampede.py`](loadtest/measure_cache_stampede.py)。

### 7. 認證與登入防護

- **Access token**:JWT(HS256),短效、無狀態。
- **Refresh token**:不透明隨機字串,只存 SHA-256 雜湊;HttpOnly cookie 限 `/v1/auth`。每次 refresh **輪替**;同一 family 的舊 token 在 grace window 後再被用 → 視為竊用,**撤銷整個 family**。CSRF 用 double-submit cookie。
- **登入鎖定是以帳號計**:`rl:login_fail:{username}` 在驗密碼**之前**就先查,省下 Argon2 的 CPU 也擋掉 CPU 耗盡攻擊;以送進來的字串計數,不存在的帳號也一樣鎖,沒有列舉 oracle。找不到使用者時跑 dummy verify 抵禦 timing 攻擊。
- **一個限流器**:自製 Redis 固定視窗(Lua `INCR` + 條件 `EXPIRE`,崩潰不會留下永不過期的 counter),登入、refresh、註冊、等候室加入、選區畫面共用;`RATE_LIMIT_ENABLED` 一鍵關閉供壓測。

### 8. PII 信封加密、Rust 密碼學模組、GDPR 抹除

買家實名資料採**信封加密**:每筆隨機 DEK 以 AES-256-GCM 加密明文,DEK 再用主金鑰 KEK 包一層,兩者一起存;查詢比對走 HMAC-SHA256 lookup hash,不需解密。KEK 有版本號(`buyer_info.kek_version`),輪替是線上的:新鑰匙設現役、舊鑰匙退役只用來解,worker 逐批把舊列的 DEK 重包到新版,密文不碰;程序在 `infra/RUNBOOK.md` 情境 E。

所有原語由 Rust crate `ticket_secrets` 實作,PyO3 綁定、maturin 編成 wheel:

| 函式 | 用途 | 演算法 |
|---|---|---|
| `hash_password` / `verify_password` | 密碼雜湊 | **Argon2id** |
| `aes_gcm_encrypt` / `aes_gcm_decrypt` | PII 加解密 | **AES-256-GCM** |
| `hmac_sha256` | 可查詢 lookup hash | **HMAC-SHA256** |

> 為什麼用 Rust:密碼學熱路徑用編譯語言實作,避開 GIL 與純 Python 實作的效能/安全疑慮;金鑰長度等不變式在 Rust 端強制檢查。

**GDPR 抹除**(`DELETE /users/{id}`,admin):`users` 列保留但匿名化、密碼置為不可用、停用;`buyer_info` 整列刪除即 **crypto-shred**(密文與包好的 DEK 一起消失);refresh token 先清。**訂單活下來**:會計紀錄有法定保留期,`orders.user_id` 不置空,限購與分頁索引也不受影響。重複呼叫冪等。

### 9. 稽核日誌 — Stream 管線 + 月分區

```
API(emit_event)──XADD──▶ Redis Stream "audit:events"(~1ms)
                                  │
             arq worker ──XREADGROUP(consumer group)──▶ 批次寫 Postgres ──▶ XACK
```

- 請求端只付一次 `XADD`;worker 批次落地,consumer group 保證 at-least-once,毒訊息進死信,lag 超標告警。
- **`audit_logs` 按月 RANGE 分區**:cron 每日預建三個月分區;保留期一到整個分區 `DROP TABLE`,不再 `DELETE` 百萬列。DEFAULT 分區是**警報不是備援**,有列進去代表預建漏了。
- 稽核事件涵蓋登入成功/失敗/鎖定、後台編輯、抹除等。

### 10. HTTP 護欄與可觀測性

- **Middleware 順序由測試釘死**(外→內):`TraceId` → `CORS` → `BodySizeLimit` → `RequestTimeout` → Prometheus → router。
- **trace id 一律由伺服器產生**,不信任 client 帶來的;ALB 的 `X-Amzn-Trace-Id` 只在 `TRUSTED_PROXY_COUNT > 0` 時以附加欄位記錄。trace id 跟著 `XADD` 進 Stream,worker 消費時重新綁定,一筆訂單從請求到落地可以串起來。
- **JSON 結構化日誌**:欄位由 `contextvars` 綁定,CloudWatch metric filter 直接比對 `event` 欄位(如 `inventory_drift`),不是 grep 文字。
- **OpenTelemetry 追蹤**(`core/tracing.py`):FastAPI / SQLAlchemy / redis / httpx 自動 instrument,log 的 trace id 就是 span 的 trace id,從瀑布圖可直接跳到那筆請求的 log。入站 `traceparent` 一律不接續(propagator 只注入不提取);`XADD` 多帶 `traceparent`,consumer 落帳的 span 接在下單請求的 server span 底下;每次 cron 執行是一個 root span。沒設 `OTEL_EXPORTER_OTLP_ENDPOINT` 就不匯出。
- **請求護欄**:body 上限 1 MiB 回 413;逾時只管到 `http.response.start` 回 504,所以 SSE 不會被切;連線層帶 `idle_in_transaction_session_timeout` 與 `lock_timeout`,`statement_timeout` 只給 API task,worker 要跑長 cron 與 migration。
- **兩種健康檢查**:`/health` 只證明 process 活著,給 ALB 用,免得共用 DB 一抖就讓每個 target 一起被判死;`/health/deps` 真的 `SELECT 1` + `PING`,回 200 或 503,給值班人與部署 smoke test 用。
- **指標**:HTTP 指標之外,API 行程在 scrape 時讀 `XLEN` 匯出 `order_stream_backlog` 與 `order_dead_letter_depth`;座位 CAS 與快取 flight 各有自己的 counter。

### 11. DB 加固與 CI 守門

- **高消耗率的主鍵加寬到 BIGINT**(`orders`、`seat_holds`、`refresh_tokens`、`audit_logs`),連 sequence 一起改;目錄表刻意留 int4。
- **後台編輯樂觀鎖**:`events`、`zones`、`event_zone_prices` 帶 `version`,client 把 GET 回來的 `version` 原樣放進 PATCH body,過期回 409。判準:高競爭用 CAS,低競爭用 version。
- **CI 守三件 `alembic check` 看不到的事**:index 定義(INCLUDE、opclass、排序)、每表 reloptions(autovacuum 調參)、trigger 與 function 本體,由 `check_schema_drift` 對獨立參考 DB 比對。
- **部署管線本身有約四十條測試**(`test_deploy_pipeline.py`):deploy 必須 `needs: test`、migration 在 roll 之前、失敗要真的停、服務用 `--task-definition` 釘版而非 `--force-new-deployment`、連線預算不超過 RDS 上限、worker 不自動擴容、scale-in 慢於 scale-out。
- **`test_ci_config.py`** 確保 CI 環境裡沒有任何 `DEBUG`,fail-closed 守衛在 CI 就以生產形狀跑過。

### 12. 背景任務

`arq` worker(`arq app.worker.WorkerSettings`)排程;order-consumer 是獨立行程(`python -m app.order_consumer`),不在 cron 裡。

| 任務 | 頻率 | 作用 |
|---|---|---|
| `expire_pending_orders` | 每分鐘 | T1 逾時未付款 → EXPIRED,釋放 |
| `expire_abandoned_payments` | 每分鐘 | T2:有 PaymentIntent 的逾時單,Stripe-first 收掉 |
| `reclaim_stale_order_intents` | 每分鐘 | 重領 consumer 沒 ack 的訂單意圖,超限進死信 |
| `process_outbox` | 每分鐘 | 重放釋放失敗的座位(兜底) |
| `consume_audit_events` | 每分鐘 | 消費稽核 Stream,批次寫 Postgres |
| `report_queue_depth` | 每分鐘 | 積壓/死信 → CloudWatch gauge;超標即暫停放行 |
| `publish_prewarm_signal` | 每分鐘 | 開賣前發 `sale_imminent`,提前擴容 |
| `detect_inventory_drift` | 每 5 分鐘 | Redis 庫存與 Postgres 比對;限購 quota 輪班審計 |
| `detect_seat_structure_drift` | 每 5 分鐘 | 座位 free-run 四個不變式檢查 |
| `ensure_audit_log_partitions` | 每日 02:15 | 預建未來三個月的 `audit_logs` 分區 |
| `purge_old_audit_logs` | 每日 02:30 | 過保留期的分區整個 DROP |
| `purge_old_stripe_events` | 每日 02:45 | 清 webhook 去重紀錄 |
| `purge_expired_refresh_tokens` | 每日 03:00 | 清過期 refresh token |
| `purge_processed_outbox` | 每日 03:15 | 清已處理七天以上的 outbox 列;未處理的永不清 |
| `purge_finished_event_keys` | 每日 03:30 | 清結束場次的 Redis key |

每筆在獨立交易中處理,單筆失敗不中斷整批;`max_jobs` 撞頂會告警而非靜靜排隊。

---

## 資料模型

| 表 | 重點欄位 |
|---|---|
| `users` | `username`(unique)、`hashed_password`(Argon2id)、`is_active`、`is_admin` |
| `events` | `status`(draft/published/cancelled)、售票窗、`queue_opens_at`/`queue_closes_at`、`venue_id`(空=自由座)、`price_cents`(自由座)、`version` |
| `orders` | `status`、`idempotency_key`(**unique**)、`zone_id`、`quantity`、`total_price_cents`、`payment_provider_id`、各狀態時間戳;**BIGINT** |
| `venues` / `zones` / `event_zone_prices` | 場館、票區(`version`)、每場每區票價(`version`) |
| `seat_blocks` / `seats` | 走道間的連號段(配位原子單位,含品質參數)、座位 `pos` 與 `label` |
| `seat_holds` | 訂單佔的區間;GiST **EXCLUDE** 防重疊、生成欄位 `last_pos` 複合 FK、`confirmed_at`;**BIGINT** |
| `outbox` | `event_type`、`attempts`、`next_attempt_at`、`processed_at`;部分索引只覆蓋未處理列 |
| `stripe_events` | 主鍵即 Stripe event id;webhook 去重 |
| `refresh_tokens` | `token_hash`、`family_id`、`used_at`、`revoked_at`、sliding/absolute 過期、`user_agent`、`ip_address`;**BIGINT** |
| `buyer_info` | `national_id_ciphertext`、`national_id_dek_encrypted`、lookup hash(信封加密) |
| `audit_logs` | `event_type`、`actor_*`、`target_*`、`payload`(JSON);**按月分區**;**BIGINT** |

狀態欄位存字串(`native_enum=False`)。Schema 由 Alembic 管理,CI 同時跑 `alembic check` 與 `check_schema_drift`。

---

## API 端點

所有端點前綴 `/v1`。互動式文件:`GET /` 轉址到 `/docs`。

### Auth / Users
| Method | Path | 說明 |
|---|---|---|
| POST | `/auth/token` | 帳密登入,回 access token + 設 refresh/CSRF cookie(限流、帳號鎖定) |
| POST | `/auth/refresh` | 用 refresh cookie 換新 token(輪替 + 重用偵測) |
| POST | `/auth/logout` / `/auth/logout-all` | 撤銷當前 family / 全部 |
| POST | `/users/` | 註冊(限流,重複回 409) |
| GET | `/users/me` | 當前使用者 |
| DELETE | `/users/{id}` | admin:GDPR 抹除(匿名化 + crypto-shred,訂單保留),冪等 |

### Waiting room
| Method | Path | 說明 |
|---|---|---|
| POST | `/events/{id}/queue` | 登記排隊;窗未開/已關回 409 |
| GET | `/events/{id}/queue/status` | 名次、`people_ahead`、`paused`、`sold_out`;放行後附 token |
| GET | `/events/{id}/queue/stream` | 同上,SSE 推播;放行或售完即結束 |

### Orders(搶票核心)
| Method | Path | 說明 |
|---|---|---|
| POST | `/orders/` | **下單**:需 `Idempotency-Key` + `Admission-Token`;回 **202**;售完 409、token 無效 403 |
| GET | `/orders/by-key/{key}` | `processing` / `ready` / `failed` |
| GET | `/orders/me` | 我的訂單(keyset 分頁) |
| GET | `/orders/{id}` | 單筆(非本人 404) |
| GET | `/orders/{id}/seats` | 座號,**CONFIRMED 後才揭露**(之前 409) |
| POST | `/orders/{id}/payment-intent` | 建立 Stripe PaymentIntent,回 `client_secret` |
| POST | `/orders/{id}/cancel` | 取消(local-first,順手 cancel intent) |
| POST | `/orders/{id}/pay` | 模擬付款,**只在 `DEBUG` + `ENABLE_MOCK_PAYMENT` 下存在**,否則 404 |

### Events / Zones
| Method | Path | 權限 | 說明 |
|---|---|---|---|
| POST | `/events/` | admin | 建活動(自由座給 `total_seats`+`price_cents`;對號座給 `venue_id`+`zone_prices`) |
| PATCH | `/events/{id}` | admin | 編輯,body 帶 `version`(樂觀鎖) |
| PATCH | `/events/{id}/zone-prices` | admin | 批次改票價,逐列 `version`,全有或全無 |
| POST | `/events/{id}/publish` | admin | 草稿 → 開賣,**灌庫存進 Redis、固定抽籤 salt** |
| GET | `/events/` / `/events/{id}` | 公開 | 已開賣活動 |
| GET | `/events/{id}/zones` | 公開 | 各區票價、剩餘、**可買的張數集合**(IP 限流、2s 快取) |
| GET / PATCH | `/zones/{id}` | admin | 票區查改(樂觀鎖) |

### Buyer Info / Webhooks / 系統
| Method | Path | 說明 |
|---|---|---|
| POST / GET | `/buyer-info/` / `/buyer-info/me` | 實名資料登記 / 查詢(信封加密) |
| POST | `/webhooks/stripe` | 驗簽、去重、核金額;`succeeded` 確認或退款、`payment_failed` 只記 log、`canceled` 收單 |
| GET | `/health` | liveness(ALB 用,不碰依賴) |
| GET | `/health/deps` | Postgres + Redis 探測,200 / 503 |
| GET | `/metrics` | Prometheus |

---

## 目錄結構

```
app/
├── api/
│   ├── deps.py              # DI:DbSession / Redis / CurrentUser / CurrentAdmin / client_ip
│   ├── middleware.py         # TraceId / CORS / BodySizeLimit / RequestTimeout
│   ├── exception_handlers.py
│   └── v1/                  # auth, users, orders, events, zones, buyer_info, webhook
├── core/                    # config(fail-closed 驗證)、security、redis、logging、
│                            #   singleflight、*_metrics、exceptions   ※ 不依賴 FastAPI
├── crud/                    # 純資料存取
├── db/                      # engine / session(主 pool + cache bulkhead pool)/ optimistic
├── models/                  # ORM:order, event, seating, outbox, stripe_event, audit_log, …
├── schemas/                 # Pydantic request/response
├── services/                # orders(狀態機)、inventory(Lua)、seating + seat_runs(配位)、
│                            #   waiting_room + queue_events(等候室/SSE)、event_cache、
│                            #   abandoned_payments、pricing、zones、erasure、audit、pii、…
├── scripts/                 # create_admin、seed_venue、reconcile_inventory、
│                            #   rebuild_seat_runs、check_schema_drift、simulate_*
├── worker.py                # arq cron(見「背景任務」)
├── order_consumer.py        # 獨立行程:消費 orders:stream 寫 DB
└── main.py                  # app + lifespan + middleware 順序 + health

ticket_secrets/              # Rust crate(PyO3 + maturin)
alembic/                     # 遷移(分區、BIGINT、EXCLUDE、version 都在這)
loadtest/                    # k6:order_flow / queue_flow / cache_stampede + CHECKLIST.md
monitoring/                  # prometheus.yml + alerts.yml
infra/                       # Terraform(AWS Seoul)+ README + RUNBOOK(DR 演練)
test/                        # pytest
.github/workflows/           # test.yml(測試 + schema 守門 + terraform validate)、deploy.yml
```

---

## 本地開發

### 前置

- Docker + Docker Compose
- (本機跑非容器版才需要)Python 3.12 + Rust toolchain + maturin

### 步驟 1 — 建立 `.env`

PII 金鑰必須是 **base64 編碼的 32 bytes**:

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"   # 跑兩次,各取一個
```

```bash
DATABASE_URL=postgresql+asyncpg://justinhu@localhost:5432/testdb
REDIS_URL=redis://localhost:6380/0
SECRET_KEY=<至少 32 字元的隨機字串>
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
PII_KEK_BASE64=<第一個>
PII_LOOKUP_KEY_BASE64=<第二個>

# 以下三個只在 DEBUG 下允許;任一在非 DEBUG 下設定,app 拒絕啟動
DEBUG=true
ENABLE_MOCK_PAYMENT=true          # /orders/{id}/pay 模擬付款
LOADTEST_BYPASS_ADMISSION=true    # 跳過等候室直接下單(demo 用;想走完整流程就拿掉)
```

### 步驟 2 — 啟動

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
```

會起 9 個容器:`api`(:8000,4 workers)、`worker`(arq)、`order-consumer`、`db`(:5432)、`redis`(:6380)、`prometheus`(:9090)、`grafana`(:3000)、`otel-collector`(:4318)、`tempo`(:3200)。

```bash
curl -i localhost:8000/health/deps   # 200 {"status":"ok"} → DB 與 Redis 都通
docker compose logs -f worker order-consumer
```

### 步驟 3 — 造 admin、建活動並發佈

```bash
docker compose exec api python -m app.scripts.create_admin admin adminpass

TOKEN=$(curl -s -X POST localhost:8000/v1/auth/token \
  -d 'username=admin&password=adminpass' | jq -r .access_token)

# 自由座活動(草稿)
curl -X POST localhost:8000/v1/events/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Concert","venue":"Taipei Arena",
       "starts_at":"2026-12-01T19:00:00+00:00","ends_at":"2026-12-01T22:00:00+00:00",
       "sale_starts_at":"2026-01-01T00:00:00+00:00","sale_ends_at":"2026-12-01T18:00:00+00:00",
       "total_seats":50000,"price_cents":1500}'

# 發佈(假設 id=1)→ 灌庫存進 Redis、固定抽籤 salt
curl -X POST localhost:8000/v1/events/1/publish -H "Authorization: Bearer $TOKEN"
```

對號座活動:先 `python -m app.scripts.seed_venue` 建示範場館,建活動時改帶 `venue_id` 與 `zone_prices`(`{zone_id: price_cents}`)。

### 步驟 4 — 跑一遍買家流程

```bash
curl -X POST localhost:8000/v1/users/ -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"secret123"}'
TOKEN=$(curl -s -X POST localhost:8000/v1/auth/token \
  -d 'username=alice&password=secret123' | jq -r .access_token)

KEY=$(uuidgen)
curl -X POST localhost:8000/v1/orders/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $KEY" -H "Admission-Token: bypass" \
  -d '{"event_id":1,"quantity":2}'            # 202;對號座再加 "zone_id"

curl localhost:8000/v1/orders/by-key/$KEY -H "Authorization: Bearer $TOKEN"   # processing → ready
curl -X POST localhost:8000/v1/orders/<id>/pay -H "Authorization: Bearer $TOKEN"  # 模擬付款 → CONFIRMED
```

`Admission-Token` header 是必填欄位,`LOADTEST_BYPASS_ADMISSION=true` 時值不檢查。想走**真正的等候室**:拿掉該旗標,建活動時把 `sale_starts_at` 設在幾分鐘後,`POST /events/1/queue` 登記,輪詢 `…/queue/status` 直到 `admitted=true`,把回傳的 `access_token` 放進 `Admission-Token`。

### (選用)本機非容器跑法

```bash
docker compose up -d db redis
pip install -r requirements.txt
(cd ticket_secrets && maturin develop)
alembic upgrade head
uvicorn app.main:app --reload              # 終端 A
arq app.worker.WorkerSettings              # 終端 B
python -m app.order_consumer               # 終端 C
```

---

## 測試與 CI

```bash
pytest test/ -v
```

Argon2 在測試裡以替身取代,只有 `test_password_hashing.py` 跑真的;沒這樣做之前它佔整個套件七成時間。

GitHub Actions `test.yml` 在 `feat/*`、`dev` push 與 PR 時跑;`main` 刻意不列,它由 `deploy.yml` 以 `workflow_call` 呼叫,測試是部署的第一關而不是平行的工作流:

1. Postgres 16 + Redis 7 service container;maturin 建 `ticket_secrets` wheel
2. `alembic upgrade head` → **`alembic check`** → sequence 型別檢查 → **`check_schema_drift`**
3. `pytest` 帶覆蓋率門檻
4. 另一個 job:`terraform fmt -check` + `terraform validate`

---

## 壓力測試

`loadtest/` 內含針對搶票場景的 **k6** 腳本與上線前 checklist([`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md))。

```bash
k6 run -e MODE=capacity loadtest/order_flow.js   # A 型:固定速率,驗「守得住」(CI 守門)
k6 run loadtest/order_flow.js                     # B 型:爬升找拐點
k6 run loadtest/queue_flow.js                     # 等候室:登記 + 輪詢,驗放行計量
k6 run loadtest/cache_stampede.js                 # 冷 key 同時打一波;量測在 measure_cache_stampede.py
```

- **Open model(arrival-rate)**:固定到達率施壓,不被 server 變慢拖累。
- **Thresholds**:p95/p99、`http_req_failed`、`dropped_iterations` 自動判定,FAIL 即非零 exit。
- **409 不是錯誤**:`setResponseCallback` 把售完排除在失敗之外。
- **壓測後驗三鐵律**:賣出 ≤ 庫存、Redis 不為負、Redis + Postgres 守恆。
- 已驗過:A 型零超賣、等候室放行計量、跨 process 的 stampede 防護。

---

## 監控

- 本地:`/metrics` → Prometheus(`monitoring/prometheus.yml`)→ Grafana;告警規則在 [`monitoring/alerts.yml`](monitoring/alerts.yml)(座位 CAS 視窗、重試率、耗盡)。
- 本地追蹤:span → `otel-collector`(`monitoring/otel-collector.yml`)→ Tempo(`monitoring/tempo.yml`,48h)→ Grafana **Explore** 選 Tempo datasource(provisioning 自動建),用回應的 `X-Request-Id` 當 trace id 查。
- AWS:JSON log → CloudWatch metric filter → alarm;worker 以 gauge 回報積壓、死信、`sale_imminent`。
- 應用層 `alert(...)` 是結構化 log 事件,不是另一條管線;所有「需要人」的情況(outbox 死信、drift、退款失敗、資源不存在)都走這裡。

---

## 部署

**映像**:多階段 `Dockerfile`:rust-builder 編 wheel → python-builder 裝依賴進 venv → `python:3.12-slim` runtime,非 root 執行。

**基礎設施**([`infra/`](infra/README.md)):Terraform,AWS Seoul。ECS Fargate 三個服務(api / worker / order-consumer)+ RDS Postgres + ElastiCache Redis + ALB + Secrets Manager + CloudWatch。原則是 **apply → learn → destroy**,RDS 有刪除保護所以 destroy 是兩步;ECR 放在獨立 state 的 `bootstrap/`,因為映像倉庫是重建環境的輸入而不是環境的一部分。GitHub OIDC provider 以 data source 引用而非擁有,destroy 不會弄壞同帳號其他專案。consumer 可依佇列深度自動擴容(旗標開啟,因為需要的 IAM 動作只有 apply 時才會暴露缺少),worker 永不擴容。

**CD**(`deploy.yml`,push `main`):OIDC 取 15 分鐘短效憑證,沒有長期 key → 建映像推 ECR → 若 `alembic/versions/` 相對上次部署有變,先拍 RDS snapshot → 以 worker task def 跑一次性 migration task,失敗即停 → 以 **SHA 釘版**的 task def roll 三個服務(不用 `--force-new-deployment`)→ `wait services-stable` 後再 `describe-services` 核對線上 ARN(waiter 回 0 不等於部署成功)→ 透過 ALB 打 `/health/deps` smoke test。ECS deployment circuit breaker 開 rollback。

**DR**([`infra/RUNBOOK.md`](infra/RUNBOOK.md)):快照還原與 PITR 兩個情境都真機演練過並記下 RTO;演練找出的七個問題(含原本的還原指令其實是空操作)與修正都在裡面。
