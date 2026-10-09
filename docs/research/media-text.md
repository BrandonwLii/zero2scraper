# Reading text from story images (issue #7)

Part of epic #2. Feeds #10 (tagger comparison). Where the model runs, its RAM budget and its unit settings are #8's findings in [`inference-hosting.md`](inference-hosting.md). Prototypes: [`research/media_text/`](../../research/media_text/README.md).

**Status: provisional.** Every number below comes from **invented, synthetic story images**. No real story media was available on the workstation, and fetching it from Instagram's CDN wasn't allowed. The harness reads the service archive layout and the labeling bot's `labels.jsonl` unchanged, so the conclusions should be re-checked on about 30 hand-transcribed real stories before #10 commits to a reader. The exact commands are in [Rerun on real stories](#rerun-on-real-stories).

Stories are classified from a single still image.

## TL;DR

- **One vision-language model (VLM) call per image is the best reader measured.** Qwen3-VL-2B-Instruct (Q4_K_M, Q8_0 vision projector) in llama.cpp's `llama-server`, CPU only, returning the text plus the tagging facts as JSON, put 99–100 % of the reference facts in the right JSON field and never invented a company. In CT 120 (6 threads) it took 14–26 s per image depending on image tokens and image size.
- **Run llama-server with `--cache-ram 0`.** Its default 8 GB prompt cache keeps every finished prompt (with its image) in RAM, so RSS grows with each new image. With only that flag changed, the 2B server's peak went from 5.9 GB to 3.2 GB. See [Memory](#memory-in-ct-120).
- **Keep RapidOCR as the cheap safety net.** It reads 93–97 % of the facts in about 0.5 s with about 0.5 GB. It's the fallback when the model server is down or over budget, so a story is never left with no text at all.
- **The 4B model: #7 has no usable measurement; use #8's.** #7's one 4B run used 1024 image tokens, llama-server's default 8 GB prompt cache and the default mmap model load. Under those settings it reached 5.8 GB with swap full and about 81 s per image, and it was stopped. #8 measured the same Qwen3-VL-4B Q4_K_M in the same CT at 512 image tokens with `--load-mode none` at **3.9 GiB and 23 s** per image (4.1 GiB at 1024 tokens). So the 4B fits the budget under #8's settings; #7 has no accuracy numbers for it.
- **512 vs 1024 image tokens: the synthetic test can't tell them apart.** On the hard set (27 images, 91 facts) both scored 91/91 field recall; 512 was about 32 % faster. The test is saturated, so this doesn't show that 512 loses nothing on real stories. See [512 vs 1024 image tokens](#512-vs-1024-image-tokens).
- **For #10:** pipeline 1 (one VLM call) is the main candidate, with the prompt and schema in `engines.py`. Pipeline 2 (OCR, then a text model) loses the facts OCR drops and gained nothing on this data. See [What this means for #10](#what-this-means-for-10).

## Target hardware (stated facts)

The reader would run on the Proxmox host that runs the service, CPU only. The coordinator measured these facts on 2026-10-08:

| | |
|---|---|
| Host CPU | A 6-core/12-thread desktop CPU with AVX-512 VNNI (and AVX2) |
| Host RAM | About 15 GB, shared with other workloads |
| GPUs | No usable GPU: an integrated GPU not passed to the container, and an entry-level card with no driver |
| CT 120 (the service's LXC) | Normally 1 core / 512 MB RAM / 4 GB disk. **Resized to 6 cores / 6 GB RAM / 20 GB disk for these benchmarks**, with the watcher and the bot still running |
| Load | A few stories a day. Tens of seconds per story is acceptable |

The RAM budget for the model service (4–6 GB, `MemoryMax=5G`) was confirmed by the user and is set in #8.

A development workstation (8-core desktop CPU; its discrete GPU isn't usable from WSL) was used only for relative comparisons before the target was fixed. Its latencies aren't comparable to the target and are labeled "PC".

## What Instagram provides

Instagram's `accessibility_caption` (its own alt text, sometimes "may be an image of text that says…") is **null on every cached item**, so there is no free OCR from Instagram.

## Method

**Synthetic set** (`synth.py`). Story-sized images (1080×1920, then downscaled and JPEG-compressed the way the CDN does, at 640–1080 px wide) of five kinds:

- overlay text ("<company> is hiring!", role, location);
- a small job-board screenshot card;
- a three-row roundup;
- an interview-process post;
- a misc post with no facts.

They use stylized open fonts (script, condensed, serif, thin), Instagram-like styles (plain, outline, shadow, white box), rotation, and sticker shapes that sometimes cover text. The **hard** variant adds sharp clutter behind the text, low-contrast plain text, a fixed 640 px width and JPEG quality 55. Each set has 27 images. Companies are well-known names, because the FAANG+/Quant lists in `docs/tags.md` depend on them. Everything else is invented.

**Score.** The reference lists the *facts that matter for tagging* in each image: company, role title, level wording, location, and sponsorship or work-authorization wording. That gives 90 facts in the normal set and 91 in the hard set. Two numbers per engine:

- **text recall**: the fact appears (fuzzy, case-insensitive) anywhere in the engine's text. This is the ceiling for any later text model.
- **field recall** (models that output JSON): the fact appears in the *right field* (`companies`, `roles`, `levels`, `locations`, `work_authorization`).

**Invented companies** counts companies an engine reports that aren't in the image. A tagger that hallucinates a FAANG name would cause wrong pings. Character error rate wasn't used as the headline, as the issue asks.

**Measured in CT 120:** 6 threads, one run at a time under a shared `flock`, peak RSS from `/usr/bin/time -v` (Python engines) or the server's `VmHWM`, per-image latency (first image and median), and model load time. llama.cpp release `b11514`, default (mmap) model loading.

## Results

### In CT 120 (the target), 6 threads

| Engine | Set | Text recall | Field recall | Invented companies | Load | 1st image | Median / p90 per image | Peak RSS |
|---|---|---|---|---|---|---|---|---|
| RapidOCR (PP-OCR models, ONNX) | normal | 96.7 % | — | — | 0.5 s | 0.8 s | 0.7 / 1.1 s | 0.7 GB |
| RapidOCR | hard | 93.4 % | — | — | 0.4 s | 0.6 s | 0.5 / 0.8 s | 0.5 GB |
| Florence-2-base (0.23B, `<OCR_WITH_REGION>`) | normal | 98.9 % | — | — | 12.6 s (4.2 s warm cache) | 4.4 s | 3.8 / 5.8 s | 2.1 GB |
| Florence-2-base | hard | 97.8 % | — | — | 4.2 s | 3.6 s | 4.2 / 5.7 s | 1.8 GB |
| **Qwen3-VL-2B Q4_K_M, extract, 1024 image tokens, `--cache-ram 0`** | hard | 98.9 % | **100 %** | **0** | 2.0 s | 19.1 s | **20.9 / 22.5 s** | **3.2 GB** |
| Qwen3-VL-2B, same settings | normal | 98.9 % | 98.9 % | 0 | 2.0 s | 26.2 s | 26.2 / 29.1 s | 3.3 GB |
| **Qwen3-VL-2B, 512 image tokens, `--cache-ram 0`** | hard | 98.9 % | **100 %** | **0** | 2.0 s | 13.5 s | **14.2 / 16.0 s** | **3.1 GB** |
| Qwen3-VL-2B, 1024 image tokens, default prompt cache (8 GB) | hard | 98.9 % | 100 % | 0 | 3.0 s | 19.9 s | 20.4 / 22.1 s | 5.9 GB peak (grows per image) |
| Qwen3-VL-2B, default prompt cache, under a 4.5 GB watchdog | normal | — | — | — | | | | killed at 4.66 GB after 11 images |
| Qwen3-VL-4B Q4_K_M, 1024 image tokens, default prompt cache, mmap load | hard | — | — | — | | | ~81 s | 5.8 GB, swap full (stopped; not rerun). **Not representative: see #8's 3.9 GiB below** |

Notes:

- The Qwen runs used `-c 4096 -np 1` and 1024 image tokens unless the row says otherwise. A first 2B run with `-c 8192` and the model's default image tokens scored the same (100 % field recall, about 20.6 s per image).
- The normal set is slower than the hard set for the VLM (26 s vs 21 s) because its images are larger on average (up to 1080 px wide vs a fixed 640 px), so they produce more image tokens.
- Florence-2's first load includes the model download. The "warm cache" figure is the second run.
- **Comparison with #8.** #8 measured Qwen3-VL-2B **Q8_0** and Qwen3-VL-4B Q4_K_M in the same CT with `--load-mode none`, on two 1080×1920 images with a tagging prompt: 2B 3.0 GiB (3.3 GiB at 1024 tokens), 4B 3.9 GiB (4.1 GiB at 1024 tokens), 18–23 s median at 4 threads and 512 tokens. Those are the memory and latency figures to plan with. #7's latencies differ because of the quantization, the image sizes and the prompt, and its 4B row differs because of the prompt cache and mmap loading (#8 saw mmap raise the 4B's RSS from 3.9 to 5.6 GB on its own).

### On the PC (relative comparison only, all threads unless noted)

27 images per set, from the per-item outputs.

| Engine | Set | Text recall | Field recall | Median per image (PC) | Peak RSS (PC) |
|---|---|---|---|---|---|
| RapidOCR | normal / hard | 96.7 % / 93.4 % | — | 0.4 / 0.2 s | 0.4–0.75 GB |
| docTR (db_resnet50 + crnn_vgg16_bn) | normal / hard | 93.3 % / 89.0 % | — | 1.3 s | 1.8 GB |
| EasyOCR | normal | 81.1 % | — | 3.0 s | 2.2 GB |
| Florence-2-base | normal / hard | 98.9 % / 97.8 % | — | 3.0 / 3.6 s | 1.8–1.9 GB |
| Florence-2-large (0.77B) | normal | 98.9 % | — | 12.2 s | 5.4 GB |
| RapidOCR → Qwen3-4B (text model, JSON) | hard | 93.4 % | 93.4 % | 6.1 s (6 threads) | 4.5 GB |

Florence-2-large was no better than base, at 4× the time and 3× the memory.

### 512 vs 1024 image tokens

What #7's data shows (Qwen3-VL-2B Q4_K_M, `--cache-ram 0`, CT 120, 6 threads):

| Image tokens | Set | Images | Facts | Text recall | Field recall | Invented companies | Median per image |
|---|---|---|---|---|---|---|---|
| 1024 | hard | 27 | 91 | 90/91 (98.9 %) | 91/91 (100 %) | 0 | 20.9 s |
| 512 | hard | 27 | 91 | 90/91 (98.9 %) | 91/91 (100 %) | 0 | 14.2 s |
| 1024 | normal | 27 | 90 | 89/90 (98.9 %) | 89/90 (98.9 %) | 0 | 26.2 s |
| 512 | normal | — | — | not run | not run | — | — |

- **The test is saturated.** Both settings hit the ceiling on the hard set. A difference smaller than about one fact in 91 can't show up, and the synthetic screenshots were read in full by every engine, OCR included, so small text was never the limiting case.
- So the data supports only this: **on 27 hard synthetic images, 512 tokens lost no measured fact and was about 32 % faster.** It doesn't show that 512 is as accurate as 1024 on real stories, especially dense screenshots with small text. 512 wasn't run on the normal set.
- #8 uses 512 as the default and measured 1024 at about twice the latency on 1080×1920 images. Nothing here argues against that default.
- **Open question for #10:** compare 512 and 1024 on real, labelled stories, with dense screenshots in the sample.

### Answers to the issue's questions

**1. How well does each option read story text?** On clean-ish overlays and screenshots, every engine except EasyOCR gets more than 93 % of the facts. The differences show up on stylized text over busy backgrounds:

| Engine (hard set) | Overlay text | Roundup rows | Screenshot card (small text) |
|---|---|---|---|
| RapidOCR | 88 % | 89 % | 100 % |
| docTR (PC) | 77 % | 89 % | 100 % |
| Florence-2-base | 100 % | 93 % | 100 % |
| Qwen3-VL-2B (field recall) | 100 % | 100 % | 100 % |

Small job-board screenshots, the case the issue worried about, were read fully by every engine down to 640 px width. The hard part is decorative display text.

**2. OCR then a text model, or one vision-language model?** One VLM call is better on this data.

- An OCR → text-model pipeline can't recover a fact the OCR dropped. RapidOCR → Qwen3-4B got exactly RapidOCR's ceiling (93.4 %). Its JSON fields were correct whenever the OCR text was, and it invented no companies.
- The VLM read words that OCR garbled or split, and it was the only engine that put a sticker-clipped company name back together. That was a correct reconstruction, not an invention; the invented-company count stayed at 0. It's also simpler: one model and one call, instead of an OCR model plus an LLM.
- The 2-stage pipeline is faster (about 6 s on the PC vs 14–21 s in the CT). That matters little at a few stories a day.
- Its real advantage is **graceful degradation**: RapidOCR alone still gives usable text when no model is running.

### Failure examples (paraphrased, synthetic)

- **Condensed all-caps font, rotated about 9°, low-contrast plain text on clutter.** RapidOCR returned a garbled line for the headline (company and role lost) but kept the location line. docTR lost the same lines. Florence-2 and Qwen3-VL read it.
- **A sticker covering the end of a company name in a roundup row.** Every OCR engine returned the truncated fragment, so the company was lost. A city on another row was clipped too.
- **Thin serif text on a light area.** RapidOCR skipped a whole roundup row (company and city).
- **Script font with a rotated headline.** docTR turned a company name into nonsense and misspelled "New Grad" as a different word. EasyOCR failed on most script and condensed overlays (52 % of overlay facts).
- **No hallucinated companies** on the misc posts from any JSON-producing engine. Note that the synthetic misc posts have no logos. Real stories with logos but no company name are untested.

## Memory in CT 120

- **The prompt cache, not the model, made the first 2B runs too big.** llama-server's `--cache-ram` defaults to 8192 MiB and stores each finished prompt (with its image) in RAM. With a new image every request, RSS climbed steadily: the 2B reached a 5.9 GB peak while its steady footprint was about 3.2 GB. **Use `--cache-ram 0`.** The service never sends the same prompt twice, so the cache is useless here. #8's runs sent two distinct images three times each, which likely gave the cache little to grow on; production sends a new image every time.
- With `--cache-ram 0` the 2B server peaked at **3.1–3.3 GB** (512 or 1024 image tokens), flat across 27 images, inside a cgroup capped at 4.5 GB with no swap.
- Every server in the CT ran under `systemd-run --scope -p MemoryMax=4500M -p MemorySwapMax=0`, or a watchdog that kills it above 4.5 GB, so it couldn't starve the watcher. The production unit gets `MemoryMax=5G` (#8).
- Disk: llama.cpp CPU build about 50 MB; Qwen3-VL-2B Q4_K_M 1.1 GB plus 0.45 GB projector. RapidOCR models are about 15 MB, and onnxruntime is about 50 MB installed.

## Options not tried, and why

- **Tesseract.** No root on the workstation for `apt`. In the CT it's an `apt` package, but it isn't expected to beat RapidOCR on stylized text over photos. Skipped as low value.
- **PaddleOCR proper.** RapidOCR runs PaddleOCR's own PP-OCR detection and recognition models through onnxruntime, without the heavy paddlepaddle dependency, so it stands in for PaddleOCR. **PaddleOCR-VL, MiniCPM-V, InternVL, Gemma 3/4:** downloaded on the PC, but the PC run was stopped before they ran (benchmarks moved to the CT). Gemma 3 4B was measured for speed in #8 (38 s per image).
- **Qwen2.5-VL-3B:** superseded by Qwen3-VL at the same size.
- **The host's integrated GPU (OpenVINO).** Untested. It would need `/dev/dri` passed into an LXC, and OpenVINO builds of the models. The CPU latency is already acceptable.
- **A hosted vision model** as an accuracy reference or fallback. **Not run:** no paid API calls in this task. #8 covers it as #10's accuracy reference; it would send story images to a third party, which needs the user's OK.

## Recommendation

1. **Reader:** one Qwen3-VL call per image through `llama-server`, with #8's unit settings (`-t 4 -c 4096 --load-mode none --image-max-tokens 512`, `MemoryMax=5G`) **plus `--cache-ram 0`**. Use the JSON schema and prompt in `engines.py` (`EXTRACT_SCHEMA`, `EXTRACT_IMAGE_PROMPT`). 2B or 4B is #10's decision; #7 only measured the 2B's accuracy.
2. **Fallback:** RapidOCR text when the model server is down, slow (time-out) or OOM-killed. The tagger then gets OCR text plus links and widens its sets, per the uncertainty contract in `docs/tags.md`. Stories must never wait on the model.
3. **Re-run on real stories** (30 hand-transcribed) before #12 builds it. Watch for logos without names, handwriting, non-English text and dense screenshots.

## What this means for #10

- **Pipeline 1 (one VLM call)** is the main candidate. The image plus the link, mention and job-page text go in one prompt, and all five dimensions come out as structured JSON. Add the job-page text (#9) as extra prompt tokens; text prefill is cheap next to the image.
- **Pipeline 2 (OCR, then a text model)** is worth keeping in #10 only as the degraded mode. Its ceiling is OCR recall (93 % on the hard synthetic set), and every fact OCR drops is a possible missed ping.
- **Compare 512 and 1024 image tokens on real labelled stories.** #7's synthetic test is saturated at both.
- **A cheap accuracy boost to try:** give the VLM the RapidOCR text as a hint alongside the image (+0.5 s). Also: the extraction step should ask for the facts verbatim and let the rules map them to tags (company lists, level words). The VLM's job is reading, not judging the taxonomy.
- Always run the server with `--cache-ram 0` in #10's benchmarks too, or memory readings over many images will be inflated.

## Rerun on real stories

From `research/media_text/` on the workstation, after `scripts/pull_archive.py`:

    A=~/story-watch-data/archive
    .venv/bin/python bench.py truth-template --archive $A --n 30   # varied sample from labels.jsonl
    # fill in $A/media_text_truth.todo.jsonl by hand, append to $A/media_text_truth.jsonl
    .venv/bin/python bench.py run --archive $A --engines rapidocr

For the model, copy only scripts into the target box (`ct_bench.sh setup && ct_bench.sh models`). Real story media must not leave the workstation unless the user agrees. Alternatively, run `llama-server` with the flags above on the workstation and point `LLAMA_SERVER` at it:

    .venv/bin/python bench.py run --archive $A --engines vlmcpp:q2b,rapidocr+llmcpp:q2b

## Open questions for the user

1. **Hand-transcribing 30 real stories** for the rerun: about an hour of work. Who does it, and when? Recommendation: do it once the archive has about 2 weeks of stories, using `truth-template`.
2. **Hosted fallback in production:** allow sending story images to a hosted model when the local one fails? Recommendation: **no**. RapidOCR as the fallback keeps everything local. (A hosted model as #10's offline accuracy reference is #8's open question 4.)
