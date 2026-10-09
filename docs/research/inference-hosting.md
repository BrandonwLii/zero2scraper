# Where to run a local vision model (issue #8)

Part of epic #2. Feeds #10 (pipeline choice) and #12 (implementation). **Status: draft with measured numbers. The RAM budget (4–6 GB) is confirmed by the user; the remaining questions are [below](#open-questions-for-the-user).**

All latency and memory figures in this doc were **measured inside the service container (CT 120) on the Proxmox host** on 2026-10-08, unless a row is explicitly labelled as a workstation side note.

## TL;DR

- **Run the model CPU-only in CT 120,** as a second systemd unit (`llama-server` from a pinned llama.cpp release) next to the watcher and the bot. Use no GPU and no second machine. A hosted model stays an option for #10's accuracy reference only.
- **Measured in CT 120** (4 threads, 4096 context, 512 image tokens, still images):

  | Model | Median per image | Worst per image | Peak RSS | Cold load |
  |---|---|---|---|---|
  | Qwen3-VL-2B (Q8_0) | 18 s | 21 s | 3.0 GiB | 3.6 s |
  | Qwen3-VL-4B (Q4_K_M) | 23 s | 26 s | 3.9 GiB | 6.0 s |

  Both fit the confirmed 4–6 GB budget. Gemma 3 4B (QAT Q4_0) also fits (3.9 GiB) but took 38 s per image, so it was dropped.
- **Use 4 threads, not 6.** On this host, 4 threads were faster than 6 for every model, and 6 threads produced a 61 s outlier. Each llama.cpp thread takes a whole CPU, and the CT shares the host with other workloads: the 5-second sampler saw the load average peak at 7.1 during the runs. How much of that came from the other workloads was not measured.
- **The watcher kept working** through ~50 minutes of back-to-back benchmarks: 7 cycles, 0 warnings or errors, at most 3 s late (8 s in the 6 hours before), no restarts, and both units active in all 595 samples.
- **Recommended unit settings:** `-t 4`, `-c 4096`, `--load-mode none`, `--image-max-tokens 512`, `MemoryMax=5G`. Use a **60 s per-story timeout** (≈2.3× the worst measured 4B latency) and fail open to "unsure" tags on a timeout (#11).

## Target hardware (stated facts)

| | |
|---|---|
| Service container (CT 120) | Unprivileged Debian 12 LXC, **resized on 2026-10-08 to 6 CPUs, 6 GB RAM, 512 MB swap and a 20 GB disk**. It runs the watcher (~30 MB) and the bot (~50 MB). No GPU device. |
| Host CPU | 6-core/12-thread desktop CPU with AVX-512 VNNI (and AVX2) |
| Host RAM | 15 GB total, shared with other workloads |
| Host GPUs | An integrated GPU (not passed to the CT) and an entry-level 2 GB card with no driver installed. |
| Input | **Still images only.** The archive saves still images (user decision, 2026-10-08), so this doc analyses one image per story, with no video frames. |
| Volume | A few to ~25 stories a day |
| RAM budget | **4–6 GB for the model service (confirmed by the user)** |

## Options compared

| | A. CPU-only in CT 120 (**recommended**) | B. GPU passthrough to the LXC | C. Separate inference machine over Tailscale | D. Hosted API (API key) | E. Claude via the user's Pro subscription |
|---|---|---|---|---|---|
| Fits a 2–4B VLM? | Yes: 3.0–3.9 GiB peak, measured | Entry-level 2 GB card: no (too little VRAM, no driver). Integrated GPU: shares system RAM | Yes, even 8B+ | n/a | n/a |
| Latency per story | **18–26 s measured** | iGPU: unmeasured, probably no faster than the CPU | Seconds | Seconds | Seconds to tens of seconds (CLI start-up) |
| Available when needed | Always | Always | Only when that machine is on; otherwise "unsure" (fail open) | When the provider is up | Until the Pro usage limit is hit (likely shared with the user's interactive use); then "unsure" (fail open) |
| Story data leaves the host | No | No | Stays on the tailnet | **Yes** (needs the user's OK) | **Yes** (needs the user's OK) |
| Marginal cost | ~0 (host already on) | ~0 | An always-on desktop: ~45–70 kWh/month | ~$0.30/month (Haiku 5.5) to ~$5/month (Sonnet 5.5), estimated | $0 marginal, but uses the user's subscription limits |
| Ops burden | One systemd unit, a model file, a pinned binary | Proprietary driver versions matched between host and CT on every update; or an unproven integrated-GPU runtime | A second machine, ACLs, wake-on-LAN or fallback, a reachability alert | A key to protect, spend monitoring, model deprecations | A long-lived OAuth token in `.env`; Claude Code installed on the server (CLAUDE.md says it isn't today); terms grey area |

### A. CPU-only in CT 120 (recommended)

It is always available, private, needs no new hardware, and its measured latency is well inside "tens of seconds per story".

- **Placement:** a second unit in CT 120, `story-watch-llm.service`, running `llama-server` bound to `127.0.0.1`.
  - Limits: `MemoryMax=5G`, `-t 4`, and either `CPUQuota=400%` or `AllowedCPUs=` set to four of the six, which keeps two CPUs free for the watcher and the bot.
  - `deploy/install.sh` installs it idempotently. It needs the `libgomp1` apt package. The prebuilt llama.cpp `ubuntu-x64` release runs on Debian 12 unchanged: it needs glibc 2.34 and GLIBCXX 3.4.30, which Debian 12 provides.
  - Restart policy: `Restart=on-failure` with a long `RestartSec`, so the unit never crash-loops.
- **llama.cpp, not Ollama:** a single pinned release with explicit flags (`--image-max-tokens`, `--load-mode none`, `-t`) and an OpenAI-compatible endpoint, and no background updater. Check the model file by SHA-256 in `install.sh`.
- **`--load-mode none`** (read the model into memory instead of mmap). On a workstation run, mmap made the 4B model's RSS 5.6 GB instead of 3.9 GB, because the CPU backend keeps a repacked copy of the weights. Cold load with this mode, with the model files evicted from the page cache first, took **3.4–3.6 s (2B) and 6.0 s (4B)**. It happens once, at service start.
- **Disk:** the 2B and 4B model files plus projectors take ~4.9 GiB, and the llama.cpp binaries ~45 MB. Ship only the chosen model in production (2.3–3.0 GB).

### B. GPU passthrough to the unprivileged LXC

- **Entry-level 2 GB card:** not worth it.
  - The smallest candidate is already about 2.3 GB before KV cache, and the card has 2 GB of VRAM on an older generation.
  - It would need the proprietary driver on the host, the same user-space version inside the CT, bind mounts for the card's device nodes and `lxc.cgroup2.devices.allow` rules, all re-matched on every host driver update.
- **Integrated GPU:** passing `/dev/dri/renderD128` into an unprivileged CT is easy (Device Passthrough with the CT's `render` GID). The runtimes would be llama.cpp's Vulkan backend or the GPU vendor's own inference toolkit. But it is a small integrated part sharing the same memory bandwidth as the CPU, and I found no VLM benchmarks for it. Since the CPU already meets the budget, there is no reason to try it now.

### C. A separate inference machine over Tailscale

The user's workstation has a discrete GPU. It would run 8B-class models in seconds, but it isn't always on. When it's off every story would be tagged "unsure", which is safe (pings fail open) but defeats the filtering. Keep it only as an upgrade path if #10 shows that ≤4B models miss pings.

### D. Hosted API (pay-per-use key)

This is the simplest route to high accuracy. In production it adds a key to protect (never logged, like `DISCORD_WEBHOOK`), an outside dependency, and sending story images to a third party, which needs the user's OK. **Estimated** cost at this volume: about $0.0004 per still-image story on Claude Haiku 5.5 and about $0.007 on Claude Sonnet 5.5, assuming ~2,000 input and ~300 output tokens. At ~25 stories a day that is roughly $0.30 and $5 a month. No hosted calls were made.

### E. Claude through the user's Pro subscription (headless Claude Code)

The service would call `claude -p` with `--output-format json` and `--json-schema`. Claude Code would read the image through `--allowedTools Read` from an empty working directory, authenticated by a `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` and stored as a secret in `.env`. See the [headless](https://code.claude.com/docs/en/headless) and [authentication](https://code.claude.com/docs/en/authentication) docs. Nothing was set up or called for this research.

- **Cost:** no marginal cost, but it draws on the user's Pro usage limits, which are likely shared with interactive use. On a usage-limit failure, tags fall back to "unsure" (fail open).
- **Terms:** they describe subscriptions as for ordinary, individual usage and point products to API keys. A personal background service is a grey area.
- **Privacy:** story images leave the host (needs the user's OK).
- **Repo rules:** CLAUDE.md says Claude Code is not installed on the server, so this would change that rule. The token follows the webhook rule: never logged, never in an exception message.
- **Best use:** it is the cheapest way to get a strong **accuracy reference for #10**, scored with the #6 harness on the labeled set, run from the workstation rather than the server. It is not recommended as the production path.

## Benchmarks (measured in CT 120)

**Setup:**
- **Inference:** llama.cpp `b11514` (prebuilt `ubuntu-x64` CPU build), `llama-server` with its OpenAI-compatible chat endpoint, `--load-mode none`, `-c 4096`, `temperature 0`, at most 256 output tokens.
- **Prompt:** a JSON tagging prompt asking for the transcribed text and all five dimensions.
- **Images:** two **synthetic** 1080×1920 JPEGs with invented text (a headline over a noisy photo-like background, and a dense job-post screenshot). Each image was sent 3 times (6 requests per row; 4 for the 1024 rows).
- **Cold load:** model files evicted from the page cache (`posix_fadvise(DONTNEED)`) before each start.
- **Memory:** peak RSS from `/usr/bin/time -v`.
- **Isolation:** each run held a shared CPU lock, so no other benchmark ran at the same time. Another worker's model process was resident (idle) in the CT during part of the window.
- **What "facts found" means:** a crude legibility check that the answer contains the strings planted in the image. It is not an accuracy score; #7 and #10 measure accuracy on real stories.

| Model (files) | Threads | Image tokens | Cold load | First request | Median | Worst | Peak RSS | JSON ok | Facts found |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3-VL-2B Q8_0 (1.8 + 0.45 GB) | **4** | 512 | 3.6 s | 15.1 s | **18.2 s** | 21.4 s | **3.0 GiB** | 6/6 | 27/27 |
| Qwen3-VL-2B Q8_0 | 6 | 512 | 3.4 s | 16.0 s | 21.9 s | 60.9 s | 3.0 GiB | 6/6 | 27/27 |
| Qwen3-VL-2B Q8_0 | 6 | 1024 | 3.4 s | 34.4 s | 37.8 s | 41.0 s | 3.3 GiB | 4/4 | 18/18 |
| Qwen3-VL-4B Q4_K_M (2.5 + 0.45 GB) | **4** | 512 | 6.0 s | 18.9 s | **22.8 s** | 26.2 s | **3.9 GiB** | 6/6 | 27/27 |
| Qwen3-VL-4B Q4_K_M | 6 | 512 | 6.0 s | 22.6 s | 25.9 s | 36.2 s | 3.9 GiB | 6/6 | 27/27 |
| Qwen3-VL-4B Q4_K_M | 6 | 1024 | 6.0 s | 38.7 s | 45.1 s | 49.3 s | 4.1 GiB | 4/4 | 18/18 |
| Gemma 3 4B QAT Q4_0 (2.5 + 0.85 GB) | 4 | fixed 256 | 6.0 s | 33.7 s | 37.9 s | 41.7 s | 3.9 GiB | 6/6 | 27/27 |
| Gemma 3 4B QAT Q4_0 | 6 | fixed 256 | 6.0 s | 34.4 s | 38.6 s | 42.7 s | 3.9 GiB | 6/6 | 27/27 |

What the table shows:
- **Image tokens drive latency.** For Qwen3-VL, 1024 image tokens cost about twice the latency of 512. Image encoding takes 11–15 s at 512; generating the ~120-token answer takes 7–10 s. Shorter answers would cut the second part.
- **Gemma 3 4B is slower despite fewer image tokens.** Its vision encoder (896 px, f16 projector) dominates. Without a clear accuracy win in #7 it is not worth carrying.
- **8B-class models were not tried in the CT.** Their weights alone (~5 GB at Q4_K_M plus a 0.75 GB projector) leave no room in a 5 GB `MemoryMax`.

### Effect on the watcher and the bot

Measured over the whole benchmark window (about 50 minutes, ~23:00–23:50 local) from the journal and a 5-second sampler. Only aggregates were recorded, because the journal contains account names:

| | During benchmarks | Baseline (6 h before) |
|---|---|---|
| Watcher cycles completed | 7 | 48 |
| WARNING / ERROR lines (watcher) | 0 / 0 | 5 / 0 |
| WARNING / ERROR lines (bot) | 0 / 0 | 4 / 0 |
| Worst lateness of a watcher cycle vs. its announced "next check in N s" | 3.0 s | 8.0 s |
| Unit restarts (watcher, bot) | 0, 0 | 0, 0 |
| Samples with either unit not `active` | 0 of 595 | – |
| Lowest `MemAvailable` in the CT | 754 MiB | – |
| Peak swap used | 85 MiB | – |
| Watcher memory (max) | 27 MiB | – |

The watcher spends almost all its time sleeping between cycles, so a model using 4 CPUs did not delay it. The lowest available memory (754 MiB) came while a 4B model and another worker's idle model process were both resident. In production, only one model runs, under `MemoryMax=5G`.

### Side note: workstation numbers (not the target)

Before CT 120 was resized, the same harness ran on a development workstation (8-core desktop CPU, DDR5). At 6 threads and 512 image tokens it measured 15.6 s (2B) and 18.9 s (4B) per image. Those numbers are kept in `research/inference-hosting/results/workstation.jsonl` for comparison only. **The earlier scaled estimates for the host are replaced by the CT 120 measurements above.**

## Running costs

- **Power:** the host is on anyway. At ~25 stories a day × ~25 s with 4 CPUs busy, the model adds about 10 minutes of CPU work a day, a few Wh. Resident RAM costs nothing extra.
- **Disk:** one model with its projector is 2.3–3.0 GB, plus 45 MB of binaries. CT 120's disk is shared with the archive and the database, so keep at least 2 GB free.
- **Model updates:** pin the llama.cpp release and the model's SHA-256 in `install.sh`. Update deliberately, re-score with the #6 harness before deploying, and never auto-update.
- **Monitoring:** reuse the existing alert path.
  - The watcher calls `GET /health` on the local server.
  - A failed request or a timeout means the story is posted with "unsure" tags (fail open, #11) and counts toward a "tagger unavailable" alert, sent once and followed by "recovered".
  - `MemoryMax` makes an out-of-memory kill hit the model, not the watcher.
  - The daily heartbeat can include counts of stories tagged and fallbacks.

## Recommendation

1. **Run CPU-only in CT 120.** A `story-watch-llm` systemd unit runs `llama-server` (llama.cpp, a pinned release) with:
   - `-t 4 -c 4096 --load-mode none --image-max-tokens 512`, bound to 127.0.0.1;
   - `MemoryMax=5G` and four of the six CPUs. The headroom is thin: the 4B model peaked at 3.9 GiB (4.1 GiB at 1024 image tokens), and the CT has 6 GB in total. The unit must not run alongside any other model (a second model, a benchmark, or #7's tools) in the same CT;
   - the apt package `libgomp1`.
2. **Candidate models for #10:** **Qwen3-VL-4B Q4_K_M** (23 s median, 3.9 GiB) and **Qwen3-VL-2B Q8_0** (18 s median, 3.0 GiB). They are close in speed, so #10's accuracy decides. Drop Gemma 3 4B (38 s) unless #7 shows a clear accuracy win. Nothing above ~4B fits the 5 GB limit.
3. **Timeout: 60 s per story,** about 2.3× the worst measured 4B latency at 4 threads. On a timeout or error, post with "unsure" tags (fail open).
   - **For #12:** tag stories one at a time and post each as soon as it is tagged or times out, so a burst of N new stories doesn't hold the first one for N × 25 s.
4. **No GPU work.** Neither the entry-level card nor the integrated GPU is needed at this volume.
5. **Accuracy reference for #10:** the user's Pro subscription via headless Claude Code (option E), or a paid API key (option D). Run it from the workstation on the labeled set, only with the user's OK to send those images. Neither is recommended for production.

### What this means for #10

- Compare **Qwen3-VL-2B Q8_0 and Qwen3-VL-4B Q4_K_M** through llama.cpp at **512 image tokens**. Add 1024 only if 512 misses small text on real stories; it doubles latency.
- Use the CT 120 numbers above as the latency column: ~18–23 s median, a **60 s budget**.
- Pipeline 2 (OCR + text model) must fit the same 5 GB and 60 s. Pipeline 1 (one VLM call) already does.
- **Sampling twice or requiring two models to agree** doubles the cost to ~40–50 s per story. That needs its own time budget: two calls don't fit inside one 60 s per-story timeout at the worst measured latency (2 × 26 s plus overhead). Either give the job-posting decision a separate, larger budget (e.g. 120 s), or run the second call only when the first is unsure. Two different models also can't both stay loaded, because of the memory limit below.
- The **accuracy ceiling** comes from option E or D, run offline with the user's OK.

## Open questions for the user

1. **CPU share.** Recommend 4 of the CT's 6 CPUs for the model (`-t 4`, `CPUQuota=400%`). 4 threads measured faster than 6. OK?
2. **Timeout.** Recommend **60 s per story**, then post with "unsure" tags. OK?
3. **Model for production** (decided in #10). Recommend Qwen3-VL-4B Q4_K_M if #10 shows it is more accurate than the 2B, and otherwise the 2B, which is slightly faster and smaller.
4. **Accuracy reference for #10.** Recommend option E (Pro subscription, headless Claude Code) run from the workstation on the labeled set. It costs no money, but it sends those story images to Anthropic and uses your Pro limits. Is that OK, or do you prefer option D (a paid API key: a few cents per ~100 stories on Haiku-class, under $1 on Sonnet-class), or no hosted reference at all?
5. **Workstation GPU.** Recommend leaving it out unless #10 shows that ≤4B models miss pings.

## Sources

- llama.cpp release `b11514` and the multimodal server docs (`tools/mtmd`).
- Qwen3-VL GGUF model cards (Qwen, ggml-org) and the Gemma 3 4B QAT GGUF (ggml-org) on Hugging Face.
- Proxmox forum threads on GPU device passthrough (`/dev/dri` and proprietary-driver cards) to unprivileged LXCs.
- Claude Code docs: [headless mode](https://code.claude.com/docs/en/headless), [authentication](https://code.claude.com/docs/en/authentication).
- Claude API pricing as of 2026-10 (Haiku 5.5: $0.10/$0.50 per M tokens; Sonnet 5.5: $2/$10).
