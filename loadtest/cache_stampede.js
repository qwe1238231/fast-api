// 冷 key burst:量 event meta 快取的 stampede 防護在真的多 process(uvicorn --workers 4)
// 下擋掉多少 DB 讀。這支只負責「同時打一波」,量測本身在 loadtest/measure_cache_stampede.py
// (它在 burst 前把 key 弄冷、前後各讀一次 pg_stat_statements)。
//
// executor 用 per-vu-iterations、每個 VU 一發:k6 在 t=0 把所有 VU 一起拉起來,是最接近
// 「開賣瞬間大家同時按下去」的形狀。ramping/constant-arrival-rate 會把請求攤開,
// 大部分會落在 key 已經暖了之後,量到的就不是 stampede。
import http from 'k6/http';
import { check } from 'k6';
import { vu } from 'k6/execution';
import { SharedArray } from 'k6/data';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const tokens = new SharedArray('tokens', () => JSON.parse(open('./tokens.json')));

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const EVENT_ID = Number(__ENV.EVENT_ID || 1);
const BURST = Number(__ENV.BURST || 300);
const MODE = __ENV.MODE || 'burst';

// 202(受理)與 409(賣完 / 限購)都是正確回應;快取讀發生在庫存判斷**之前**,
// 所以就算全部 409,量到的 DB 讀數一樣有效。
http.setResponseCallback(http.expectedStatuses(202, 409));

const SCENARIOS = {
    // 開賣瞬間:所有 VU 同時各打一發。
    burst: {
        executor: 'per-vu-iterations',
        vus: BURST,
        iterations: 1,
        maxDuration: '30s',
    },
    // 穩態流量下反覆把 key 弄冷(由 measure 腳本在旁邊 DEL):定速打一段時間。
    churn: {
        executor: 'constant-arrival-rate',
        rate: Number(__ENV.RATE || 400),
        timeUnit: '1s',
        duration: __ENV.DURATION || '6s',
        preAllocatedVUs: 200,
        maxVUs: 600,
    },
};

export const options = {
    scenarios: { main: SCENARIOS[MODE] || SCENARIOS.burst },
    thresholds: {
        'http_req_failed': ['rate<0.01'],     // 只抓 5xx / timeout
    },
};

export default function () {
    const token = tokens[(vu.idInTest - 1) % tokens.length];
    const res = http.post(
        `${BASE_URL}/v1/orders/`,
        JSON.stringify({ event_id: EVENT_ID, quantity: 1 }),
        {
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`,
                'Idempotency-Key': uuidv4(),
                'Admission-Token': 'loadtest',   // LOADTEST_BYPASS_ADMISSION=True 時不會被看
            },
            tags: { name: 'create_order' },
        },
    );
    check(res, { 'accepted (202) or refused (409)': (r) => r.status === 202 || r.status === 409 });
}
