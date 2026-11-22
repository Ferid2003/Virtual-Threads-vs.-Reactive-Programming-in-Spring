// Low-load smoke test: correctness, not performance. Fails (non-zero exit) on any failed check or error.
//   k6 run -e BASE_URL=http://localhost:8081 load/smoke.js
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8081';
const ROWS = 1000000;

export const options = {
  scenarios: {
    smoke: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 5),
      timeUnit: '1s',
      duration: __ENV.DURATION || '10s',
      preAllocatedVUs: 5,
      maxVUs: 20,
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate==0'],
    dropped_iterations: ['count==0'],
  },
};

export default function () {
  const id = 1 + Math.floor(Math.random() * ROWS);

  const order = http.get(`${BASE_URL}/orders/${id}`, { tags: { name: 'order' } });
  check(order, {
    'order 200': (r) => r.status === 200,
    'order id matches': (r) => r.json('id') === id,
  });

  const enriched = http.get(`${BASE_URL}/orders/${id}/enriched`, { tags: { name: 'enriched' } });
  check(enriched, {
    'enriched 200': (r) => r.status === 200,
    'enriched order matches': (r) => r.json('order.id') === id,
    'customer matches order': (r) => r.json('customer.id') === r.json('order.customerId'),
    // Sequential DB + 50 ms stub call; anything faster means the stub was skipped.
    'enriched >= stub delay': (r) => r.timings.duration >= Number(__ENV.STUB_DELAY_MS || 50),
  });
}
