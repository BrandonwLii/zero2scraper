#!/usr/bin/env bash
# Benchmark matrix inside the service container. Every run holds the shared CPU
# lock so it never overlaps another worker's benchmark. Usage (in the CT):
#   ./ct_run.sh            # BASE defaults to /root/bench/issue-8
# Env: MODELS ("q2 q4"), THREADS ("6 4"), CAPS ("512"), RUNS (3).
set -u
BASE=${BASE:-/root/bench/issue-8}
SERVER=$(ls "$BASE"/bin/llama-*/llama-server | head -1)
OUT=$BASE/results/ct120.jsonl
mkdir -p "$BASE/results"
start=$(date '+%Y-%m-%d %H:%M:%S')
# Sample container memory and the watcher's state every 5 s while the matrix runs.
( while :; do
    echo "$(date +%s) $(awk '/MemAvailable/{print $2}' /proc/meminfo) $(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t-f}' /proc/meminfo)" \
         "$(systemctl show story-watch -p MemoryCurrent --value) $(systemctl is-active story-watch) $(systemctl is-active story-watch-bot) $(cut -d' ' -f1 /proc/loadavg)"
    sleep 5
  done ) >> "$BASE/results/samples.txt" &
sampler=$!
trap 'kill $sampler' EXIT
for m in ${MODELS:-q2 q4}; do
  for t in ${THREADS:-6 4}; do
    for cap in ${CAPS:-512}; do
      tv=$BASE/results/time-v-$m-t$t-c$cap.txt
      flock /root/bench/.cpu.lock python3 "$BASE/bench_server.py" --server "$SERVER" \
        --model "$BASE/models/$m.gguf" --mmproj "$BASE/models/$m-mmproj.gguf" \
        --threads "$t" --ctx 4096 --image-max-tokens "$cap" --runs "${RUNS:-3}" \
        --extra="--load-mode none" --evict --time-v "$tv" --label "$m" \
        "$BASE"/images/*.jpg >> "$OUT" || echo "{\"label\": \"$m\", \"threads\": $t, \"cap\": $cap, \"error\": true}" >> "$OUT"
    done
  done
done
python3 "$BASE/watcher_health.py" --since "$start" >> "$BASE/results/watcher.jsonl"
python3 "$BASE/watcher_health.py" --since "$start" --unit story-watch-bot >> "$BASE/results/watcher.jsonl"
