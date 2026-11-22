"""Self-check for analyze.py on a synthetic two-run config:  python analysis/test_analyze.py"""
import json
import math
import tempfile
from pathlib import Path

import analyze


def fake_run(d, rep, p99, cpu_usec):
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(
        {"workload": "w2", "variant": "webflux", "jdk": 21, "cpus": 2, "rate": 100, "rep": rep}))
    (d / "k6-summary.json").write_text(json.dumps({"metrics": {
        "http_req_duration": {"p(50)": 3, "p(95)": 5, "p(99)": p99, "p(99.9)": 20},
        "http_reqs": {"count": 900, "rate": 90.0}, "http_req_failed": {"value": 0.01},
        "dropped_iterations": {"count": 100}}}))
    cg = lambda t, usec: "\n".join([f"meta epoch {t}"] + [
        f"{s} {k} {v}" for s in ("app", "stub", "db")
        for k, v in (("usage_usec", usec), ("throttled_usec", usec / 10), ("memory_current", 2**20),
                     ("memory_peak", 2 * 2**20))])
    (d / "cgroup-before.txt").write_text(cg(1000.0, 0))
    (d / "cgroup-after.txt").write_text(cg(1010.0, cpu_usec))  # 10 s window
    (d / "prometheus-before.txt").write_text('jvm_gc_pause_seconds_sum{gc="G1"} 1.0\n')
    (d / "prometheus-after.txt").write_text(
        '# HELP x\njvm_gc_pause_seconds_sum{gc="G1"} 1.5\n'
        'jvm_memory_used_bytes{area="heap",id="eden"} 1048576\njvm_memory_used_bytes{area="heap",id="old"} 1048576\n'
        'jvm_memory_used_bytes{area="nonheap",id="meta"} 9999999\njvm_threads_live_threads 30.0\n')


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    fake_run(root / "cfg" / "run1", 1, p99=10, cpu_usec=10e6)  # 1 core over 10 s
    fake_run(root / "cfg" / "run2", 2, p99=12, cpu_usec=20e6)  # 2 cores
    runs = [analyze.run_metrics(root / "cfg" / f"run{i}") for i in (1, 2)]
    r = runs[0]
    assert r["app_cpu_cores"] == 1.0 and r["app_cpu_util"] == 0.5 and r["app_throttled_s"] == 1.0
    assert r["dropped_rate"] == 0.1 and r["heap_used_mb"] == 2.0 and r["app_mem_peak_mb"] == 2.0
    assert math.isclose(r["gc_pause_s"], 0.5) and r["threads_live"] == 30

    [s] = analyze.aggregate(runs)
    assert s["n"] == 2 and s["p99_ms"] == 11
    # two samples 10, 12: sd = sqrt(2), t(0.975, 1) = 12.706 -> half-width = 12.706 * sqrt(2) / sqrt(2)
    assert math.isclose(s["p99_ms_ci95"], 12.7062, rel_tol=1e-4)
    assert math.isnan(analyze.mean_ci([5.0])[1])
    analyze.plot([s], root)
    assert (root / "w2_jdk21.png").stat().st_size > 0
print("ok")
