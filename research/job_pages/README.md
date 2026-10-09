# Prototypes for issue #9 (throwaway)

Findings are in [`docs/research/job-pages.md`](../../docs/research/job-pages.md).
Nothing here is imported by the service, and its dependencies stay out of `pyproject.toml`.

| File | What it is |
|---|---|
| `fetch.py` | Guarded fetch: the `jobs.py` guards (fresh session, https only, public address on every hop, 2 MB cap), plus a best-effort check that only detects a connection to a private peer address (not a fix for DNS rebinding), and browser-style `Sec-Fetch-*` headers. |
| `extract.py` | Link → `Posting` (title, company, locations, description text). Greenhouse, Lever, Ashby, Workday and SmartRecruiters APIs; JSON-LD; JSON app state (one big-tech careers site); iCIMS iframe; visible-text fallback; closed-posting detection. |
| `rules.py` | Text rules for sponsorship, level and "in or open to Canada", returning sets as in `docs/tags.md`. |
| `llm.py` | The same three fields from a local model behind llama.cpp's `llama-server` (JSON-schema output). `full` sends the posting; `snippets` sends only the relevant sentences. |
| `run.py` | Fetches a list of links once and prints coverage per ATS and per extractor. |
| `evaluate.py` | Scores rules, model and their union against hand labels. Prints aggregates only. |
| `test_rules.py` | Offline tests with invented phrases. |

Everything derived from real links (fetched postings, labels, model logs) goes to `cache/`, and
models go to `.models/`; both are gitignored. The links file is built locally from story-cache or the
archive and kept outside the repo.

    ../../.venv/bin/python -m venv .venv && uv pip install --python .venv/bin/python requests pytest
    .venv/bin/python -m pytest -q .
    .venv/bin/python run.py /path/outside/repo/links.json
    .venv/bin/python evaluate.py                      # rules only
    # model: llama.cpp release build + a GGUF in .models/, server bound to 127.0.0.1 only
    .models/llamacpp/llama-b11512/llama-server -m .models/gguf/<model>.gguf --host 127.0.0.1 --port 8089 -c 8192 -t 6
    .venv/bin/python evaluate.py --llm http://127.0.0.1:8089 --modes snippets,full

The release build of llama-server needs `libgomp.so.1`; on a machine without it, `apt-get download libgomp1`
and `dpkg-deb -x` it somewhere, then set `LD_LIBRARY_PATH`.
