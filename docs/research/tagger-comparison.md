# Comparing tagging pipelines on the labelled set (issue #10)

Part of epic #2. Uses #6's harness, #7's reader findings ([media-text.md](media-text.md)), #9's posting rules ([job-pages.md](job-pages.md)) and #8's hosting findings ([inference-hosting.md](inference-hosting.md)). Prototypes: [`research/tagger/`](../../research/tagger/README.md). **Status: recommendation drafted. The user signs off before #12.**

## TL;DR

- **Recommendation: pipeline 3, the hybrid, with Qwen3-VL-4B Q4_K_M as its model.** One local VLM call per story at 512 image tokens decides the post type and the role; fixed rules decide company (the `docs/tags.md` lists), sponsorship (#9's rules on the saved posting, plus Tesla as a known sponsor) and level (title words); a few contract guards widen sets where the model is known to be overconfident. The exact configuration is in [Recommendation](#recommendation).
- **On the 48 labelled stories it missed no pings** under the 10 user configs in `eval/ping_configs.toml`, with 26 extra pings (of 401 possible). Claude Sonnet, the accuracy reference, also missed none, with 25–28 extra. With the 2B model instead the hybrid also missed none but gave 40 extra pings, and it ruled out a true value on 15 stories against 9 for the 4B.
- **Every model needs the rules.** The models alone, with no rules, all missed pings or ruled out true values often. The 4B alone missed 12 pings, and the 2B alone ruled out a true value on 31 of 48 stories, with errors spread over role, level, sponsorship and company. Claude alone was close (1 missed). The rules fixed most of the local models' errors: the models are good readers, but they don't follow the taxonomy.
- **Measured in CT 120** (4 threads, `nice 10`, a 4.5 GiB no-swap scope, the watcher and the bot running): 4B **36 s median, 41 s p95, 57 s worst, 3.8 GiB peak RSS**; 2B Q4_K_M 20.5 s median, 27 s worst, 2.4 GiB. RapidOCR adds about 1 s and 0.75 GiB. It changed the hybrid's tags on 8 stories, but its effect on the scores was small: one missed ping and two company sets. The watcher kept polling through about 1.75 hours of benchmarks.
- **These numbers are optimistic.** The set is small (48 stories, 30 job postings, many values with fewer than 10 stories), and I wrote the rules and guards while looking at the errors on this same set. Freeze this configuration now and re-score it on the next ~30 labelled stories before #12 ships. Those stories are a true held-out set.

## Data and method

**The labelled set.** It has 48 archived stories, every one labelled (the last `labels.jsonl` line per story wins). The user reviewed the labels on 2026-10-09, so they are ground truth. Stories are still images only: no video, frame sampling or speech. The one exception is an older story whose archive entry has only its clip; its first frame stands in for the still it lacks (one frame, no other video content). Leaving that story out doesn't change the recommended pipeline's result (0 missed, 26 extra, 9 stories ruling out a true value, on 47 stories). Label counts (a multi-value label counts once per value):

| Dimension | Values (stories) |
|---|---|
| Post type | job posting 30, event 8, process info 5, misc 6 |
| Sponsorship (job postings) | sponsor or Canadian 3, no sponsor 9, unknown 18 |
| Company | FAANG+ 15, quant 8, other 20 |
| Role | ML 4, SWE 27, PM 4, other 11 |
| Level | internship 29, new grad 17, other 4 |

38 stories have a link sticker. Every labelled job posting in this set has one.

**Inputs every pipeline gets.** The still image (or its OCR text), the link sticker's host and path, mentions, and the saved job listing for the link: #9's extractor output, saved once, so every pipeline sees the same pages and nothing was fetched live. Of the 38 linked stories, 28 listings were read (`ok`), 3 were closed and 7 failed (bot challenge, JavaScript-only or HTTP errors). The prompt gets the listing's title, company, locations, employment type, the first 600 characters of the description, and every sentence about work authorization (#9's `work_auth_sentences`).

**Scoring adjustments.**
- The label review marked 8 stories whose sponsorship is **unverifiable** (closed or unreadable listing, image silent). 7 of them are in this set (the 8th story was removed). Their sponsorship is left out of scoring: not counted in sponsorship accuracy, and for pings the tagger's sponsorship is replaced by the label's, so that dimension can neither miss nor add a ping. Sponsorship accuracy is therefore over 23 job postings, not 30.
- A job posting with no link and sponsorship `unknown` would be scored as unsure (all three values), because the image can't say more. No story in this set has that case.

**Metrics.** These use #6's harness functions (`story_watch/evaluation/metrics.py`), driven by `research/tagger/score.py`:
- **Missed pings**, the headline: the true tags would ping a user and the predicted tags would not. It uses the 10 configs in `eval/ping_configs.toml` (69 true pings in total).
- **Extra pings:** the reverse (401 is the ceiling, reached by "no idea").
- **One-value users:** 20 synthetic users, one per tag value (`research/tagger/ping_configs_single.toml`), 182 true pings. They are more sensitive than the 10 configs. I also give the count without the "misc only" user, which no one is expected to be.
- **Stories ruling out a true value:** stories where any dimension's set leaves out a labelled value. A story counts once, however many dimensions are wrong. This is the risk that a missed ping hides behind.
- **Per dimension:** exact (the set equals the label), covers (no labelled value ruled out) and mean set size.

**Where the numbers come from.**
- **CT 120:** every latency and memory figure, and the replies the local models are scored on. The benchmark used the same request bodies as development, under the shared CPU lock, `-t 4`, `nice -n 10` and `systemd-run --scope -p MemoryMax=4.5G -p MemorySwapMax=0`. A 5-second sampler recorded memory and the watcher's state, with a kill switch below 1 GiB available.
- **Workstation:** prompt development (the same 2B model, CPU only, never the GPU), and the Claude calls. At temperature 0 the CT's and the workstation's 2B replies were byte-identical on 23 of 48 stories (different CPUs give different floating-point results), so the scores below use the CT replies.
- **Claude:** `claude -p --model sonnet --output-format json --json-schema …` on the workstation, one call per story, with the image file read through the Read tool. It ran 2 full passes (96 calls) with the same prompt, and hit no usage limit.

## Pipelines

| # | Name | What it does |
|---|---|---|
| 1 | `vlm` | One Qwen3-VL call per image through `llama-server`: the image, plus the link and listing text, gives JSON for all five dimensions under the uncertainty contract, plus the company names and job titles read off the image. `*_raw` is the model's output as is. Without the suffix it adds the **contract guards**: a link keeps `job_posting` in the post type (unless the read page is an event page); a bare `{unknown}` sponsorship on a link whose page wasn't read becomes all three values; Tesla is `sponsor_or_canadian`. |
| 2a | `ocr_rules` | RapidOCR text plus link and listing, through keyword and title rules only. No model. |
| 2b | `ocrtext` | RapidOCR text plus link and listing, through the 2B model used as a text model (no image). |
| 3 | `hybrid` | The model's post type and role, with rules for the rest. **Company:** the `docs/tags.md` FAANG+ and Quant lists (parsed from the doc), matched on the listing company, the link host, the company names the model read and the OCR text. A named company on neither list is `other`. Event-platform links (meeting and form hosts) don't count as the employer. **Sponsorship:** #9's rules on a read posting, plus refusal phrasings from the user's decisions (US-school enrolment, "US-based applicants only", "must be a U.S. person"); Tesla is a known sponsor; an unread posting gets all three values unless the image itself says Canada or refuses. **Level:** title words; for job postings only, #9's years-of-experience fallback. **Guards:** on top of pipeline 1's, a non-job post for students gets `{internship, new_grad}`; a story with no link that the model calls a job posting or misc keeps `process_info` when the image text has hiring-process words. `_rolerules` also takes the role from the listing or story title when title words settle it. `_noocr` drops the OCR text. |
| 4 | `claude` | Claude Sonnet (Pro plan, headless Claude Code), with the same prompt, inputs and schema, run raw, with the guards, and as the model inside the hybrid. |
| — | `listing_rules` | Pipeline 2a without OCR: what's left when no model is available (the fallback). |
| — | `union` | The per-dimension union of the 2B hybrid and `ocr_rules`. |
| — | `unsure`, `rules` | Baselines: "no idea" (`Tags.unsure()`), and today's placeholder classifier. |

The prompt is `research/tagger/prompts/tag_v1.txt`. It holds the definitions from `docs/tags.md`, the company lists, an instruction to list every value the model can't rule out, and three invented examples. **Few-shot examples from the labelled set weren't used**, so no story is scored on its own example. Output is constrained by a JSON schema (enums, `minItems: 1`), so every reply parsed.

## Results

All local-model rows use replies generated in CT 120. Claude rows use pass 1 (`v1`) unless they say `v1b` (pass 2, the same prompt).

### Pings

| Pipeline | Missed (10 configs, 69 true) | Extra (of 401) | Missed, one-value users (182 true), excl. misc-only | Extra, one-value users | Stories ruling out a true value (of 48) |
|---|---|---|---|---|---|
| `unsure` (no idea) | 0 | 401 | 0 | 764 | 0 |
| `rules` (today's placeholder) | 13 | 276 | 21 | 367 | 13 |
| `listing_rules` (fallback) | 1 | 185 | 5 | 333 | 4 |
| 2a `ocr_rules` | 1 | 121 | 4 | 196 | 8 |
| 2b `ocrtext` 2B | 3 | 99 | 21 | 178 | 30 |
| 2b + hybrid rules (`hybrid_ocrtext`, role rules) | 0 | 50 | 6 | 95 | 16 |
| 1 `vlm` 2B Q4_K_M, 512, raw | 1 | 80 | 26 | 130 | 31 |
| 1 `vlm` 2B Q4_K_M, 512, guards | 1 | 89 | 26 | 154 | 31 |
| 3 `hybrid` 2B Q4_K_M, 512 | 0 | 43 | 11 | 85 | 21 |
| 3 `hybrid` 2B Q4_K_M, 512, role rules | **0** | 40 | 6 | 76 | 15 |
| 3 same, no OCR | 1 | 37 | 6 | 72 | 16 |
| union (2B hybrid ∪ `ocr_rules`) | 0 | 133 | 3 | 224 | 7 |
| 1 `vlm` 4B Q4_K_M, 512, raw | 12 | 14 | 25 | 27 | 23 |
| 1 `vlm` 4B Q4_K_M, 512, guards | 11 | 25 | 20 | 55 | 22 |
| 3 `hybrid` 4B Q4_K_M, 512 | 3 | 26 | 9 | 45 | 13 |
| **3 `hybrid` 4B Q4_K_M, 512, role rules (recommended)** | **0** | **26** | **5** | **43** | **9** |
| 3 same, no OCR | 1 | 24 | 5 | 39 | 11 |
| 1 `vlm` 2B Q8_0, 512, raw | 3 | 65 | 45 | 89 | 43 |
| 3 `hybrid` 2B Q8_0, 512, role rules | 0 | 47 | 6 | 81 | 18 |
| 1 `vlm` 2B Q4_K_M, 1024, raw | 2 | 80 | 32 | 115 | 35 |
| 3 `hybrid` 2B Q4_K_M, 1024, role rules | 0 | 41 | 7 | 74 | 16 |
| 4 `claude` Sonnet, raw | 1 | 16 | 5 | 27 | 6 |
| 4 `claude` Sonnet, guards | 0 | 28 | 4 | 47 | 5 |
| 4 `claude` Sonnet, guards, pass 2 | 0 | 28 | 3 | 47 | 4 |
| 4 Claude inside the hybrid | 0 | 25 | 4 | 48 | 5 |

How to read this:
- With only 69 true pings in the 10 configs, the differences at the top are 0 to 3 stories. "0 missed" means "none of the 48 stories", not "never". The one-value users and the "ruling out" column separate the pipelines better.
- The small models fail in opposite directions. The 2B gives wide, often wrong sets (31 stories rule out a true value, but they rarely veto a ping). The 4B is confident (mean set size close to 1) and wrong more often in ways that cost pings (12 missed raw). Neither is safe alone.
- The guards alone barely help, because the models' main errors are in company, sponsorship and role, which the guards don't touch. The hybrid's rules do.

### Per dimension

Exact / covers / mean set size. N is the number of stories the label applies to: sponsorship 23 (30 job postings minus 7 unverifiable), company, role and level 43.

| Pipeline | Post type (48) | Sponsorship (23) | Company (43) | Role (43) | Level (43) |
|---|---|---|---|---|---|
| `ocr_rules` | 32 / 42 / 1.31 | 22 / 22 / 1.00 | 36 / 42 / 1.26 | 23 / 42 / 2.12 | 30 / 43 / 1.67 |
| `vlm` 2B raw | 38 / 43 / 1.10 | 2 / 14 / 2.04 | 11 / 35 / 1.77 | 22 / 28 / 1.16 | 29 / 32 / 1.07 |
| `hybrid` 2B, role rules | 34 / 43 / 1.27 | 22 / 22 / 1.00 | 41 / 42 / 1.02 | 33 / 35 / 1.09 | 39 / 40 / 1.16 |
| `vlm` 4B raw | 44 / 44 / 1.00 | 17 / 20 / 1.23 | 32 / 32 / 1.00 | 30 / 31 / 1.02 | 33 / 39 / 1.27 |
| **`hybrid` 4B, role rules** | 38 / 47 / 1.21 | 22 / 22 / 1.00 | 41 / 42 / 1.02 | 34 / 36 / 1.07 | 34 / 43 / 1.37 |
| `vlm` 2B Q8_0 raw | 41 / 41 / 1.00 | 2 / 6 / 1.30 | 23 / 27 / 1.09 | 27 / 29 / 1.07 | 30 / 30 / 1.00 |
| `hybrid` 2B Q8_0, role rules | 37 / 42 / 1.19 | 22 / 22 / 1.00 | 41 / 42 / 1.02 | 34 / 36 / 1.09 | 35 / 38 / 1.19 |
| `vlm` 2B Q4_K_M 1024 raw | 42 / 42 / 1.00 | 1 / 11 / 1.83 | 12 / 34 / 1.63 | 24 / 31 / 1.19 | 28 / 32 / 1.09 |
| `hybrid` 2B Q4_K_M 1024, role rules | 38 / 42 / 1.17 | 22 / 22 / 1.00 | 41 / 42 / 1.02 | 33 / 37 / 1.14 | 39 / 39 / 1.12 |
| `claude` Sonnet raw | 44 / 48 / 1.10 | 18 / 20 / 1.09 | 42 / 43 / 1.02 | 33 / 40 / 1.26 | 39 / 43 / 1.26 |
| `claude` inside the hybrid | 40 / 48 / 1.21 | 22 / 22 / 1.00 | 41 / 42 / 1.02 | 33 / 40 / 1.26 | 37 / 42 / 1.28 |

- **Sponsorship: rules, clearly.** The rules got 22 of 23 exactly right. The 2B alone ruled out the true value on 9 of 23, every time by answering sponsor-or-Canadian for a US posting (7 labelled unknown, 2 no sponsor). The one rule miss is a posting whose only hint is a short "US-based" note in the story text, which no rule reads as a refusal. That matches #9.
- **Company: rules.** The list match fixes every local-model company error but one. The remaining error is an event hosted on a big-tech meeting tool, where the tool's name in the screenshot matched the FAANG+ list.
- **Role: still the weakest dimension** for every local model (35–37 of 43 covered, even with title rules). Claude covers 40. The usual errors are AI-titled engineering roles given only one of `{ml, swe}`, and data or business roles called SWE.
- **Post type:** the 4B and Claude are good, the 2B less so. The 2B turns most misc posts into job postings, which costs only extra pings.
- **Claude doesn't need the rules,** except for sponsorship. It is mostly consistent between passes: 37 of 48 stories were identical. Of the 14 dimension differences, 5 were in dimensions that don't apply to the story, and 8 didn't change whether the label was covered. In 1, pass 1 left out a labelled role that pass 2 kept, which is why the passes differ by one story in the tables.

### Latency and memory (CT 120)

4 threads, `-c 4096 -np 1 --cache-ram 0 --load-mode none`, llama.cpp `b11514`, temperature 0, at most 320 output tokens. "Peak RSS" is the server's `VmHWM`. "Scope peak" is the cgroup's `memory.peak`, which also counts the model file's page cache (reclaimable) and the request runner.

| Configuration | Median | p95 | Worst | Prefill / generate (median) | Peak RSS | Scope peak | Cold load |
|---|---|---|---|---|---|---|---|
| 2B Q4_K_M, 512 image tokens | 20.5 s | 23.3 s | 27.2 s | 14.0 s / 6.4 s | 2.4 GiB | 3.8 GiB | 3.3 s |
| 2B Q8_0, 512 image tokens | 26.1 s | 29.9 s | 41.4 s | 18.3 s / 7.9 s | 3.0 GiB | 4.5 GiB (at the cap) | 3.5 s |
| 2B Q4_K_M, 1024 image tokens | 29.3 s | 39.6 s | 48.4 s | 21.9 s / 7.2 s | 2.6 GiB | 3.3 GiB | 2.5 s |
| **4B Q4_K_M, 512 image tokens** | **36.4 s** | **41.3 s** | **56.6 s** | 23.2 s / 13.8 s | **3.8 GiB** | 4.5 GiB (at the cap) | 6.3 s |
| 2B Q4_K_M as a text model (OCR text, no image) | 8.6 s | 11.2 s | 17.4 s | 2.5 s / 6.1 s | 1.7 GiB | 1.7 GiB | 1.5 s |
| RapidOCR (4 threads) | 1.0 s | — | 3.1 s | — | 0.75 GiB | — | < 0.1 s |
| Rules (company, sponsorship, level) | < 10 ms | | | | — | | |
| Claude Sonnet via `claude -p` (**workstation**, network-bound) | 5.6 s wall (4.2 s API) | | 7.4 s | | — | | |

- The system prompt (~1,400 tokens) is reused from the server's slot cache between requests; `--cache-ram 0` only turns off the extra host-memory cache. The uncached part is the image plus the story text, about 670 tokens.
- The 4B is slower here than in #8's 23 s median: prefill (the image plus ~670 uncached tokens of story and listing text) took 23 s and generation 14 s. I didn't isolate how much of the difference comes from the images and how much from the prompt. Generation is about 100 tokens, because the JSON comes out pretty-printed; a compact output format would cut that (re-score if it changes).
- The 4B's scope peak hit the 4.5 GiB cap. Its RSS was 3.8 GiB and the rest was page cache, which the kernel reclaimed. No request failed. Production should use `MemoryMax=5G`, as #8 recommended.
- **Effect on the watcher.** Over the ~1.75 hours of CT benchmarks, the watcher completed 14 cycles with 0 errors, and was at most 5 s late against its announced next check. It logged 2 warnings, both network errors posting to Discord. Both units were active in all 1,195 five-second samples. The lowest `MemAvailable` in the CT was 2.06 GiB, no swap was used, and the 1 GiB kill switch never fired.

### Cost

Local models cost nothing per story; the host is on anyway. Claude has no marginal cost on the Pro plan, but it draws on the user's Pro limits. The CLI reported an API-equivalent of about $0.014 per story, so 96 calls came to about $1.40. That isn't billed on Pro; on a paid API key it would be about $10 a month at 25 stories a day.

## Operational burden

| | Hybrid with a local VLM (recommended) | Claude via the Pro plan | OCR plus rules only |
|---|---|---|---|
| New moving parts | One systemd unit (`llama-server`), one model file (2.9 GB with its projector), a pinned binary; RapidOCR optional | Claude Code installed in the CT, a long-lived OAuth token | onnxruntime and RapidOCR models (~65 MB) |
| RAM | 3.8 GiB (4B) or 2.4 GiB (2B) resident, always | the CLI per call (not measured: it ran on the workstation) | 0.75 GiB during a call |
| Latency | 36 s median (4B), 57 s worst | ~6 s | ~1 s |
| Failure modes | OOM kill (contained by `MemoryMax`), slow CPU when the host is busy | Usage limit (shared with the user's own use), token expiry, outages, CLI updates | none beyond the code |
| Data leaves the host | No | Yes: every story image goes to Anthropic | No |
| Accuracy here | 0 missed, 26 extra | 0 missed, 25–28 extra | 1 missed, 121 extra |

### What running Claude on the server would take

Nothing below was set up.

- Install Claude Code in CT 120 for the service user. That reverses CLAUDE.md's "Claude Code isn't installed on the server" rule.
- Create a long-lived token on the workstation with `claude setup-token`, and store it as `CLAUDE_CODE_OAUTH_TOKEN` in the service's `.env`. It follows the webhook rule: never logged, never in an exception message.
- The tagger would run `claude -p` per story as in `research/tagger/claude_ref.py`: an empty temporary directory, only the Read tool, `--json-schema`, `--no-session-persistence`.
- On a usage-limit or authentication error it would fail open to unsure tags, and send one alert.
- The open questions remain: Pro limits are shared with the user's interactive use; the terms describe subscriptions as "ordinary, individual usage"; and every story leaves the host.
- It is the most accurate option measured and the cheapest to run, but I don't recommend it as the main path. A good use is as a later upgrade, or for an occasional offline re-check.

## Failure examples (paraphrased, invented style)

- **A careers page in a big-tech city, with nothing about visas:** the 2B called it `sponsor_or_canadian`, as if any listed location settled it. The rules say `unknown`.
- **An event hosted on a big-tech company's meeting tool:** the screenshot shows the tool's name, so the list match said FAANG+ while the hosting company was on neither list. The hybrid kept that error; Claude didn't make it.
- **An interview report about an internship, with no link:** the 2B called it a job posting and the 4B called it misc. Either way the process-info user lost the ping, until the no-link process-words guard kept `process_info`.
- **A dense job-board screenshot for an "AI" software internship:** the models picked either `ml` or `swe`; the label (and the doc) says both. Title rules catch this only when the listing title has the words.
- **A personal post (a photo or a meme):** the 2B called most of these job postings. That's harmless for pings (extra only) but would defeat a "no post" filter for misc (#13 drops only confident non-job posts, so it fails open).
- **A posting whose only US-only hint is a short "US-based" note in the story's own text:** labelled `no_sponsor`, read as `unknown` by the rules. That's a safe error under the default "don't ping me: no sponsor" setting.
- **A US internship requiring enrolment at a US school:** #9's rules returned `unknown`. Per the user's #9 decision it is `no_sponsor`, so that phrasing was added.

## Things tried

| Idea from the issue | Result |
|---|---|
| Structured output with evidence, under the uncertainty contract | Done (JSON schema with enums and `minItems: 1`, plus an evidence string). Every reply parsed. Small models rarely widen sets on their own: the 4B's mean set size was 1.0 for company and role. |
| Tuning when to say "unsure" | Done through rules and guards, not thresholds. Contract-driven widening (unread pages, student events, no-link process posts) removed the remaining missed pings at little cost. |
| Two models agree, or sample several times | Tested only as the union with `ocr_rules`: 0 missed, but 133 extra (3.3× the hybrid). Two VLM calls don't fit the time budget (#8). Not worth it. |
| Prompt variants, few-shot from the set | One prompt (`tag_v1`), invented examples only. I tuned the rules, not the prompt, to keep the prompt general. |
| 512 vs 1024 image tokens; Q4 vs Q8 | Neither helped. With the rules, the 2B ruled out a true value on 18 stories at Q8_0 and on 16 at 1024 image tokens, against 15 for Q4_K_M at 512. Both were slower (26 s and 29 s median vs 20.5 s). Differences of 1–3 stories are within run-to-run noise: the same 2B on the workstation and in the CT gave 34 vs 31 for the raw model. Stay at Q4_K_M and 512. |
| 2B vs 4B | The 4B with the rules beat the 2B with the rules: 9 vs 15 stories ruling out a true value, 26 vs 40 extra pings. It took 1.8× the time and 1.6× the memory. Both missed 0. |
| OCR hint to the VLM | Not run. With the rules, RapidOCR text changed the hybrid's scores by only one missed ping and two company sets. |

## Caveats

- **Small set.** 48 stories, 30 job postings, 23 scored for sponsorship; ML, PM, quant, sponsor-or-Canadian and senior roles each have fewer than 10. One story is about 2 % of the set.
- **One story is a video's first frame**, not a still (see [Data and method](#data-and-method)).
- **The rules were fitted on this set.** The extra refusal phrasings, the platform-host rule, the student-event and no-link process guards, and the role-title rules were all written after I looked at errors on these 48 stories. The prompt and the company lists weren't. The even/odd split in `score.py` isn't a held-out test for that reason. **Freeze this configuration and re-score it on the next ~30 labelled stories** (the labelling bot keeps collecting them) before #12 ships.
- One account, mostly US postings. The Canada rules are still tested on only 3 stories.
- One CPU benchmark run per configuration. The host's other workloads were near idle during the runs.

## Recommendation

**Pipeline 3: the hybrid, with Qwen3-VL-4B Q4_K_M at 512 image tokens.**

| Setting | Value |
|---|---|
| Model | Qwen3-VL-4B-Instruct **Q4_K_M** (2.5 GB) plus the **Q8_0** vision projector (0.45 GB), checked by SHA-256 in `install.sh` |
| Server | llama.cpp `llama-server`, release pinned (`b11514` measured), bound to `127.0.0.1` |
| Server flags | `-t 4 -tb 4 -c 4096 -np 1 --cache-ram 0 --load-mode none --image-max-tokens 512` |
| Request | `/v1/chat/completions`, `temperature 0`, `max_tokens 320`, `response_format` = the JSON schema in `research/tagger/prompting.py` (`SCHEMA`) |
| Prompt | `research/tagger/prompts/tag_v1.txt`, with the company lists filled in from `docs/tags.md` at start-up |
| Context text | Link host and path, mentions, and from the saved posting: title, company, locations, employment type, the first 600 characters, and the work-authorization sentences (max 700 characters) |
| Rules | Company: list match on listing company, link, model-read names and OCR text, with platform hosts ignored. Sponsorship: #9's `rules.sponsorship` plus the three extra refusal phrasings, Tesla as a known sponsor, all three values when the posting wasn't read and the image is silent. Level: title words, then #9's posting fallback for job postings. Role: title words when they settle it, else the model. Plus the guards listed under [Pipelines](#pipelines). These all port from `research/tagger/common.py` and `taggers.py` as pure functions with the offline tests in `test_common.py`. |
| Thresholds | None. The contract is set-valued, and doubt is expressed by widening sets. |
| RapidOCR | Optional. Without it the hybrid missed 1 ping here and covered 2 fewer company sets. Recommendation: include it, because it is also the reader for the fallback below and costs ~1 s. |
| Timeout | **90 s per story** for the model call (worst measured 57 s; #8's 60 s is too tight for the 4B with listing text) |
| Fallback | Model down, timed out or OOM-killed: `listing_rules` (link → job posting; company, sponsorship, level and role from the posting rules; with RapidOCR text if available). It missed 1 ping here, with 185 extra. If that raises too, `Tags.unsure()` (0 missed, every user pinged). The story is never delayed past the timeout. |

**If the user prefers lighter:** the same hybrid with **Qwen3-VL-2B Q4_K_M** also missed 0 pings here, at 20.5 s median and 2.4 GiB, with 40 extra pings and 15 stories ruling out a true value (against 26 and 9). It's a reasonable choice if CT 120's RAM is needed elsewhere. Switching is a model file and a version bump.

### Operational plan for #12

- **Process model.** A third systemd unit in CT 120, `story-watch-llm.service`, runs `llama-server` with the flags above as its own user, bound to `127.0.0.1`. The watcher calls it over HTTP, one story at a time, and posts each story as soon as it is tagged or the call times out (#8). The tagger lives in the watcher process: rules and the HTTP client in pure modules, plus RapidOCR in-process if included.
- **Limits.** `MemoryMax=5G`, `MemorySwapMax=0`, `CPUQuota=400%`, `CPUWeight=20`, `Nice=10`, `Restart=on-failure` with `RestartSec=60` and a start-limit burst, so it never crash-loops. The CT keeps 6 GB RAM (the 4B's 3.8 GiB plus ~1 GiB for RapidOCR, the watcher and the bot). 4 cores and `cpuunits 50` stay as decided in #8.
- **When the model is down.** The watcher checks `/health` before each cycle with new items. If it's down, or a call fails or times out, the story gets the fallback tags above: fail open, never blocking a notification. A "tagger unavailable" alert is sent once, with a "recovered" message when the model is back. The exception type alone is logged. The story's tags are stored at first classification (as today), so a story tagged by the fallback keeps those tags on retries.
- **Version and re-scoring.** The prompt file, model SHA-256 and rules version together form the tagger version. Changing any of them means re-scoring with `scripts/eval_tagger.py` on the labelled archive before deploying.

## Open questions for the user

1. **Model: 4B (recommended) or 2B?** The 4B gives fewer extra pings and fewer risky sets for 1.8× the time (36 s median) and 3.8 GiB. Recommendation: **4B**, because accuracy comes first and the latency is well inside "tens of seconds per story".
2. **Timeout: 90 s?** Recommendation: **yes**, since the worst measured 4B story took 57 s.
3. **Include RapidOCR in the normal path?** Recommendation: **yes**. It's cheap and it is the fallback's reader. Without it, the hybrid missed 1 ping here.
4. **Re-score before #12 ships** on the next ~30 labelled stories, with this configuration frozen. Recommendation: **yes**. Keep labelling until then (or use #35's fix-tags command once it exists).
5. **Claude:** keep it as an offline accuracy reference only, not in production. Recommendation: **yes**, for the reasons in [What running Claude on the server would take](#what-running-claude-on-the-server-would-take).
