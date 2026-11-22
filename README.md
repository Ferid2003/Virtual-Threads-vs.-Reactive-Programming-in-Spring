# Spring MVC (platform / virtual threads) vs WebFlux benchmark

| Variant        | Service           | Host port | Stack                                              |
|----------------|-------------------|-----------|----------------------------------------------------|
| `mvc-platform` | `mvc-service`     | 8081      | Tomcat, platform threads, JDBC + HikariCP (50)     |
| `mvc-virtual`  | `mvc-service`     | 8082      | same image, `SPRING_THREADS_VIRTUAL_ENABLED=true`  |
| `webflux`      | `webflux-service` | 8083      | Netty, R2DBC + r2dbc-pool (50)                     |
| stub           | `stub-service`    | 8090      | WebFlux, non-blocking `STUB_DELAY_MS` delay (50 ms default) |

Endpoints (identical in both apps):
- `GET /orders/{id}`: one primary-key lookup.
- `GET /orders/{id}/enriched`: the lookup, then `GET stub/customers/{customerId}`.
- Unknown id: `404`, empty body.
- `GET /actuator/health`, `GET /actuator/prometheus` in all services.

Workloads (`load/w*.js`, open model, k6 `constant-arrival-rate`):

| | Endpoint mix | Bound by |
|---|---|---|
| **W1** | 100% `/orders/{id}/enriched` | mixed I/O: DB + 50 ms downstream call |
| **W2** | 100% `/orders/{id}` | DB |
| **W3** | `ENRICHED_RATIO` (default 0.5) enriched, the rest plain lookups | mixed traffic |

Ids are uniform random over the 1M rows. Response bodies are discarded by k6 (correctness is covered by the smoke
test), and the request timeout is 10 s (`TIMEOUT`).

```
mvc-service/  webflux-service/  stub-service/   Spring Boot 4.2 apps (one Java file each)
docker/       Dockerfile (SERVICE, JDK build args), docker-compose.yml, .env (limits), postgres/init.sql
load/         w1.js w2.js w3.js lib.js (workloads), smoke.js, verify-identical.sh, run.sh (benchmark matrix)
analysis/     analyze.py (stats + charts), test_analyze.py, requirements.txt
results/      created by run.sh (git-ignored)
```

## Run locally

Requirements: Linux with cgroup v2 (needed for the CPU/memory counters), Docker with Compose v2, k6, Python 3.11+,
`jq`, `curl`. On macOS or Windows, run inside a Linux VM.

**1. Bring the stack up and check correctness**

```sh
cd docker
docker compose up -d --build        # postgres (seeds 1M rows, ~5 s), stub, all three variants on JDK 21
../load/verify-identical.sh         # byte-compares status, Content-Type and body across the three variants
for p in 8081 8082 8083; do k6 run -e BASE_URL=http://localhost:$p ../load/smoke.js; done
docker compose down -v
```

`JDK=25 APP_CPUS=1 docker compose up -d --build webflux` overrides any value in `docker/.env`. The image for each
JDK is `bench/<service>:jdk<J>`. The stub always runs on JDK 21, since it is part of the environment.

**2. Run the benchmark matrix**

```sh
DRY_RUN=1 load/run.sh               # prints the plan and an estimated duration
load/run.sh                         # the full default matrix
```

Defaults are 3 workloads × 3 variants × JDK 21/25/27 × 1/2/4 CPUs × 500/1000/2000 req/s × 5 repetitions
= 1215 runs, **about 72 h**. Narrow it with env vars:

```sh
WORKLOADS=w1 JDKS=25 CPUS="1 2" RATES="250 500 1000 2000 4000" REPS=5 load/run.sh
```

| Var | Default | Meaning |
|---|---|---|
| `WORKLOADS` `VARIANTS` `JDKS` `CPUS` `RATES` | see above | the matrix (space-separated lists) |
| `REPS` | 5 | repetitions per config |
| `WARMUP` / `DURATION` | 60 / 120 | seconds of warm-up (discarded) and of measurement, at the same rate |
| `COOLDOWN` | 10 | idle seconds between runs |
| `SHUFFLE` | 1 | shuffle config order within each repetition (seeded, so reproducible) |
| `K6_RAW` | 0 | 1 = also save per-request k6 JSON (`k6.json.gz`, large) |
| `K6_CPUSET` | | pin k6 with `taskset`, e.g. `10-15` |
| `RESULTS` | `results/` | output root |

Each run:
1. Starts a fresh container for the variant (cold JVM). Postgres and the stub stay up, and the table is
   preloaded into shared buffers once at the start.
2. Warms up, snapshots the counters, measures, then snapshots again.
3. Stops the JVM gracefully so the JFR recording is written.

The repetition loop is outermost, so the 5 runs of a config are spread across the whole session instead of
running back to back. A run counts as done once its `k6-summary.json` exists. Re-running the same command
**resumes**, skipping finished runs. For a long session use `nohup load/run.sh > run.log 2>&1 &` or `tmux`.

`results/<workload>_<variant>_jdk<J>_cpu<C>_r<RATE>/run<N>/` contains:

| File | Content |
|---|---|
| `config.json` | matrix coordinates, image id, git commit, host, start time |
| `compose-effective.yml` | the exact resolved container config: limits, env, JVM flags |
| `jvm-flags.txt` | `-XX:+PrintFlagsFinal` from inside the run's container (GC and heap settings as resolved under its limits) |
| `gc.log` | `-Xlog:gc*`: GC/heap settings at startup (`gc,init`), then every collection |
| `recording.jfr` | JFR, `default` settings, whole container lifetime (warm-up included) |
| `k6-summary.json`, `k6.log` | measured window; `k6-warmup-*` for the warm-up |
| `prometheus-{before,after}.txt` | `/actuator/prometheus` around the measured window |
| `cgroup-{before,after}.txt` | cgroup v2 `cpu.stat`, `memory.current`, `memory.peak` for app, stub and DB |
| `app.log`, `k6-exit-code` | container log, k6 exit status |

**3. Analyse**

```sh
python3 -m venv analysis/.venv && analysis/.venv/bin/pip install -r analysis/requirements.txt
analysis/.venv/bin/python analysis/analyze.py              # reads results/, writes results/_analysis/
analysis/.venv/bin/python analysis/test_analyze.py         # self-check
```

The analysis writes the following:
- `runs.csv`: one row per run.
- `summary.csv`: one row per config, giving the mean and the half-width of the two-sided 95% Student-t
  confidence interval (`*_ci95`) across repetitions.
- `<workload>_jdk<J>.{png,pdf}`: charts of p50, p99, throughput, error rate, CPU (fraction of limit) and memory
  peak against offered load, with one column per CPU limit.

Metrics:
- **Latency:** k6 `http_req_duration` percentiles over all requests, failures included.
- **Throughput:** completed requests/s.
- **Error rate:** non-2xx/3xx responses or timeouts.
- **Dropped rate:** iterations k6 could not start because every VU was still waiting. This is the open-model
  overload signal, so read it together with the error rate.
- **CPU:** cgroup `usage_usec` delta over the measured window. `app_throttled_s` is time spent CPU-throttled by
  the quota.
- **Memory:** cgroup `memory.peak` for the container, plus JVM heap used and GC pause time from Micrometer.
- **Environment CPU:** `stub_cpu_cores` and `db_cpu_cores` show whether the environment, rather than the app,
  was the bottleneck. Check them before drawing conclusions.

## Run on a VPS

Use a **dedicated-vCPU** instance (not shared/burstable) with at least 16 vCPUs and 16 GB RAM. That leaves room
for an app with 4 CPUs plus Postgres (4), the stub (2) and k6 without them competing.

```sh
# Ubuntu 24.04+
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER   # log out and in again
sudo gpg -k && sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
  --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69
echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
  | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update && sudo apt-get install -y k6 jq git python3-venv
git clone https://github.com/Ferid2003/Virtual-Threads-vs.-Reactive-Programming-in-Spring.git bench && cd bench
```

Pin each component to its own cores so they never share a core. Run `lscpu -e` first and keep SMT siblings in
the same group:

```sh
export APP_CPUSET=0-3 DB_CPUSET=4-7 STUB_CPUSET=8-9 K6_CPUSET=10-15
sudo cpupower frequency-set -g performance 2>/dev/null || true    # if the VM exposes frequency scaling
(cd docker && docker compose up -d --build) && load/verify-identical.sh   # sanity check, then:
nohup load/run.sh > run.log 2>&1 &
```

With `APP_CPUSET=0-3` and `CPUS=1`, the app may run on any of the 4 cores but is limited to 1 CPU of time by
the quota; the JVM sees 1 processor. Copy the results back with
`rsync -a vps:bench/results/ results/` and analyse locally. Record the instance type, region and kernel in the
paper; `config.json` stores the kernel version and core count.

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

**Added setup code on the MVC side: a connection-limit semaphore.** The JDK `HttpClient` cannot cap connections,
so `MvcApplication` wraps every stub call in a `RestClient` interceptor holding a fair
`java.util.concurrent.Semaphore(stub.max-connections)`:
- A request takes a permit before it is sent. If no permit frees up within `stub.acquire-timeout` (30 s), the call fails with an `IOException`, which surfaces as HTTP 500. This mirrors Reactor Netty's `PoolAcquireTimeoutException`.
- The permit is released when the response headers arrive.
- Fairness (FIFO) matches Reactor Netty's pending-acquire queue.
- With HTTP/1.1 and no pipelining, in-flight requests equal open connections, so the semaphore enforces the same limit as `maxConnections`.
- At 1000 permits and a 50 ms stub it only binds above about 20,000 enriched requests/s, but it is part of the measured code path: an uncontended permit costs one `tryAcquire`/`release` pair per call.

The JVM-wide `jdk.httpclient.keepalive.timeout` and `jdk.httpclient.connectionPoolSize` properties are set in
the same constructor. WebFlux needs no extra code: its limits are `ConnectionProvider` settings.

Checked with the cap lowered to 5 and 50 concurrent requests: all three variants queued in the same 50 ms
waves (min ≈66 ms, max ≈565 ms, no errors).

## Threats to validity

- **Response framing.** Tomcat sends `Transfer-Encoding: chunked`, while Netty sends `Content-Length`. The body bytes are identical (enforced by `load/verify-identical.sh`); the framing adds a few bytes per response on MVC.
- **HTTP client** (see above). The two clients also differ in internal threading: the JDK client runs a selector thread plus a small executor, while Reactor Netty shares WebFlux's event loops. MVC's connection permit is released when the response headers arrive; the ~100-byte body is read immediately afterwards.
- **Response-timeout semantics** differ slightly between the clients (see table). With a 50 ms stub and a 10 s limit, neither should ever fire. A timeout would show up in the error rate.
- **Co-located load generator.** k6 runs on the same host as the system under test. Pin it (`K6_CPUSET`) and check that `dropped_iterations` are caused by the target, not by k6: k6's own CPU should stay well below its cores.
- **Measured window edges.** The cgroup snapshots also cover k6's ~1 s start-up and its graceful stop, so CPU averages are slightly diluted, by the same amount for every variant.
- **Aggregation.** Each run's percentile is one sample, and CIs are taken across runs (n = `REPS`). With n = 5 the t-interval is wide (t = 2.78), so report n.
- **Diagnostics overhead.** JFR (`default` settings, about 1%) and Micrometer are enabled in every variant, so the overhead is the same everywhere and not zero.
- **JDK version effects.** JDK 24+ (JEP 491) no longer pins virtual threads inside `synchronized`, so `mvc-virtual` on JDK 21 may behave differently from 25/27 for reasons outside the threading model itself. JFR records `jdk.VirtualThreadPinned` events, so this can be checked.
