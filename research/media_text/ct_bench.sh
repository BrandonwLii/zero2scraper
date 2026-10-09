#!/bin/bash
# Benchmark on the target box (CPU only), inside one directory. Debian 12, Python 3.11, no apt.
#
#   ./ct_bench.sh setup            venv + core deps, llama.cpp CPU build
#   ./ct_bench.sh setup-florence   torch CPU + transformers (for florence2-base)
#   ./ct_bench.sh models           Qwen3-VL 2B and 4B GGUF (Q4_K_M + Q8_0 mmproj)
#   ./ct_bench.sh ocr  <set> <engine>           e.g. ocr synthetic-hard rapidocr
#   ./ct_bench.sh vlm  <set> <2b|4b> <engines> [tag]   e.g. vlm synthetic-hard 4b vlmcpp:q4b,rapidocr+llmcpp:q4b
#       (CTX and SERVER_ARGS env vars pass through to llama-server)
#
# <set> is a directory under ./data with the archive layout (see bench.py). Every run holds
# $LOCK so only one benchmark uses the CPU at a time; THREADS caps the threads.
set -euo pipefail
B=$(cd "$(dirname "$0")" && pwd)
LOCK=${LOCK:-/root/bench/.cpu.lock}
THREADS=${THREADS:-6}
LLAMA_TAG=b11514
cd "$B"

fetch() {  # url dest -- no curl in a minimal container, so use Python
    python3 -c 'import shutil, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2] + ".part", "wb") as f:
    shutil.copyfileobj(r, f, 1 << 20)' "$1" "$2"
    mv "$2.part" "$2"
}

case "${1:-}" in
setup)
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
    mkdir -p bin
    fetch "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-x64.tar.gz" bin/llama.tgz
    tar -C bin -xzf bin/llama.tgz && rm bin/llama.tgz
    find bin -name llama-server -type f
    ;;
setup-florence)
    .venv/bin/pip install -q --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1
    .venv/bin/pip install -q transformers==5.19.0 timm==1.0.30 einops==0.8.2
    ;;
models)
    mkdir -p models
    for s in 2B 4B; do
        for f in "Qwen3VL-$s-Instruct-Q4_K_M.gguf" "mmproj-Qwen3VL-$s-Instruct-Q8_0.gguf"; do
            [ -s "models/$f" ] || fetch "https://huggingface.co/Qwen/Qwen3-VL-$s-Instruct-GGUF/resolve/main/$f" "models/$f"
        done
    done
    du -sh models
    ;;
ocr)
    set_=$2 engine=$3
    flock -o "$LOCK" /usr/bin/time -v -o "runs/time.$set_.${engine//[^A-Za-z0-9]/_}.txt" \
        env MEDIA_TEXT_THREADS="$THREADS" HF_HOME="$B/hf" .venv/bin/python bench.py run \
        --archive "data/$set_" --engines "$engine" --results runs
    grep -E "Maximum resident" "runs/time.$set_.${engine//[^A-Za-z0-9]/_}.txt"
    ;;
vlm)
    # Extra llama-server flags via SERVER_ARGS, e.g. SERVER_ARGS="--image-max-tokens 1024".
    # flock -o: the lock fd is not inherited by the server, so a stray server can't hold it.
    # The server runs in a cgroup capped at MEMMAX (default 4500M, no swap) so it can't starve
    # the service sharing the box; over the cap it is OOM-killed and the run reports errors.
    set_=$2 size=$3 engines=$4 tag=${5:-default}
    S=$(echo "$size" | tr a-z A-Z)
    server=$(find bin -name llama-server -type f | head -1)
    out="runs/server.$set_.$size.$tag"
    flock -o "$LOCK" bash -c '
        set -e
        systemd-run --scope -q -p MemoryMax=${MEMMAX:-4500M} -p MemorySwapMax=0 \
        "'"$server"'" -m "models/Qwen3VL-'"$S"'-Instruct-Q4_K_M.gguf" \
            --mmproj "models/mmproj-Qwen3VL-'"$S"'-Instruct-Q8_0.gguf" \
            -t '"$THREADS"' -tb '"$THREADS"' -c ${CTX:-4096} -np 1 --host 127.0.0.1 --port 8080 --no-webui \
            '"${SERVER_ARGS:-}"' > "'"$out"'.log" 2>&1 &
        pid=$!
        trap "grep VmHWM /proc/$pid/status > \"'"$out"'.hwm\" 2>/dev/null || true; kill $pid 2>/dev/null; wait $pid 2>/dev/null || true" EXIT
        MEDIA_TEXT_THREADS='"$THREADS"' HF_HOME="'"$B"'/hf" .venv/bin/python bench.py run \
            --archive "data/'"$set_"'" --engines "'"$engines"'" --results runs
    '
    echo "server peak RSS: $(cat "$out.hwm")"
    ;;
*)
    sed -n 2,12p "$0"
    exit 2
    ;;
esac
