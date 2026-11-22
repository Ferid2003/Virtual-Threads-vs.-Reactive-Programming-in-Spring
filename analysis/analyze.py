"""Aggregate load/run.sh results: per-run metrics, per-config mean + 95% CI, and charts.

    python analysis/analyze.py [results_dir] [out_dir]      (defaults: results/  results/_analysis/)

Per run (one measured window):
  latency p50/p95/p99/p99.9 (ms, all requests incl. failures), throughput (completed req/s),
  error rate (non-2xx/3xx or timeout), dropped rate (iterations k6 could not start = target fell behind),
  app CPU (cores and fraction of the limit, from cgroup usage_usec), CPU throttling, app memory (cgroup peak),
  JVM heap used and GC pause time (from the Prometheus snapshots), stub/DB CPU (to show they weren't the bottleneck).
Per config: mean over repetitions with a two-sided 95% Student-t confidence interval (runs are the samples).
"""
import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

from scipy import stats

KEYS = ["workload", "variant", "jdk", "cpus", "rate"]
VARIANTS = ["mvc-platform", "mvc-virtual", "webflux"]  # fixed order = fixed colour/marker per variant


def read_cgroup(path):
    """'service key value' lines -> {service: {key: float}}."""
    out = defaultdict(dict)
    for line in path.read_text().splitlines():
        svc, key, val = line.split()
        out[svc][key] = float("nan") if val == "NA" else float(val)
    return out


PROM_LINE = re.compile(r"^([a-zA-Z_:][\w:]*)(\{[^}]*\})? ([^ ]+)")


def prom_sum(path, name, label=""):
    """Sum of all samples of a metric whose label set contains `label`."""
    total = 0.0
    for line in path.read_text().splitlines():
        m = PROM_LINE.match(line)
        if m and m.group(1) == name and label in (m.group(2) or ""):
            total += float(m.group(3))
    return total


def run_metrics(run_dir):
    cfg = json.loads((run_dir / "config.json").read_text())
    k6 = json.loads((run_dir / "k6-summary.json").read_text())["metrics"]
    before, after = read_cgroup(run_dir / "cgroup-before.txt"), read_cgroup(run_dir / "cgroup-after.txt")
    window = after["meta"]["epoch"] - before["meta"]["epoch"]
    cores = {s: (after[s]["usage_usec"] - before[s]["usage_usec"]) / 1e6 / window for s in ("app", "stub", "db")}
    lat = k6["http_req_duration"]
    reqs = k6["http_reqs"]["count"]
    dropped = k6.get("dropped_iterations", {}).get("count", 0)
    pa, pb = run_dir / "prometheus-after.txt", run_dir / "prometheus-before.txt"
    return {
        **{k: cfg[k] for k in KEYS}, "rep": cfg["rep"],
        "p50_ms": lat["p(50)"], "p95_ms": lat["p(95)"], "p99_ms": lat["p(99)"], "p999_ms": lat["p(99.9)"],
        "throughput_rps": k6["http_reqs"]["rate"],
        "error_rate": k6["http_req_failed"]["value"],
        "dropped_rate": dropped / (reqs + dropped) if reqs + dropped else 0.0,
        "app_cpu_cores": cores["app"],
        "app_cpu_util": cores["app"] / cfg["cpus"],
        "app_throttled_s": (after["app"]["throttled_usec"] - before["app"]["throttled_usec"]) / 1e6,
        "app_mem_peak_mb": after["app"]["memory_peak"] / 2**20,
        "app_mem_mb": after["app"]["memory_current"] / 2**20,
        "heap_used_mb": prom_sum(pa, "jvm_memory_used_bytes", 'area="heap"') / 2**20,
        "gc_pause_s": prom_sum(pa, "jvm_gc_pause_seconds_sum") - prom_sum(pb, "jvm_gc_pause_seconds_sum"),
        "threads_live": prom_sum(pa, "jvm_threads_live_threads"),
        "stub_cpu_cores": cores["stub"],
        "db_cpu_cores": cores["db"],
    }


METRICS = ["p50_ms", "p95_ms", "p99_ms", "p999_ms", "throughput_rps", "error_rate", "dropped_rate",
           "app_cpu_cores", "app_cpu_util", "app_throttled_s", "app_mem_peak_mb", "app_mem_mb", "heap_used_mb",
           "gc_pause_s", "threads_live", "stub_cpu_cores", "db_cpu_cores"]


def mean_ci(xs):
    """Mean and half-width of the two-sided 95% Student-t CI (NaN half-width for n < 2)."""
    xs = [x for x in xs if not math.isnan(x)]
    n = len(xs)
    if n == 0:
        return float("nan"), float("nan")
    m = sum(xs) / n
    if n < 2:
        return m, float("nan")
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, stats.t.ppf(0.975, n - 1) * sd / math.sqrt(n)


def aggregate(runs):
    groups = defaultdict(list)
    for r in runs:
        groups[tuple(r[k] for k in KEYS)].append(r)
    rows = []
    for key, rs in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        row = dict(zip(KEYS, key), n=len(rs))
        for m in METRICS:
            row[m], row[m + "_ci95"] = mean_ci([r[m] for r in rs])
        rows.append(row)
    return rows


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# --- Charts -------------------------------------------------------------------------------------------------
# Categorical slots 1-3 of the dataviz reference palette (validated: CVD-safe, light surface). Aqua is below 3:1
# contrast, so every series also has its own marker and line style, and summary.csv is the table view.
STYLE = {
    "mvc-platform": dict(color="#2a78d6", marker="o", linestyle="-", label="MVC, platform threads"),
    "mvc-virtual": dict(color="#eb6834", marker="s", linestyle="--", label="MVC, virtual threads"),
    "webflux": dict(color="#1baf7a", marker="^", linestyle=":", label="WebFlux"),
}
PANELS = [("p50_ms", "p50 latency (ms)", True), ("p99_ms", "p99 latency (ms)", True),
          ("throughput_rps", "Throughput (req/s)", False), ("error_rate", "Error rate", False),
          ("app_cpu_util", "App CPU (% of limit)", False), ("app_mem_peak_mb", "App memory peak (MB)", False)]


def plot(summary, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, PercentFormatter

    plt.rcParams.update({
        "font.size": 9, "axes.edgecolor": "#898781", "axes.labelcolor": "#52514e", "xtick.color": "#898781",
        "ytick.color": "#898781", "axes.grid": True, "grid.color": "#e6e5e0", "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": "#fcfcfb",
        "axes.facecolor": "#fcfcfb", "savefig.facecolor": "#fcfcfb", "text.color": "#0b0b0b",
    })
    by_fig = defaultdict(list)
    for row in summary:
        by_fig[(row["workload"], row["jdk"])].append(row)
    for (workload, jdk), rows in sorted(by_fig.items()):
        cpus = sorted({r["cpus"] for r in rows})
        fig, axes = plt.subplots(len(PANELS), len(cpus), figsize=(3.2 * len(cpus) + 0.6, 2.1 * len(PANELS)),
                                 sharex=True, sharey="row", squeeze=False)
        for col, c in enumerate(cpus):
            axes[0][col].set_title(f"{c} CPU{'s' if c != 1 else ''}", color="#0b0b0b")
            for i, (metric, ylabel, log) in enumerate(PANELS):
                ax = axes[i][col]
                for k, v in enumerate(VARIANTS):
                    pts = sorted((r["rate"], r[metric], r[metric + "_ci95"]) for r in rows
                                 if r["variant"] == v and r["cpus"] == c)
                    if not pts:
                        continue
                    x, y, ci = zip(*pts)
                    ci = [0 if math.isnan(e) else e for e in ci]
                    # All metrics are non-negative: clip the lower CI bound at 0. Dodge variants by +-1.5% in x.
                    ax.errorbar([xi * (1 + 0.015 * (k - 1)) for xi in x], y, yerr=[[min(e, yi) for e, yi in zip(ci, y)], ci],
                                capsize=3, linewidth=1.5, markersize=6, markeredgecolor="#fcfcfb",
                                markeredgewidth=1, **STYLE[v])
                if log:
                    ax.set_yscale("log")
                    ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _: f"{val:g}"))
                    ax.yaxis.set_minor_formatter(FuncFormatter(  # label 2x and 5x per decade only
                        lambda val, _: f"{val:g}" if round(val / 10 ** math.floor(math.log10(val))) in (2, 5) else ""))
                else:
                    ax.set_ylim(bottom=0)
                if metric in ("error_rate", "app_cpu_util"):
                    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
                if col == 0:
                    ax.set_ylabel(ylabel)
                if i == len(PANELS) - 1:
                    ax.set_xlabel("Offered load (req/s)")
        handles, labels = axes[0][0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.0))
        fig.suptitle(f"{workload.upper()}, JDK {jdk}: mean of runs, 95% CI", y=1.025, color="#0b0b0b")
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        for ext in ("png", "pdf"):
            fig.savefig(out_dir / f"{workload}_jdk{jdk}.{ext}", dpi=200, bbox_inches="tight")
        plt.close(fig)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "results")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "_analysis"
    run_dirs = sorted(p.parent for p in root.glob("*/run*/k6-summary.json") if not p.parts[-3].startswith("_"))
    if not run_dirs:
        sys.exit(f"no completed runs under {root}")
    runs = [run_metrics(d) for d in run_dirs]
    summary = aggregate(runs)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "runs.csv", runs)
    write_csv(out / "summary.csv", summary)
    plot(summary, out)
    skipped = len(list(root.glob("*/run*/FAILED")))
    print(f"{len(runs)} runs, {len(summary)} configs ({skipped} failed runs skipped) -> {out}")


if __name__ == "__main__":
    main()
