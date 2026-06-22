import http from 'k6/http';
import { check } from 'k6';
import { vu } from 'k6/execution';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const TOTAL_USERS = 200;    // 200 個 user 同時搶

http.setResponseCallback(
    http.expectedStatuses(200, 201, 409),
);

const MODE = __ENV.MODE || 'knee';

const scenario = MODE === 'capacity'
    ? {     
        executor: 'constant-arrival-rate',
        rate: 800,
        timeUnit: '1s',
        duration: '60s',
        preAllocatedVUs: 300,
        maxVUs: 800,
        }
     : {
        executor: 'ramping-arrival-rate',
        startRate: 50,
        timeUnit: '1s',
        preAllocatedVUs: 100,
        maxVUs: 500,             // 工人池放大,避免高 rate 時不夠用
        stages: [
            { target: 200,  duration: '15s' },
            { target: 1000, duration: '15s' },
            { target: 3000, duration: '15s' },
            { target: 3000, duration: '15s' },
        ],
    };

const thresholds = {
    'http_req_duration{name:create_order}': ['p(95)<500', 'p(99)<1000'],
    'http_req_failed': ['rate<0.01'],
};

if (MODE === 'capacity') {
    thresholds['dropped_iterations'] = ['count<50'];
}

export const options = {
    setupTimeout: '300s',
    thresholds,
    scenarios: { main: scenario },
};


const BASE_URL = 'http://localhost:8000';
const PASSWORD = 'testpass123';


function randomUsername() {
    return 'lt_' + Math.random().toString(36).slice(2, 12);
}


export function setup() {
    console.log(`Setup: preparing ${TOTAL_USERS} tokens (sequential)...`);
    const tokens = [];
    
    for (let i = 0; i < TOTAL_USERS; i++) {
        const username = randomUsername();
        
        const r1 = http.post(
            `${BASE_URL}/v1/users/`,
            JSON.stringify({ username: username, password: PASSWORD }),
            { headers: { 'Content-Type': 'application/json' } },
        );
        if (r1.status !== 201) continue;
        
        const r2 = http.post(
            `${BASE_URL}/v1/auth/token`,
            { username: username, password: PASSWORD },
        );
        if (r2.status !== 200) continue;
        
        tokens.push(r2.json('access_token'));
    }
    
    console.log(`Setup done: ${tokens.length}/${TOTAL_USERS} tokens prepared`);
    return { tokens };
}


export default function (data) {
    // 每個 VU 用固定的 token（不重複）
    // vu.idInTest 是 1-indexed，所以減 1
    const token = data.tokens[(vu.idInTest - 1) % data.tokens.length];
    
    const res = http.post(
        `${BASE_URL}/v1/orders/`,
        JSON.stringify({ event_id: 1, quantity: 1 }),
        {
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`,
                'Idempotency-Key': uuidv4(),
            },
            tags: {
                name: 'create_order'
            },
        },
    );
    
    check(res, {
        'order 201 or 409': (r) => r.status === 201 || r.status === 409,
    });
    
    // 沒有 sleep！搶票是瞬間動作
}