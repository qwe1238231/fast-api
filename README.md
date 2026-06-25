# Ticket System API（搶票系統）

高併發售票系統的後端 API。核心目標:**在「十萬人搶五萬張票」的尖峰下,絕不超賣、優雅降級**。

技術重點不只在「能跑」,而在**正確性、安全性、可觀測性與可驗證的容量**:Redis 原子庫存保證不超賣、Rust 實作的密碼學原語、PII 信封加密、refresh token 輪替與重用偵測、Redis Stream 稽核管線,以及一套親手驗證過的 k6 壓測方法論。

---

## 目錄

- [技術棧](#技術棧)
- [系統架構](#系統架構)
- [核心設計](#核心設計)
  - [1. 搶票庫存 — Redis 原子計數,絕不超賣](#1-搶票庫存--redis-原子計數絕不超賣)
  - [2. 訂單狀態機](#2-訂單狀態機)
  - [3. 冪等性](#3-冪等性)
  - [4. 認證 — JWT + Refresh Token 輪替](#4-認證--jwt--refresh-token-輪替)
  - [5. PII 信封加密 + Rust 密碼學模組](#5-pii-信封加密--rust-密碼學模組)
  - [6. 稽核日誌 — Redis Stream 管線](#6-稽核日誌--redis-stream-管線)
  - [7. 背景任務](#7-背景任務)
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
| Web 框架 | **FastAPI** 0.136 / Starlette / **Uvicorn**(多 worker) |
| 語言 | **Python 3.12**(async/await 全程非同步) |
| 密碼學原語 | **Rust**(Argon2id、AES-256-GCM、HMAC-SHA256),經 **PyO3 + maturin** 編成 Python 套件 |
| 資料庫 | **PostgreSQL 16** + **SQLAlchemy 2.0**(async)+ **asyncpg** + **Alembic** 遷移 |
| 快取 / 庫存 / 佇列 | **Redis 7**(原子庫存計數、冪等性、稽核 Stream、rate limit backing) |
| 背景任務 | **arq**(async task queue,cron 排程) |
| 認證 | **PyJWT**(HS256 access token)+ 自製 refresh token 輪替 |
| 金流 | **Stripe**(PaymentIntent + webhook 簽章驗證) |
| 限流 | **slowapi** |
| 資料驗證 | **Pydantic v2** / pydantic-settings |
| 可觀測性 | **Prometheus**(prometheus-fastapi-instrumentator)+ **Grafana** |
| 壓測 | **k6**(open-model arrival-rate、thresholds、A/B 模式) |
| 容器 | **Docker** 多階段建置(Rust builder → Python builder → 非 root runtime) |
| CI | **GitHub Actions**(Postgres/Redis service + Rust 建置 + Alembic + pytest) |

---

## 系統架構

```
                         ┌──────────────────────────────────────┐
       HTTP              │             FastAPI (api)              │
  ───────────────▶  ALB  │  Uvicorn × N workers                  │
                         │  ┌────────────────────────────────┐   │
                         │  │ api/      路由 + DI + 例外處理   │   │
                         │  │ services/ 業務邏輯(狀態機/庫存)│   │
                         │  │ crud/     純資料存取             │   │
                         │  │ core/     設定/安全/Redis        │   │
                         │  └────────────────────────────────┘   │
                         └───────┬──────────────────┬────────────┘
                                 │                  │
                    ┌────────────▼──────┐   ┌───────▼─────────────┐
                    │   PostgreSQL 16   │   │       Redis 7       │
                    │  訂單/活動/使用者 │   │  庫存計數(原子)    │
                    │  refresh token    │   │  冪等性 claim        │
                    │  buyer_info(PII) │   │  稽核 Stream         │
                    │  audit_logs       │   └───────┬─────────────┘
                    └────────────▲──────┘           │ XREADGROUP
                                 │ batch insert      │
                         ┌───────┴───────────────────▼───────────┐
                         │          arq worker                    │
                         │  • 過期 pending 訂單(每分鐘,釋放庫存)│
                         │  • 消費稽核 Stream → 批次寫 Postgres   │
                         │  • 清理過期 refresh token / 稽核日誌   │
                         └────────────────────────────────────────┘

  可觀測性:  /metrics ──▶ Prometheus ──▶ Grafana
```

**分層原則**:`core/` 不依賴 FastAPI;`api/` 負責所有框架轉接(`Depends`、`HTTPException`、`oauth2_scheme`);`services/` 放業務規則(狀態機、庫存、信封加密);`crud/` 只做純資料存取。

---

## 核心設計

### 1. 搶票庫存 — Redis 原子計數,絕不超賣

庫存的即時計數放在 Redis,用**單執行緒原子 `DECRBY`** 處理併發扣減 —— 十萬個請求會被 Redis **天生序列化**,每個拿到獨一無二的扣減後數值,**不可能兩人同時搶到最後一張**,也不需要任何鎖。

```python
# app/services/inventory.py
async def reserve(redis, *, event_id, quantity):
    remaining = await redis.decrby(_key(event_id), quantity)
    if remaining < 0:                          # 超賣 → 補回去、拒絕
        await redis.incrby(_key(event_id), quantity)
        raise InsufficientInventory(...)        # → HTTP 409
```

**已實測驗證**:71,691 個請求搶 50,000 張票 → 正好賣出 50,000、Redis 剩 0、零超賣(見 [`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md))。

> **設計取捨**:Redis 計數換來高吞吐,代價是它不持久。Redis 重啟/failover 後須從 Postgres 重算(`剩餘 = total_seats − 已持有訂單量`)校正。完整的 reconcile / 暫停開賣 / 飄移偵測設計記錄在 [`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md) 附錄。

### 2. 訂單狀態機

下單即 `PENDING`,各狀態轉換在 service 層集中驗證,非法轉換一律 `409`:

```
            付款                  確認(webhook)
  PENDING ───────▶ PAID ──────────────▶ CONFIRMED   (終態)
     │               │
     │ 取消/逾時      │ 取消
     ▼               ▼
  EXPIRED        CANCELLED                            (終態)
  (釋放庫存)     (釋放庫存)
```

- 逾時(預設 10 分鐘未付款):由背景 worker 轉 `EXPIRED` 並**釋放庫存**回 Redis。
- `CANCELLED` / `EXPIRED` 都會把票還回庫存計數。

### 3. 下單非同步化 + 冪等性

搶票主路徑**不在請求內寫 DB**:一個原子 Lua script 一口氣完成「去重 + 扣庫存 + 入列 + 寫 claim」,然後立刻回 **202 Accepted**;真正的 `orders` INSERT 由背景 worker 從 Redis Stream(`orders:stream`)消費後寫入。這把搶票時最慢的同步 DB 寫入移出請求路徑。

- **冪等**:請求帶 `Idempotency-Key` header(UUID)。claim(`idempotency:{key}`)在原子 script 內依此 key 去重,client 重試不會重複扣庫存或重複建單;DB 對 `orders.idempotency_key` 的 **UNIQUE 約束**讓 worker 端「重抄無害」(`ON CONFLICT` 等效)。
- **查詢**:client 拿 `Idempotency-Key` 輪詢 `GET /orders/by-key/{key}` → `processing`(漆帳中)/ `ready`(附訂單)/ `failed`(放棄並已退票)。
- **可靠性**:worker 崩潰、未 ack 的訊息由 reclaim(`XPENDING` + `XCLAIM`)重領;反覆失敗的毒訊息超過上限後進死信 stream、**退回庫存**、claim 標 `FAILED`。

### 4. 認證 — JWT + Refresh Token 輪替

- **Access token**:JWT(HS256),短效(預設 30 分鐘),無狀態。
- **Refresh token**:不透明隨機字串,**只存 SHA-256 雜湊**進 DB;HttpOnly cookie 限定 `/v1/auth` 路徑。
  - **輪替(rotation)**:每次 refresh 都發新 token、舊的標記 used。
  - **重用偵測(reuse detection)**:同一 family 的舊 token 若在 grace window 後再被使用 → 視為竊用,**撤銷整個 family**。
  - **CSRF**:double-submit cookie(cookie + `X-CSRF-Token` header 比對)。
- **登入防護**:slowapi 限流(預設 `5/minute`);找不到使用者時跑 dummy verify **抵禦 timing 列舉攻擊**;登入成功/失敗都寫稽核事件。
- `logout`(撤銷當前 family)/ `logout-all`(撤銷使用者全部 token)。

### 5. PII 信封加密 + Rust 密碼學模組

買家實名資料(身分證字號)採**信封加密(envelope encryption)**:

```
每筆資料 → 隨機 DEK(32 bytes)→ AES-256-GCM 加密明文
         DEK 本身 → 用主金鑰 KEK 再加密一次 → 兩者一起存 DB
查詢比對 → HMAC-SHA256 lookup hash(不需解密即可判斷「是否已存在」)
```

所有密碼學原語由 **Rust crate `ticket_secrets`** 實作(`app/core/security.py`、`app/services/pii.py` 呼叫),經 PyO3 綁定、maturin 編譯成 wheel:

| 函式 | 用途 | 演算法 |
|---|---|---|
| `hash_password` / `verify_password` | 密碼雜湊 | **Argon2id** |
| `aes_gcm_encrypt` / `aes_gcm_decrypt` | PII 加解密 | **AES-256-GCM** |
| `hmac_sha256` | 可查詢 lookup hash | **HMAC-SHA256** |

> 為什麼用 Rust:密碼學熱路徑用編譯語言實作,避開 Python GIL 與純 Python 實作的效能/安全疑慮;金鑰長度等不變式在 Rust 端強制檢查。

### 6. 稽核日誌 — Redis Stream 管線

寫入路徑不能因為「記稽核」而變慢,所以採**生產者-消費者**解耦:

```
API(emit_event)──XADD──▶ Redis Stream "audit:events"(~1ms,近即時)
                                  │
                  arq worker ──XREADGROUP(consumer group)──▶ 批次寫入 Postgres ──▶ XACK
```

請求端只付出一次 `XADD`(極快),真正落地由 worker 批次處理。Stream 用 consumer group 確保 at-least-once、`maxlen` 近似裁切防爆量;稽核資料保留 90 天後由 cron 清除(GDPR 資料最小化)。

### 7. 背景任務

`arq` worker(`arq app.worker.WorkerSettings`)排程:

| 任務 | 頻率 | 作用 |
|---|---|---|
| `expire_pending_orders` | 每分鐘 | 逾時未付款訂單轉 EXPIRED,釋放庫存 |
| `consume_audit_events` | 每分鐘 | 消費稽核 Stream,批次寫 Postgres |
| `purge_expired_refresh_tokens` | 每日 03:00 | 清理過期 refresh token |
| `purge_old_audit_logs` | 每日 02:30 | 清除超過保留期的稽核日誌 |

每筆訂單在獨立交易中處理 —— 單筆失敗不會中斷整批。

---

## 資料模型

| 表 | 重點欄位 |
|---|---|
| `users` | `username`(unique)、`hashed_password`(Argon2id)、`is_active` |
| `events` | `total_seats`、`price_cents`、`status`(draft/published/cancelled)、`sale_starts_at`/`sale_ends_at` 售票窗 |
| `orders` | `status`(狀態機)、`idempotency_key`(**unique**)、`quantity`、`total_price_cents`、`payment_provider_id`、各狀態時間戳 |
| `refresh_tokens` | `token_hash`、`family_id`、`used_at`、`revoked_at`、`expires_at`(sliding)、`absolute_expires_at`、`user_agent`、`ip_address` |
| `buyer_info` | `real_name`、`national_id_ciphertext`、`national_id_dek_encrypted`、lookup hash(PII 信封加密) |
| `audit_logs` | `event_type`、`actor_user_id`、`actor_ip`、`target_*`、`payload`(JSON)、`success`、`error_code` |

狀態欄位用 `SAEnum(native_enum=False)`(存字串,跨 DB 可攜)。Schema 由 Alembic 管理。

---

## API 端點

所有端點前綴 `/v1`。互動式文件:啟動後 `GET /` 會轉址到 `/docs`(Swagger UI)。

### Auth
| Method | Path | 說明 |
|---|---|---|
| POST | `/auth/token` | 帳密登入,回 access token + 設置 refresh/CSRF cookie(限流 5/min) |
| POST | `/auth/refresh` | 用 refresh cookie 換新 access token(輪替 + 重用偵測,限流 30/min) |
| POST | `/auth/logout` | 撤銷當前 token family |
| POST | `/auth/logout-all` | 撤銷使用者所有 token |

### Users
| Method | Path | 說明 |
|---|---|---|
| POST | `/users/` | 註冊(使用者名稱重複回 409) |
| GET | `/users/me` | 取得當前使用者 |

### Orders（搶票核心）
| Method | Path | 說明 |
|---|---|---|
| POST | `/orders/` | **下單搶票**(需 `Idempotency-Key` header;回 **202** 已受理、售完回 409) |
| GET | `/orders/by-key/{key}` | 用 `Idempotency-Key` 查狀態(processing / ready / failed） |
| GET | `/orders/me` | 我的訂單列表 |
| GET | `/orders/{id}` | 單筆訂單(非本人回 404) |
| POST | `/orders/{id}/pay` | 模擬付款(PENDING→PAID→CONFIRMED) |
| POST | `/orders/{id}/cancel` | 取消(釋放庫存) |
| POST | `/orders/{id}/payment-intent` | 建立 Stripe PaymentIntent,回 client_secret |

### Events（活動管理）
| Method | Path | 權限 | 說明 |
|---|---|---|---|
| POST | `/events/` | admin | 建立活動(草稿) |
| POST | `/events/{id}/publish` | admin | 草稿 → 開賣,**自動把庫存灌進 Redis** |
| POST | `/events/{id}/reconcile-inventory` | admin | 從 Postgres 重算、覆寫 Redis 庫存(Redis 遺失後的恢復) |
| GET | `/events/` | 公開 | 列出已開賣的活動 |
| GET | `/events/{id}` | 公開 | 單一活動 |

### Buyer Info / Webhooks
| Method | Path | 說明 |
|---|---|---|
| POST | `/buyer-info/` | 登記實名資料(PII 加密儲存) |
| GET | `/buyer-info/me` | 取得實名資料(解密回傳) |
| POST | `/webhooks/stripe` | Stripe webhook(驗簽;`payment_intent.succeeded` → 訂單轉 paid+confirmed) |

`GET /metrics` 由 Prometheus instrumentator 提供。

---

## 目錄結構

```
app/
├── api/
│   ├── deps.py            # DI:DbSession / Redis / CurrentUser / limiter / oauth2_scheme
│   ├── exception_handlers.py
│   └── v1/                # 路由:auth, users, orders, buyer_info, webhook
├── core/                  # config(pydantic-settings)、security、redis、exceptions
│                          #   ※ 不依賴 FastAPI
├── crud/                  # 純資料存取:order, user, refresh_token, buyer_info
├── db/                    # SQLAlchemy engine / session / Base
├── models/                # ORM 模型
├── schemas/               # Pydantic request/response
├── services/              # 業務邏輯:orders(狀態機)、inventory(庫存)、
│                          #   idempotency、audit、pii、buyer_info、stripe_client
├── worker.py              # arq 背景任務
└── main.py                # FastAPI app + lifespan + middleware

ticket_secrets/            # Rust crate(密碼學原語,PyO3 + maturin)
├── src/lib.rs
└── Cargo.toml

alembic/                   # 資料庫遷移
loadtest/                  # k6 壓測腳本 + 上線前 checklist
monitoring/                # Prometheus 設定
test/                      # pytest
.github/workflows/test.yml # CI
```

---

## 本地開發

### 前置

- Docker + Docker Compose
- (本機跑非容器版才需要)Python 3.12 + Rust toolchain + maturin

### 步驟 1 — 取得程式碼

```bash
git clone <repo-url>
cd fast-api
```

### 步驟 2 — 建立 `.env`

PII 金鑰必須是 **base64 編碼的 32 bytes**,先產生:

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"   # 跑兩次,各取一個
```

在專案根目錄建 `.env`:

```bash
DATABASE_URL=postgresql+asyncpg://justinhu@localhost:5432/testdb
REDIS_URL=redis://localhost:6380/0
SECRET_KEY=<至少 32 字元的隨機字串>
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
PII_KEK_BASE64=<上面產生的第一個>
PII_LOOKUP_KEY_BASE64=<上面產生的第二個>
```

### 步驟 3 — 啟動所有服務

```bash
docker compose up -d --build
```

會起 6 個容器:`api`(:8000)、`worker`(arq 背景任務)、`db`(Postgres :5432)、`redis`(:6380)、`prometheus`(:9090)、`grafana`(:3000)。

### 步驟 4 — 套用資料庫遷移

```bash
docker compose exec api alembic upgrade head
```

### 步驟 5 — 確認服務起來了

```bash
curl -i localhost:8000/docs        # 200 → API 活著
docker compose ps                  # 所有容器 healthy / running
docker compose logs -f worker      # 看到 arq 啟動 = worker 活著
```

開瀏覽器到 <http://localhost:8000/docs> 看互動式 API 文件。

### 步驟 6 — 造一個 admin、建活動並發佈

建立活動需要 admin 權限。先用 bootstrap 腳本造一個 admin:

```bash
docker compose exec api python -m app.scripts.create_admin admin adminpass
```

登入拿 admin 的 access token,然後**建活動 → 發佈**(發佈會自動把庫存灌進 Redis,不用再手動 seed):

```bash
TOKEN=$(curl -s -X POST localhost:8000/v1/auth/token \
  -d 'username=admin&password=adminpass' | jq -r .access_token)

# 建活動(草稿),記下回傳的 id
curl -X POST localhost:8000/v1/events/ \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Concert","venue":"Taipei Arena",
       "starts_at":"2026-12-01T19:00:00+00:00","ends_at":"2026-12-01T22:00:00+00:00",
       "sale_starts_at":"2026-01-01T00:00:00+00:00","sale_ends_at":"2026-12-01T18:00:00+00:00",
       "total_seats":50000,"price_cents":1500}'

# 發佈(假設 id=1)→ 自動 seed 庫存到 Redis
curl -X POST localhost:8000/v1/events/1/publish -H "Authorization: Bearer $TOKEN"
```

### 步驟 7 — 跑一遍完整流程

```bash
# 註冊(JSON)
curl -X POST localhost:8000/v1/users/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"secret123"}'

# 登入(form-encoded,OAuth2 規格)→ 取回 access_token
curl -X POST localhost:8000/v1/auth/token \
  -d 'username=alice&password=secret123'

# 搶票(帶 token + Idempotency-Key)
curl -X POST localhost:8000/v1/orders/ \
  -H "Authorization: Bearer <貼上 access_token>" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"event_id":1,"quantity":1}'
```

成功回 **`202`** + `idempotency_key`(已受理、處理中);售完回 `409`。
之後用 `GET /v1/orders/by-key/{idempotency_key}` 輪詢結果(`processing` / `ready` / `failed`)。

### (選用)本機非容器跑法

只用容器跑相依服務,API/worker 在本機跑(方便 debug、熱重載):

```bash
docker compose up -d db redis              # 只起 Postgres + Redis
pip install -r requirements.txt
(cd ticket_secrets && maturin develop)     # 編譯 Rust 模組到當前環境
alembic upgrade head
uvicorn app.main:app --reload              # 終端 A:API
arq app.worker.WorkerSettings              # 終端 B:背景 worker
```

---

## 測試與 CI

```bash
pytest test/ -v
```

GitHub Actions(`.github/workflows/test.yml`)在 `main` 與 `feat/*` push / PR 時:

1. 起 Postgres 16 + Redis 7 service container
2. 用 maturin 建置 `ticket_secrets` Rust crate 並安裝 wheel
3. `alembic upgrade head` 套遷移
4. `pytest`

---

## 壓力測試

`loadtest/` 內含一套針對搶票場景設計的 **k6** 壓測,以及上線前 checklist。

```bash
k6 run -e MODE=capacity loadtest/order_flow.js   # A型:固定速率,驗「守得住」(CI 守門)
k6 run loadtest/order_flow.js                     # B型:爬升找拐點(容量探測)
```

設計重點(完整方法論見 [`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md)):

- **Open model(arrival-rate)**:固定到達率施壓,不被 server 變慢拖累,還原真實搶票尖峰。
- **A/B 雙模式**:`constant-arrival-rate` 守門 vs `ramping-arrival-rate` 找拐點,用環境變數切換。
- **Thresholds**:`p95/p99` 延遲、`http_req_failed`、`dropped_iterations` 自動判定,FAIL 即非零 exit code,可塞進 CI。
- **正確性驗證**:壓測後查 Redis 剩餘 + Postgres 訂單,驗「賣出 ≤ 庫存、Redis 不為負、守恆」三鐵律。
- **`setResponseCallback`**:把 409(售完)排除在「失敗」之外 —— 它是正確行為,不是錯誤。

---

## 監控

`prometheus-fastapi-instrumentator` 在 `/metrics` 暴露 HTTP 指標,Prometheus 抓取(設定見 `monitoring/prometheus.yml`),Grafana(`:3000`)做視覺化。

---

## 部署

多階段 `Dockerfile`:

1. **rust-builder**:用 maturin 把 `ticket_secrets` 編成 wheel。
2. **python-builder**:裝 Python 依賴 + 上一步的 wheel 進 venv。
3. **runtime**:`python:3.12-slim`,只複製 venv 與程式碼,**非 root 使用者**執行。

Production 規劃(Singapore 區、ECS Fargate + RDS + ElastiCache)與容量擴張策略見 [`loadtest/CHECKLIST.md`](loadtest/CHECKLIST.md):API 為 stateless 可橫向擴張,真正的容量瓶頸在共用的 Postgres(連線數 → RDS Proxy;熱點讀 → 快取 event 設定)。
