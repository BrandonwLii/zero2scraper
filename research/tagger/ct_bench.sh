#!/usr/bin/env bash
# #10 benchmarks inside CT 120. Copy ct_bench.sh, ct_serve.py, run_requests.py and ocr.py to
# /root/bench/issue-10/, and the private request files and stills to /root/bench/issue-10/data/
# (deleted afterwards). Reuses #7's and #8's llama.cpp build, models and RapidOCR venv.
#
#   ./ct_bench.sh vlm <label> <model.gguf> <mmproj.gguf|none> <image tokens>
#   ./ct_bench.sh ocr
#
# Every run: the shared CPU lock, 4 threads, nice 10, a scope with MemoryMax=4.5G and no swap, and a
# sampler that records MemAvailable and the watcher's state every 5 s and kills the run if the CT
# has less than 1 GiB available.
set -u
B=/root/bench/issue-10
SERVER=${SERVER:-/root/bench/issue-8/bin/llama-b11514/llama-server}
PY_OCR=${PY_OCR:-/root/bench/issue-7/.venv/bin/python}
mkdir -p "$B/results/raw"
LIMITS=(-p MemoryMax=4.5G -p MemorySwapMax=0)

sampler() {  # $1 = tag
  while :; do
    avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
    echo "$(date +%s) $1 $avail $(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t-f}' /proc/meminfo)" \
         "$(systemctl is-active story-watch) $(systemctl is-active story-watch-bot) $(cut -d' ' -f1 /proc/loadavg)"
    if [ "$avail" -lt 1048576 ]; then
      echo "$(date +%s) $1 LOW-MEMORY kill"; pkill -f "llama-server.*--port 8091"; pkill -f "$B/ocr.py"
    fi
    sleep 5
  done >> "$B/results/samples.txt"
}

case "${1:-}" in
vlm)
  label=$2
  sampler "$label" & sp=$!; trap 'kill $sp' EXIT
  flock -o /root/bench/.cpu.lock systemd-run --scope --quiet "${LIMITS[@]}" nice -n 10 \
    python3 "$B/ct_serve.py" --server "$SERVER" --model "$3" --mmproj "$4" --image-tokens "$5" \
      --requests "$B/data/requests/$label.jsonl" --out "$B/results/raw/$label" --label "$label" --results "$B/results"
  ;;
ocr)
  sampler ocr & sp=$!; trap 'kill $sp' EXIT
  flock -o /root/bench/.cpu.lock systemd-run --scope --quiet "${LIMITS[@]}" nice -n 10 \
    /usr/bin/time -v "$PY_OCR" "$B/ocr.py" "$B/data/images" "$B/results/ocr" --threads 4 \
    > "$B/results/ocr.summary.json" 2> "$B/results/ocr.time-v.txt"
  ;;
*) echo "usage: $0 vlm <label> <model> <mmproj|none> <image tokens> | ocr" >&2; exit 2 ;;
esac
