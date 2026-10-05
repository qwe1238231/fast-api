import http from 'k6/http';
import { check, sleep } from 'k6';
import { vu } from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';

// 離線 seed 的 bearer token（loadtest/seed.py）。全 VU 共用一份。
const tokens = new SharedArray('tokens', () => JSON.parse(open('./tokens.json')));

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const EVENT_ID = Number(__ENV.EVENT_ID);                 // 必填:setup_event_b.py 印出
const POLL_INTERVAL = Number(__ENV.POLL_INTERVAL || 2);  // 秒;真實 5,壓測縮短加速觀察
const VUS = Number(__ENV.VUS || 1000);

// join/poll 正常都回 200;登記窗關了才 409。兩者列 expected,http_req_failed 只抓真錯。
http.setResponseCallback(http.expectedStatuses(200, 409));

const joinOk = new Counter('queue_join_ok');
const joinRejected = new Counter('queue_join_rejected');   // 409:窗外
const admittedC = new Counter('queue_admitted');            // 每個 VU 只計一次
const soldOutC = new Counter('queue_sold_out');
const joinDur = new Trend('queue_join_duration', true);
const pollDur = new Trend('queue_poll_duration', true);

// 每個 VU 獨立(k6 VU = 獨立 JS runtime):拿到票/賣完就標記,之後閒置不再打。
let done = false;

export const options = {
    scenarios: {
        waiters: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: [
                { target: VUS, duration: __ENV.RAMP || '5s' },   // 進場洪峰
                { target: VUS, duration: __ENV.HOLD || '40s' },  // 撐住 + 觀察 admission
            ],
            gracefulStop: '5s',
        },
    },
    thresholds: {
        'queue_poll_duration': ['p(95)<200', 'p(99)<500'],   // Redis-only 熱路徑,該很快
        'queue_join_duration': ['p(95)<500'],
        'http_req_failed': ['rate<0.01'],
    },
};

function authHeader() {
    return { Authorization: `Bearer ${tokens[(vu.idInTest - 1) % tokens.length]}` };
}

export default function () {
    if (done) { sleep(POLL_INTERVAL); return; }   // 已 admitted/sold_out -> 離開佇列,閒置

    const headers = authHeader();

    if (vu.iterationInInstance === 0) {
        // 進場登記(冪等,每個 user 一次)。
        const r = http.post(`${BASE_URL}/v1/events/${EVENT_ID}/queue`, null, {
            headers, tags: { name: 'queue_join' },
        });
        joinDur.add(r.timings.duration);
        if (r.status === 200) joinOk.add(1); else joinRejected.add(1);
        check(r, { 'join 200': (res) => res.status === 200 });
    } else {
        // 輪詢等候狀態(Redis-only,無 rate limit)。
        const r = http.get(`${BASE_URL}/v1/events/${EVENT_ID}/queue/status`, {
            headers, tags: { name: 'queue_poll' },
        });
        pollDur.add(r.timings.duration);
        check(r, { 'poll 200': (res) => res.status === 200 });
        if (r.status === 200) {
            const body = r.json();
            if (body.admitted && body.access_token) { admittedC.add(1); done = true; }
            else if (body.sold_out) { soldOutC.add(1); done = true; }
        }
    }
    sleep(POLL_INTERVAL);
}
