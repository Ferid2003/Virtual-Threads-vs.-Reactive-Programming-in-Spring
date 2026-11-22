-- Deterministic seed: same rows on every machine and every run.
CREATE TABLE orders (
    id          bigint      PRIMARY KEY,
    customer_id bigint      NOT NULL,
    status      text        NOT NULL,
    total_cents bigint      NOT NULL,
    created_at  timestamptz NOT NULL
);

INSERT INTO orders
SELECT g,
       1 + (g * 7919) % 100000,
       (ARRAY['NEW', 'PAID', 'SHIPPED', 'DELIVERED', 'CANCELLED'])[1 + g % 5],
       100 + (g * 31337) % 100000,
       timestamptz '2025-01-01 00:00:00+00' + make_interval(secs => g * 30)
FROM generate_series(1::bigint, 1000000) AS g;

VACUUM ANALYZE orders;
