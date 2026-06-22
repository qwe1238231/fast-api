import http from 'k6/http';
import {sleep, check} from 'k6';

export const options = {
    vus:50,
    duration: '30s',
};

export default function () {
    const res = http.get('http://localhost:8000');

    check(res,{
        'status is 200 or 307': (r) => r.status ===200 || r.status === 307,
    });
    
    sleep(Math.random() *2 +1 );
}