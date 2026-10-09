#!/usr/bin/env bash
# Run the CPU benchmark matrix. Usage: ./run_matrix.sh CACHE_DIR OUT.jsonl
# CACHE_DIR holds bin-linux/llama-*/llama-server, models/<name>.gguf + <name>-mmproj.gguf, images/*.jpg.
set -u
C=$1; OUT=$2
SERVER=$(ls "$C"/bin-linux/llama-*/llama-server | head -1)
here=$(dirname "$0")
for m in ${MODELS:-q2 q4 q8}; do
  for t in ${THREADS:-8 4}; do
    for cap in ${CAPS:-0 1024 512}; do
      "$here"/.venv/bin/python "$here"/bench_server.py --server "$SERVER" \
        --model "$C/models/$m.gguf" --mmproj "$C/models/$m-mmproj.gguf" \
        --threads "$t" --image-max-tokens "$cap" --runs "${RUNS:-1}" --ctx "${CTX:-8192}" --label "$m" \
        "$C"/images/*.jpg >> "$OUT" || echo "{\"label\": \"$m\", \"threads\": $t, \"cap\": $cap, \"error\": true}" >> "$OUT"
    done
  done
done
