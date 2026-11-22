#!/usr/bin/env bash
# Asserts all variants return byte-identical status, Content-Type and body for the same requests.
# Usage: load/verify-identical.sh [base-url ...]   (default: the three compose ports)
set -euo pipefail
urls=("$@")
(( ${#urls[@]} )) || urls=(http://localhost:8081 http://localhost:8082 http://localhost:8083)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# Edge ids + a fixed pseudo-random sample; 0 and 1000001 must 404 everywhere.
ids=(0 1 2 999999 1000000 1000001 $(RANDOM=42; for _ in $(seq 40); do echo $(( (RANDOM * 32768 + RANDOM) % 1000000 + 1 )); done))
fail=0 n=0
for id in "${ids[@]}"; do
  for path in "/orders/$id" "/orders/$id/enriched"; do
    for i in "${!urls[@]}"; do
      meta[i]=$(curl -sS -o "$tmp/$i" -w '%{http_code} %{content_type}' "${urls[i]}$path")
      if (( i > 0 )) && { [[ ${meta[i]} != "${meta[0]}" ]] || ! cmp -s "$tmp/0" "$tmp/$i"; }; then
        echo "MISMATCH $path"; echo "  ${urls[0]}: ${meta[0]} $(cat "$tmp/0")"; echo "  ${urls[i]}: ${meta[i]} $(cat "$tmp/$i")"
        fail=1
      fi
    done
    n=$((n + 1))
  done
done
echo "checked $n requests x ${#urls[@]} variants: $( ((fail)) && echo FAILED || echo all identical)"
exit $fail
