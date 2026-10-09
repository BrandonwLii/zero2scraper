# Prototypes for issue #8 (throwaway)

Findings are in [`docs/research/inference-hosting.md`](../../docs/research/inference-hosting.md).
Nothing here is imported by the service, and these dependencies stay out of `pyproject.toml`.

| File | What it is |
|---|---|
| `make_images.py` | Writes two synthetic 1080×1920 story-sized JPEGs (a headline over a noisy background, and a dense job-post screenshot). The text is invented. No real story media is used. |
| `bench_server.py` | Starts `llama-server` on 127.0.0.1, waits for `/health`, sends each image through the OpenAI-compatible chat endpoint with a tagging-style JSON prompt, and prints one JSON line: load time, RSS, latency, image tokens, generation speed, and whether the JSON parsed and contained the synthetic facts. |
| `ct_run.sh` | The matrix run inside the service container (CT 120): every run holds the shared CPU lock, evicts the model from the page cache first (cold load), runs the server under `/usr/bin/time -v`, samples memory and the units' state every 5 s, and finishes with `watcher_health.py`. |
| `watcher_health.py` | Summarises the watcher's (or bot's) journal over a window as aggregates only: cycles, WARNING/ERROR counts, worst lateness against the announced next check. Prints no log text. |
| `run_matrix.sh` | Runs `bench_server.py` over models × threads × image-token caps (env: `MODELS`, `THREADS`, `CAPS`, `CTX`, `RUNS`). |
| `results/ct120.jsonl`, `results/ct120-watcher.jsonl`, `results/ct120-samples-summary.json` | **Measured in CT 120**: the benchmark rows, the watcher/bot health during the runs, and the sampler summary. These are the numbers in the doc. |
| `results/workstation.jsonl` | Earlier runs on a development workstation (8-core desktop CPU, DDR5), kept as a side note only (not the target). No model output is stored anywhere. |

## Rerun

Everything below stays outside the repo (`~/.cache/story-watch-research`). Nothing is sent anywhere; the only network use is the downloads.

    C=~/.cache/story-watch-research; mkdir -p $C/models $C/bin-linux
    # llama.cpp release b11514: llama-b11514-bin-ubuntu-x64.tar.gz -> $C/bin-linux
    # The Linux build needs libgomp.so.1 (apt install libgomp1, or copy one next to llama-server).
    # Models (Hugging Face), saved as $C/models/<name>.gguf and <name>-mmproj.gguf:
    #   q2: ggml-org/Qwen3-VL-2B-Instruct-GGUF  (Q8_0 + mmproj Q8_0)
    #   q4: Qwen/Qwen3-VL-4B-Instruct-GGUF      (Q4_K_M + mmproj Q8_0)
    cd research/inference-hosting
    uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python -r requirements.txt
    .venv/bin/python make_images.py $C/images
    MODELS="q2 q4" THREADS=4 CTX=4096 CAPS=512 ./run_matrix.sh $C results/local.jsonl

## Rerun inside CT 120

The CT needs `apt-get install libgomp1 time` (python3 3.11 is already there). The prebuilt `ubuntu-x64` llama.cpp release runs on Debian 12 as is. Put the release under `/root/bench/issue-8/bin/`, the models under `models/` (`q2`, `q4` as above), the two synthetic images under `images/`, and `bench_server.py`, `watcher_health.py` and `ct_run.sh` next to them, then:

    cd /root/bench/issue-8 && MODELS="q2 q4" THREADS="4 6" CAPS=512 RUNS=3 ./ct_run.sh

Hold the shared CPU lock for the whole session if other benchmarks share the CT, and keep at least 2 GB of disk free for the watcher.

To try real stories, point the scripts at images from the local archive. Keep their output out of the repo.
