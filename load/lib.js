// Shared open-model load profile for w1/w2/w3. Env vars:
//   BASE_URL  target, e.g. http://localhost:8081            RATE      requests/s (default 100)
//   DURATION  e.g. 120s (default 60s)                       TIMEOUT   per-request timeout (default 10s)
//   VUS / MAX_VUS  pre-allocated / max VUs. Too few shows up as dropped_iterations, i.e. the target fell behind.
import http from 'k6/http';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8081';
const RATE = Number(__ENV.RATE || 100);
const MAX_VUS = Number(__ENV.MAX_VUS || 2000);
const ROWS = 1000000;
const PARAMS = { timeout: __ENV.TIMEOUT || '10s' };

export const options = {
  discardResponseBodies: true, // correctness is checked by smoke.js / verify-identical.sh; keeps k6 cheap
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: Number(__ENV.VUS || Math.min(MAX_VUS, Math.max(50, Math.ceil(RATE / 5)))),
      maxVUs: MAX_VUS,
    },
  },
  // Empty thresholds make k6 export per-endpoint sub-metrics in the summary (needed for W3).
  thresholds: {
    'http_req_duration{name:order}': [],
    'http_req_duration{name:enriched}': [],
    'http_req_failed{name:order}': [],
    'http_req_failed{name:enriched}': [],
  },
};

const randomId = () => 1 + Math.floor(Math.random() * ROWS);

export const order = () => http.get(`${BASE_URL}/orders/${randomId()}`, { ...PARAMS, tags: { name: 'order' } });
export const enriched = () => http.get(`${BASE_URL}/orders/${randomId()}/enriched`, { ...PARAMS, tags: { name: 'enriched' } });
