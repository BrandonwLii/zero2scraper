# Tagger prototypes for issue #10 (throwaway)

Findings: [`docs/research/tagger-comparison.md`](../../docs/research/tagger-comparison.md). Nothing here is
imported by the service, and its dependencies stay out of `pyproject.toml`.

Private inputs and outputs live under `~/story-watch-data` (`STORY_WATCH_DATA`), never in the repo:
the labelled archive, the saved listings (`listings/<media id>.json`, #9's extractor output), and the
work dir `eval-work/issue-10/` (stills, OCR text, request files, model replies, scores).

| File | What it is |
|---|---|
| `common.py` | Story inputs (image, link, mentions, saved listing), the prompt context text, company-list matching parsed from `docs/tags.md`, title rules, extra refusal phrasings, and the contract guards. |
| `prompts/tag_v1.txt` | The tagging prompt: definitions from `docs/tags.md`, the company lists, three invented examples. |
| `prompting.py` | System prompt, JSON schema and llama-server request bodies. |
| `prep_images.py` | One still per labelled story into the work dir. |
| `ocr.py` | RapidOCR over the stills (standalone; runs in CT 120 with #7's venv). |
| `make_requests.py` | Request bodies per pipeline (`vlm`, `ocr-text`, `vlm-ocr`) as JSON lines. |
| `run_requests.py` | Sends request bodies to a llama-server, saves replies and latency (stdlib only). |
| `ct_serve.py`, `ct_bench.sh` | CT 120 benchmark wrapper: CPU lock, `-t 4`, `nice 10`, a `MemoryMax=4.5G` no-swap scope, a 5 s memory/watcher sampler with a 1 GiB kill switch, server peak RSS and scope `memory.peak`. |
| `claude_ref.py` | The Claude reference: one headless `claude -p --model sonnet` call per story, cached; stops at the first usage-limit error. |
| `taggers.py` | The pipelines as harness taggers (`--tagger taggers:<factory>`), reading saved replies. |
| `score.py` | Scores taggers with #10's adjustments (unverifiable sponsorship excluded), aggregates only. |
| `ping_configs_single.toml` | One synthetic user per tag value, a more sensitive missed-ping count. |
| `test_common.py` | Offline tests on invented inputs. |

    uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python pillow numpy rapidocr==3.10.0 onnxruntime==1.30.0 requests pytest imageio-ffmpeg -e ../..
    .venv/bin/python -m pytest -q test_common.py
    .venv/bin/python prep_images.py
    .venv/bin/python ocr.py ~/story-watch-data/eval-work/issue-10/images ~/story-watch-data/eval-work/issue-10/ocr
    .venv/bin/python make_requests.py vlm-v1 --mode vlm
    # llama-server -m <gguf> --mmproj <mmproj> -t 4 -c 4096 -np 1 --cache-ram 0 --load-mode none --image-max-tokens 512 --port 8090
    .venv/bin/python run_requests.py ~/story-watch-data/eval-work/issue-10/requests/vlm-v1.jsonl ~/story-watch-data/eval-work/issue-10/raw/ws-q2b4-i512-v1
    .venv/bin/python claude_ref.py sonnet-v1
    TAGGER_RAW_PREFIX=ws PYTHONPATH=. .venv/bin/python score.py vlm_q2b4_i512 hybrid_q2b4_i512_rolerules claude_sonnet_v1
    # the #6 harness works too:
    PYTHONPATH=research/tagger research/tagger/.venv/bin/python scripts/eval_tagger.py --tagger taggers:hybrid_q2b4_i512_rolerules --no-cache

In CT 120: copy `ct_bench.sh ct_serve.py run_requests.py ocr.py` to `/root/bench/issue-10/`, the stills and
request files to `/root/bench/issue-10/data/`, then `./ct_bench.sh ocr` and
`./ct_bench.sh vlm <label> <model.gguf> <mmproj.gguf|none> <image tokens>`. Delete `data/` afterwards.
