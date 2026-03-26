#!/usr/bin/env python3
"""Parse Go benchmark output and write results/signing_bench.json."""
import json
import re
import statistics
import sys
from pathlib import Path

# Map Go benchmark sub-test names -> JSON keys
NAME_MAP = {
    "RSA-SHA256": "RSASHA256",
    "ECDSA-P256": "ECDSAP256SHA256",
    "Ed25519": "ED25519",
    "Falcon-512": "Falcon-512",
    "Falcon-1024": "Falcon-1024",
    "ML-DSA-44": "ML-DSA-44",
    "ML-DSA-65": "ML-DSA-65",
    "ML-DSA-87": "ML-DSA-87",
    "MAYO-1": "MAYO-1",
    "MAYO-3": "MAYO-3",
    "SNOVA": "SNOVA",
    "SLH-DSA-SHA2-128s": "SLH-DSA-SHA2-128s",
}


def main():
    bench_output = sys.stdin.read()
    data = {}
    for line in bench_output.splitlines():
        m = re.match(r"BenchmarkSign/(.+?)-\d+\s+(\d+)\s+(\d+)\s+ns/op", line)
        if m:
            name = m.group(1)
            ns = int(m.group(3))
            n = int(m.group(2))
            data.setdefault(name, []).append((ns, n))

    result = {}
    for bench_name, json_key in NAME_MAP.items():
        if bench_name not in data:
            print(f"WARNING: {bench_name} not found in benchmark output", file=sys.stderr)
            continue
        entries = data[bench_name]
        vals_ms = [ns / 1e6 for ns, _ in entries]
        n = entries[0][1]
        mean = statistics.mean(vals_ms)
        std = statistics.stdev(vals_ms) if len(vals_ms) > 1 else 0.0
        cv = std / mean if mean > 0 else 0.0
        result[json_key] = {
            "mean_ms": round(mean, 4),
            "std_ms": round(std, 4),
            "cv": round(cv, 4),
            "n": n,
        }

    out_path = Path(__file__).resolve().parent.parent / "results" / "signing_bench.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=4)
    print(f"Written {out_path}")
    for k, v in result.items():
        print(f"  {k}: {v['mean_ms']:.4f} ms (CV={v['cv']:.4f})")


if __name__ == "__main__":
    main()
