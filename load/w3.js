// W3: mixed traffic. ENRICHED_RATIO of requests hit /enriched (default 0.5), the rest hit /orders/{id}.
import { enriched, order } from './lib.js';
export { options } from './lib.js';
const RATIO = Number(__ENV.ENRICHED_RATIO || 0.5);
export default () => (Math.random() < RATIO ? enriched() : order());
