import http from 'k6/http';
import { check } from 'k6';
import { scenario } from 'k6/execution';
import { Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

// 離線 seed 出來的 bearer token（loadtest/seed.py 產生）。SharedArray 全 VU 共用
// 一份,不會每個 VU 各複製一份整陣列 -> 避免 VU 一多就 OOM。
const tokens = new SharedArray('tokens', () => JSON.parse(open('./tokens.json')));

// A2 開關:'real' 走真實 verify_admission,送 loadtest/admission.json 裡與 tokens.json
// 同序配對的單次 admission token;'bypass'(預設)送假值,靠 LOADTEST_BYPASS_ADMISSION 跳過。
const ADMISSION = __ENV.ADMISSION || 'bypass';
const admissions = ADMISSION === 'real'
    ? new SharedArray('admissions', () => JSON.parse(open('./admission.json')))
    : null;

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const EVENT_ID = Number(__ENV.EVENT_ID || 1);

// 旁路模式下 POST /orders 只會回兩種碼:
//   202 -> 接受(座位已原子預約 + 入 stream,worker 稍後寫 DB)
//   409 -> InsufficientInventory,賣完(這是【正確的拒絕】,不是錯誤)
// 兩者都列為 expected,http_req_failed 才只計真正的錯(5xx / timeout)。
http.setResponseCallback(http.expectedStatuses(202, 409));

const accepted = new Counter('orders_accepted');   // 202 計數
const soldOut = new Counter('orders_sold_out');     // 409 計數

const MODE = __ENV.MODE || 'knee';

const SCENARIOS = {
    // 有界的正確性 smoke:固定總請求數,本機即可跑。搭配小庫存驗不超賣。
    smoke: {
        executor: 'shared-iterations',
        vus: 50,
        iterations: Number(__ENV.ITERATIONS || 2000),
        maxDuration: '60s',
    },
    // 拐點:開放模型逐步拉高到達率,找 p99 爆掉/開始 drop 的點(需獨立壓力機才準)。
    knee: {
        executor: 'ramping-arrival-rate',
        startRate: 50,
        timeUnit: '1s',
        preAllocatedVUs: 100,
        maxVUs: 800,             // 工人池放大,避免高 rate 時 VU 不夠 -> dropped_iterations
        stages: [
            { target: 200,  duration: '15s' },
            { target: 1000, duration: '15s' },
            { target: 3000, duration: '15s' },
            { target: 3000, duration: '15s' },
        ],
    },
    // 定速容量:固定到達率壓一段時間,看單台能否穩住(需獨立壓力機才準)。
    capacity: {
        executor: 'constant-arrival-rate',
        rate: 800,
        timeUnit: '1s',
        duration: '60s',
        preAllocatedVUs: 300,
        maxVUs: 800,
    },
};

const scenarioCfg = SCENARIOS[MODE] || SCENARIOS.knee;

const thresholds = {
    'http_req_duration{name:create_order}': ['p(95)<500', 'p(99)<1000'],
    'http_req_failed': ['rate<0.01'],   // 202/409 不算 fail,只抓 5xx/timeout
};
if (MODE === 'capacity') {
    thresholds['dropped_iterations'] = ['count<50'];
}

export const options = {
    scenarios: { main: scenarioCfg },
    thresholds,
};

export default function () {
    const i = scenario.iterationInTest;
    // real 模式:admission token 單次使用,必須按唯一 index 取(不可 modulo 重複用),
    // 且 bearer 要與之配對(同一 user);bypass 模式可循環用帳號。
    const token = ADMISSION === 'real' ? tokens[i] : tokens[i % tokens.length];
    const admissionToken = ADMISSION === 'real' ? admissions[i] : 'loadtest';

    const res = http.post(
        `${BASE_URL}/v1/orders/`,
        JSON.stringify({ event_id: EVENT_ID, quantity: 1 }),
        {
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`,
                'Idempotency-Key': uuidv4(),
                'Admission-Token': admissionToken,
            },
            tags: { name: 'create_order' },
        },
    );

    if (res.status === 202) accepted.add(1);
    else if (res.status === 409) soldOut.add(1);

    check(res, {
        'accepted (202) or sold out (409)': (r) => r.status === 202 || r.status === 409,
    });
    // 沒有 sleep!搶票是瞬間動作。
}
