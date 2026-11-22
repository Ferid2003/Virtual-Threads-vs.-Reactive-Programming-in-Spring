# Spring MVC (platform / virtual threads) vs WebFlux benchmark

| Variant        | Service           | Host port | Stack                                              |
|----------------|-------------------|-----------|----------------------------------------------------|
| `mvc-platform` | `mvc-service`     | 8081      | Tomcat, platform threads, JDBC + HikariCP (50)     |
| `mvc-virtual`  | `mvc-service`     | 8082      | same image, `SPRING_THREADS_VIRTUAL_ENABLED=true`  |
| `webflux`      | `webflux-service` | 8083      | Netty, R2DBC + r2dbc-pool (50)                     |
| stub           | `stub-service`    | 8090      | WebFlux, non-blocking `STUB_DELAY_MS` delay (50 ms default) |

Endpoints (identical in both apps):
- `GET /orders/{id}`: one primary-key lookup (W2, DB-bound).
- `GET /orders/{id}/enriched`: the lookup, then `GET stub/customers/{customerId}` (W1/W3, mixed I/O).
- Unknown id: `404`, empty body.

## Run

Requires Docker with Compose v2 and k6.

```sh
cd docker
docker compose up -d --build                   # postgres (seeds 1M rows, ~5 s), stub, all three variants
../load/verify-identical.sh                    # byte-compares status, Content-Type and body across variants
k6 run -e BASE_URL=http://localhost:8081 ../load/smoke.js   # repeat for 8082, 8083
docker compose down -v
```

For benchmarking, start **only one** variant at a time: `docker compose up -d mvc-virtual`. Its dependencies start with it.

Limits and JDK come from `docker/.env` and can be overridden per command:
`JDK=25 APP_CPUS=1 docker compose up -d --build webflux`.

## Fairness decisions

- **One code shape.** The records, SQL string (`:id` named parameter in both `JdbcClient` and `DatabaseClient`) and JSON shape are copied character for character between `MvcApplication.java` and `WebfluxApplication.java`. `load/verify-identical.sh` enforces this.
- **Same stub-client limits.** Both apps read one identical `stub.*` block in `application.properties`; see the table below.
- **Same DB pool.** Both pools are fixed at 50 (Hikari min=max=50; r2dbc initial=max=50), with a 30 s acquire timeout and a 30 min max lifetime.
- **Same JVM.** Fixed heap (`-Xms=-Xmx`) and G1 pinned, so ergonomics can't switch to SerialGC at 1 CPU. The jar is compiled once on JDK 21 (`release 21`); only the runtime image changes with `JDK`.
- **Same container limits** (`APP_CPUS`, `APP_MEM`) for every variant. Postgres and the stub get their own limits so they don't become the bottleneck.
- **Deterministic data.** `init.sql` builds the rows arithmetically from the id, with no randomness.

## Stated variable: HTTP client

The study compares the programming models as people actually use them, so each stack uses its usual client
for the stub call. This choice is part of the treatment, not a controlled constant:

| | MVC (both thread variants) | WebFlux |
|---|---|---|
| API | `RestClient` | `WebClient` |
| Client | JDK `java.net.http.HttpClient`, blocking `send()` | Reactor Netty, non-blocking |
| Protocol | HTTP/1.1 (forced; the default HTTP/2 would try an h2c upgrade) | HTTP/1.1 |
| Max connections (`stub.max-connections`) | 1000: fair `Semaphore` around each call (the JDK client has no cap of its own) | 1000: `ConnectionProvider.maxConnections` |
| Waiting for a connection | FIFO, unbounded, `stub.acquire-timeout` = 30 s | FIFO, unbounded (`pendingAcquireMaxCount=-1`), `pendingAcquireTimeout` = 30 s |
| Connect timeout (`stub.connect-timeout`) | 2 s | 2 s |
| Response timeout (`stub.response-timeout`) | 10 s (`setReadTimeout`: until response headers) | 10 s (`responseTimeout`: between reads) |
| Idle keep-alive (`stub.idle-timeout`) | 30 s (`jdk.httpclient.keepalive.timeout`) | 30 s (`maxIdleTime`) |

Checked with the cap lowered to 5 and 50 concurrent requests: all three variants queued in the same 50 ms
waves (min ≈66 ms, max ≈565 ms, no errors).

## Threats to validity

- **Response framing.** Tomcat sends `Transfer-Encoding: chunked`, while Netty sends `Content-Length`. The body bytes are identical (enforced by `load/verify-identical.sh`); the framing adds a few bytes per response on MVC.
- **HTTP client** (see above). The two clients also differ in internal threading: the JDK client runs a selector thread plus a small executor, while Reactor Netty shares WebFlux's event loops. MVC's connection permit is released when the response headers arrive; the ~100-byte body is read immediately afterwards.
- **Response-timeout semantics** differ slightly between the clients (see table). With a 50 ms stub and a 10 s limit, neither should ever fire. A timeout would show up in the error rate.
