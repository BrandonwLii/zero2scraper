# Prototypes for issue #7 (reading text from story images)

Findings are in [`docs/research/media-text.md`](../../docs/research/media-text.md).
Nothing here is imported by the service, and these dependencies stay out of `pyproject.toml`.
Stories are classified from a single still image.

| File | What it is |
|---|---|
| `synth.py` | Generates invented story-like images with known text, in the archive's layout, plus `media_text_truth.jsonl`. `--hard` adds busy backgrounds, low-contrast text, 640 px width and JPEG q55. |
| `engines.py` | The readers: `rapidocr`, `easyocr`, `doctr`, `florence2-base`, `florence2-large`, `vlm:<ollama model>[:transcribe\|:extract]`, `<ocr>+llm:<ollama model>`, and for llama.cpp's `llama-server` at `$LLAMA_SERVER`: `vlmcpp:<label>[:transcribe\|:extract]`, `<ocr>+llmcpp:<label>`. The extraction prompt and JSON schema are here too. |
| `ct_bench.sh` | Setup and runs on the target box (Debian 12, Python 3.11, no apt, no curl): venv, llama.cpp CPU build, Qwen3-VL GGUFs; every run under a shared `flock`, and the model server in a `systemd-run` scope capped at 4.5 GB without swap. |
| `bench.py` | Runs engines over an archive directory and prints aggregate scores, latency and memory. Per-item output (it contains story text) goes to `~/story-watch-data/media-text-research/runs/`, never stdout. |

## Setup (workstation, CPU only)

    cd research/media_text
    uv venv --python 3.12 .venv
    VIRTUAL_ENV=$PWD/.venv uv pip install -r requirements.txt
    # optional engines (torch CPU), see the comment in the file:
    VIRTUAL_ENV=$PWD/.venv uv pip install --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match -r requirements-heavy.txt

Vision and text models run in Ollama. A user-space install works without root: unpack
`ollama-linux-amd64.tar.zst` from the Ollama GitHub release into
`~/story-watch-data/media-text-research/bin/ollama`, then

    OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=~/story-watch-data/media-text-research/models \
        ~/story-watch-data/media-text-research/bin/ollama/bin/ollama serve &
    ollama pull qwen3-vl:4b   # and the others named in the doc

`MEDIA_TEXT_THREADS=6` limits every engine to 6 CPU threads, as on the target box.

## Rerun on synthetic data

    D=~/story-watch-data/media-text-research
    .venv/bin/python synth.py --out $D/synthetic --fonts $D/fonts
    .venv/bin/python synth.py --hard --seed 11 --out $D/synthetic-hard --fonts $D/fonts
    .venv/bin/python bench.py run --archive $D/synthetic --engines rapidocr

On the target box (copy in the scripts and `data/<set>/` with synthetic photos only):

    ./ct_bench.sh setup && ./ct_bench.sh models
    ./ct_bench.sh ocr synthetic-hard rapidocr
    CTX=4096 SERVER_ARGS="--image-max-tokens 512 --cache-ram 0" \
        ./ct_bench.sh vlm synthetic-hard 2b vlmcpp:q2b,rapidocr+llmcpp:q2b nc

Always pass `--cache-ram 0` to llama-server. Its default 8 GB prompt cache grows with every
new image.

Fonts are OFL Google Fonts (Pacifico, Anton, Bebas Neue, Caveat, Playfair Display, Montserrat,
Inter) in `$D/fonts`; without them it falls back to DejaVu.

## Rerun on real stories

Pull the archive (`scripts/pull_archive.py`, defaults to `~/story-watch-data/archive`), then:

    A=~/story-watch-data/archive
    .venv/bin/python bench.py truth-template --archive $A --n 30
    # Fill in $A/media_text_truth.todo.jsonl by hand (text + facts, wording as in the story),
    # then append the filled lines to $A/media_text_truth.jsonl.
    .venv/bin/python bench.py run --archive $A --engines rapidocr
    # with a llama-server running Qwen3-VL-2B (flags as above) on this machine:
    LLAMA_SERVER=http://127.0.0.1:8080 .venv/bin/python bench.py run --archive $A \
        --engines vlmcpp:q2b,rapidocr+llmcpp:q2b

`truth-template` picks a varied sample using the labeling bot's `labels.jsonl` (round-robin over
the labeled post types). Both files stay in the archive directory, outside the repo.
Only paste aggregates from the printed summaries into the repo or issues.
