#!/usr/bin/env bash
# Benchmark matrix: workload x variant x JDK x CPU limit x rate, REPS repetitions each.
# Results: results/<workload>_<variant>_jdk<J>_cpu<C>_r<RATE>/run<N>/
#
# Every knob is an env var (defaults below), e.g.
#   WORKLOADS=w2 JDKS=25 CPUS=2 RATES="500 1000" REPS=3 load/run.sh
#   DRY_RUN=1 load/run.sh           # print the plan and estimated duration only
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)

: "${WORKLOADS:=w1 w2 w3}"
: "${VARIANTS:=mvc-platform mvc-virtual webflux}"
: "${JDKS:=21 25 27}"
: "${CPUS:=1 2 4}"
: "${RATES:=500 1000 2000}"
: "${REPS:=5}"
: "${WARMUP:=60}"       # seconds, same workload and rate as the measured window, results discarded
: "${DURATION:=120}"    # seconds, measured window
: "${COOLDOWN:=10}"     # seconds idle between runs
: "${SHUFFLE:=1}"       # 1 = shuffle config order within each repetition (seeded by repetition number)
: "${K6_RAW:=0}"        # 1 = also write per-request k6 JSON (k6.json.gz); large, and costs load-generator CPU
: "${K6_CPUSET:=}"      # e.g. 10-15: pin k6 with taskset (see README, VPS)
: "${RESULTS:=$ROOT/results}"
: "${DRY_RUN:=0}"

declare -A PORT=([mvc-platform]=8081 [mvc-virtual]=8082 [webflux]=8083)
export HOST_UID=$(id -u) HOST_GID=$(id -g)
compose() { docker compose -f "$ROOT/docker/docker-compose.yml" "$@"; }
wait_http() { for _ in $(seq 120); do curl -sf -o /dev/null "$1" && return 0; sleep 1; done; return 1; }
k6cmd=(k6); [[ -n $K6_CPUSET ]] && k6cmd=(taskset -c "$K6_CPUSET" k6)

configs=()
for w in $WORKLOADS; do for v in $VARIANTS; do for j in $JDKS; do for c in $CPUS; do for r in $RATES; do
  [[ -n ${PORT[$v]:-} && -f $ROOT/load/$w.js ]] || { echo "unknown variant/workload: $v/$w" >&2; exit 1; }
  configs+=("$w $v $j $c $r")
done; done; done; done; done
total=$(( ${#configs[@]} * REPS ))
echo "plan: ${#configs[@]} configs x $REPS reps = $total runs, ~$(( total * (WARMUP + DURATION + COOLDOWN + 25) / 3600 )) h"
(( DRY_RUN )) && { printf '  %s\n' "${configs[@]}"; exit 0; }

# --- Images and the shared environment (Postgres + stub stay up for the whole matrix). ---
compose build -q stub-service
for j in $JDKS; do JDK=$j compose build -q mvc-platform webflux; done
compose rm -sf mvc-platform mvc-virtual webflux >/dev/null 2>&1 # only the variant under test may run
compose up -d --wait postgres stub-service
wait_http http://localhost:8090/actuator/health
# Load the whole table and index into shared_buffers so the first config doesn't pay for a cold cache.
compose exec -T postgres psql -qU bench -c "CREATE EXTENSION IF NOT EXISTS pg_prewarm" \
  -c "SELECT pg_prewarm('orders'), pg_prewarm('orders_pkey')" >/dev/null

current=""
trap '[[ -n $current ]] && compose rm -sf "$current" >/dev/null 2>&1' EXIT

# Raw cgroup v2 counters for app, stub and DB: CPU time, CPU-quota throttling, memory.
snapshot() { # $1 = before|after
  curl -s "$url/actuator/prometheus" > "$dir/prometheus-$1.txt"
  { echo "meta epoch $(date +%s.%N)"
    for s in app:"$v" stub:stub-service db:postgres; do
      docker exec "$(compose ps -q "${s#*:}")" sh -c 'cat /sys/fs/cgroup/cpu.stat
        echo memory_current $(cat /sys/fs/cgroup/memory.current)
        echo memory_peak $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo NA)' | sed "s/^/${s%%:*} /"
    done; } > "$dir/cgroup-$1.txt"
}

run_one() { # workload variant jdk cpus rate rep
  local w=$1 j=$3 c=$4 r=$5 rep=$6; v=$2
  local cfg="${w}_${v}_jdk${j}_cpu${c}_r${r}"
  dir="$RESULTS/$cfg/run$rep" url="http://localhost:${PORT[$v]}"
  [[ -f $dir/k6-summary.json ]] && { echo "skip $cfg/run$rep (done)"; return; }
  echo "$(date +%T) [$n/$total] $cfg/run$rep"
  rm -rf "$dir"; mkdir -p "$dir"
  export JDK=$j APP_CPUS=$c RUN_DIR=$dir

  jq -n --arg workload "$w" --arg variant "$v" --argjson jdk "$j" --argjson cpus "$c" --argjson rate "$r" \
    --argjson rep "$rep" --argjson warmup "$WARMUP" --argjson duration "$DURATION" \
    --arg image "$(docker image inspect -f '{{.Id}}' "bench/${v%%-*}-service:jdk$j")" \
    --arg commit "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)$(git -C "$ROOT" diff --quiet 2>/dev/null || echo -dirty)" \
    --arg host "$(uname -srm)" --argjson host_cpus "$(nproc)" --arg started "$(date -Is)" \
    '$ARGS.named' > "$dir/config.json"
  compose config "$v" > "$dir/compose-effective.yml" # exact limits, JVM flags, env of this run

  current=$v
  compose up -d --no-deps --force-recreate "$v" >/dev/null 2>&1
  if ! wait_http "$url/actuator/health"; then
    echo "  app did not become healthy, skipping"; compose logs --no-color "$v" > "$dir/app.log"
    touch "$dir/FAILED"; compose rm -sf "$v" >/dev/null 2>&1; return
  fi
  # GC/heap settings as resolved inside this container (same cgroup limits); gc.log has the runtime view.
  compose exec -T -e JDK_JAVA_OPTIONS= "$v" java -XX:+PrintFlagsFinal -version > "$dir/jvm-flags.txt" 2>&1

  local k6env=(-e "BASE_URL=$url" -e "RATE=$r")
  "${k6cmd[@]}" run -q "${k6env[@]}" -e "DURATION=${WARMUP}s" --summary-export "$dir/k6-warmup-summary.json" \
    "$ROOT/load/$w.js" > "$dir/k6-warmup.log" 2>&1 || true
  snapshot before
  local raw=(); (( K6_RAW )) && raw=(--out "json=$dir/k6.json.gz")
  local rc=0
  "${k6cmd[@]}" run "${k6env[@]}" -e "DURATION=${DURATION}s" --summary-export "$dir/k6-summary.tmp" "${raw[@]}" \
    "$ROOT/load/$w.js" > "$dir/k6.log" 2>&1 || rc=$?
  snapshot after
  echo "$rc" > "$dir/k6-exit-code"

  compose stop -t 60 "$v" >/dev/null 2>&1 # graceful: the JVM dumps recording.jfr on exit
  compose logs --no-color "$v" > "$dir/app.log"
  compose rm -f "$v" >/dev/null 2>&1; current=""
  mv "$dir/k6-summary.tmp" "$dir/k6-summary.json" # marks the run complete
  sleep "$COOLDOWN"
}

n=0
for rep in $(seq 1 "$REPS"); do
  if (( SHUFFLE )); then mapfile -t order < <(printf '%s\n' "${configs[@]}" | shuf --random-source=<(yes "$rep"))
  else order=("${configs[@]}"); fi
  for cfg in "${order[@]}"; do n=$((n + 1)); run_one $cfg "$rep"; done
done
echo "done: $RESULTS"
