# Reading the job posting behind a link sticker (#9)

Part of #2. Feeds #10. Prototypes: [`research/job_pages/`](../../research/job_pages/).

## Summary

- **Coverage.** For 23 of 28 archived links (82%), the full page text came back over plain HTTPS, with every `jobs.py` guard in place. 2 of the 28 links are event pages, not jobs: one came back with text (it is among the 23) and one didn't (a JavaScript-rendered page). That leaves 26 job links. 3 of them had closed, so 23 job postings were live, and the description came through for **22 of those 23 (96%)**. The one live job posting that failed hit a JavaScript bot challenge.
- **Worth building** (in this order): the Workday `cxs` JSON, Greenhouse and Ashby APIs, the JSON-LD `JobPosting.description` that `jobs.py` already parses, the Lever API, the iCIMS iframe URL and a closed-posting check. Next comes a plain-text fallback for the visible page text. No headless browser: it can't keep the guards cheaply, and only 1 live job posting in 23 needed one.
- **Deriving sponsorship, location and level.** On 22 hand-labelled postings, the evidence favours **text rules over a model for sponsorship**. For location and level it is neutral or untested:
  - The rules got sponsorship exactly right on all 22 and never ruled out a correct value.
  - Level was exact on 21; the 22nd came out wider than the label, which fails open. The model's best run also got 21, so n=22 doesn't separate the two.
  - Location: no posting in the sample was in or open to Canada, so the Canada rules have only been tested on invented phrases.
  - A 4B local model on CPU (6 threads, timed on a faster desktop) did worse on sponsorship. It ruled out the correct value on 4 to 6 of 22 postings even after one round of prompt fixes (depending on the prompt mode), and 14 of 22 before them. Only one model (Qwen3-4B) was tried. It needs about 4.5 GB of RAM, and a story takes 7 to 14 s median.
  - A union of rules and model never ruled out a correct value, but added extra sponsorship values on 5 to 8 of 22.
- **For #10:** in pipeline 3 (hybrid), take sponsorship from these rules over the extracted posting. Location and level rules are a cheap default, but the #6 eval set decides them; this sample can't (no Canadian postings, and level isn't separated at n=22). Only one 4B model was tried, so a larger or different model isn't ruled out. The numbers below are small and the rules were tuned on the same postings, so #10 must re-score everything on the #6 set.

## Target hardware (stated facts)

The model, if any, would run on the Proxmox host that runs the service, not on the development PC.

- **The service container today:** 1 core, 512 MB RAM, 256 MB swap, 4 GB disk, no GPU. It can be resized, or a separate container or VM could host a model.
- **Host:** a 6-core/12-thread desktop CPU with AVX-512 VNNI, and about 15 GB of RAM shared with other workloads. About 140 GB of disk is free.
- **GPU:** none usable. Inference is CPU-only.
- **Load:** a few stories a day, so tens of seconds per story is acceptable.

All model timings below come from a faster desktop used for relative timing, with llama.cpp limited to **6 threads** to approximate the host's 6 cores. Treat them as relative numbers. The host CPU has lower clocks and memory bandwidth, so expect it to be roughly 1.5 to 2 times slower; that is an estimate, not a measurement.

The rules and extractors themselves need nothing beyond the current container: no extra memory, and they run in milliseconds.

## Data and method

- **Links:** every link-sticker URL in the local story cache and the local story dumps, deduplicated: 28 URLs, of which `is_job_link()` accepts 27. The one it rejects is the JavaScript-rendered event page. All are from one watched account, captured over a few days before 2026-10-08. The #5 archive on the server wasn't reachable from this research (no SSH), so this is the whole sample.
- **Fetching:** each link was fetched once on 2026-10-08 with `research/job_pages/fetch.py`, which keeps the `jobs.py` guards (see [Guards](#guards)). That was 33 requests in total for the final run (28 pages plus API calls, a little more during development). The median time per link was 0.4 s and the slowest 4.5 s.
- **Labels:** I labelled sponsorship, level and "in or open to Canada" for the 22 job postings that came back with a description, reading each posting against [`docs/tags.md`](../tags.md). The 23rd page with text was an event (a coffee-chat sign-up), which has no sponsorship dimension, so it wasn't labelled. The labels stay in the gitignored cache. They are one reader's labels; on the first pass I missed a citizenship requirement that the rules then caught. So expect some label noise, and treat #6 as the real eval.
- **Overfitting:** the rules were written while looking at these postings, so their scores are optimistic. Two phrasings (an "only employ those authorized to work in the US" sentence and a "U.S. person required (ITAR)" sentence) were added after they were missed. The offline tests use invented phrases only.

## 1. Coverage

### Per ATS

"Full description" means the posting's body text, at least 400 characters, from a structured source; for the visible-text fallback, a page that looks like a posting.

| Link host | Links | Full description | How | Not obtained |
|---|---|---|---|---|
| Company career sites | 13 | 10 (one is an event page) | JSON-LD (7), JSON app state (1), visible text (1), embedded Greenhouse board (1) | 2 closed postings (404; redirect to the listing), 1 bot challenge |
| Workday | 4 | 4 | `wday/cxs` JSON | — |
| Ashby | 4 | 4 | posting API (3); JSON-LD on the page (1, not on the public board) | — |
| Greenhouse | 4 | 3 | job board API (2); page text (1, EU board) | 1 closed |
| Lever | 1 | 1 | postings API | — |
| iCIMS | 1 | 1 | JSON-LD in the `?in_iframe=1` document | — |
| Eightfold | 1 | 0 | — | an event page rendered by JavaScript, not a job |
| SmartRecruiters | 0 | — | extractor written, not exercised | — |
| **Total** | **28** (26 jobs, 2 events) | **23** (22 jobs, 1 event) | | 3 closed, 1 event without text, 1 blocked |

### Per field (the 23 pages with a description: 22 jobs, 1 event)

| Field | Present | Source |
|---|---|---|
| Title | 23 | every source |
| Location | 20 | ATS fields, JSON-LD `jobLocation`. Missing for the visible-text and EU-Greenhouse fallbacks and one app-state page. |
| Company | 16 | JSON-LD `hiringOrganization`, Greenhouse `company_name`, Workday. `jobs.py` already gets most of the rest from titles and `og:site_name`. |
| Any sponsorship or work-authorization sentence | 8 | description text |
| Level settled by the title alone | 19 | title |

In this sample, **every posting was in the US**. None was in or open to Canada, so the Canada rules have only been tested on invented phrases. The tag guide expects mostly `sponsor_or_canadian` or `unknown`. Here the labels were 1 `sponsor_or_canadian`, 6 `no_sponsor` and 15 `unknown`, which suggests that this account mostly posts US roles. #6 will show whether that holds.

### What each source needs

| Source | Request | JavaScript? | Notes |
|---|---|---|---|
| JSON-LD `JobPosting` | the page itself (already fetched by `jobs.py`) | no | Best general source. One site double-escapes the HTML in `description`, so convert to text twice when tags remain. Some descriptions are short teasers, so require a minimum length. |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>` | no | `content` is HTML-escaped HTML. The EU host has no API host that resolves; its page is server-rendered, so fall back to the page text. Company sites with `?gh_jid=` name the board in their embed script. |
| Lever | `api.lever.co/v0/postings/<company>/<id>` | no | Plain-text description plus lists; `categories.location`, `allLocations`, `workplaceType`. |
| Ashby | `api.ashbyhq.com/posting-api/job-board/<board>` (the whole board), then match the id | no | One board response per link. An unlisted posting is missing from the board, but its own page still has JSON-LD. |
| Workday | `<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/job/<path>` | no (the HTML does need it) | Clean JSON: title, description, locations, `timeType`. `timeType` says "Full time" for interns, so don't use it for level. |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/<c>/postings/<id>` | no | Written from the public API docs; no link in the sample. |
| iCIMS | the posting URL with `?in_iframe=1` | no | The outer page is an empty frame; the iframe document has JSON-LD. |
| JSON app state | the page, `<script type="application/json">` | no | One big-tech careers site answered **HTTP 400** unless the request carried the navigation headers every browser sends (`Sec-Fetch-Mode`, `Sec-Fetch-Dest`, `Sec-Fetch-Site`). With them, the posting object is in the page. Adding those headers doesn't weaken any guard. |
| Bot challenge | — | yes | One large company's site answered `202` with an AWS WAF challenge. Without running its JavaScript, there's no way through. |
| Login walls (e.g. LinkedIn) | — | — | None in the sample. Out of scope: never send credentials. Treat them like a failed fetch. |

**When the text can't be read** (closed, blocked, JavaScript-only, login), keep what `jobs.py` does today: title and company from metadata or the URL slug. Tag sponsorship as **unsure (all three values)**, not `unknown`, as `docs/tags.md` requires. Level still comes from the title when there is one. A failed fetch never blocks a notification.

**Closed postings.** 3 of the 28 links were already closed when fetched. Stories are fetched minutes after posting, so production should see fewer. They show up as a 404, an ATS API 404, or a redirect to a page without the job id. The prototype reports all three as `dead`, and the tags then follow the "can't read" rule above.

## Guards

Every request in the prototype goes through `research/job_pages/fetch.py`:

- **No Instagram cookies:** a fresh `requests.Session` per run, never the instaloader one.
- **HTTPS only:** checked on every hop.
- **Public addresses only:** `getaddrinfo` is checked on every hop, and redirects are followed by hand.
- **Size cap:** a streamed body capped at 2 MB, and at most 5 redirects.

ATS API calls are ordinary HTTPS GETs to public hosts, so the same guards cover them. The API URL is built from the link's own path, never from page content. The one exception is the Greenhouse board name, which comes from the company page's embed script. It still goes to a fixed Greenhouse host, through the same checks.

**A gap found in `jobs.py`.** `_check_public()` resolves the host, and then urllib3 resolves it again when it connects. A hostile DNS server could answer with a public address first and a private one second (DNS rebinding). The prototype adds a peer-address check, but it only **detects** a private connection, **best effort**. It runs after the request has already been sent, so it can't prevent the connection. It reads a private urllib3 attribute (`resp.raw._connection.sock`) and silently skips the check when that isn't exposed. The real fix, for #12, is to connect to the IP that was already checked: a transport adapter that dials that address, with SNI and the Host header set to the name. It needs a test with a mocked resolver.

### Headless browser: ruled out

A browser makes its own requests: subresources, XHR and fetch calls, websockets, prefetch, service workers. None of them go through `_check_public()`. Keeping the guards would mean all of this:

- forcing all browser traffic through a local filtering proxy that enforces HTTPS, public addresses per connection and size caps (and terminating TLS or relying on CONNECT host checks);
- a throwaway profile per fetch;
- disabling downloads, WebRTC and plugins;
- a CPU and memory budget: about 300 to 500 MB per Chromium instance, in a container that has 512 MB today.

That is a lot of attack surface and operational weight for 1 live job posting in 23, and the one failure here was a bot challenge, which a headless browser often fails anyway. Recommendation: **don't**. Revisit only if #6 shows that JavaScript-only pages are a large share of missed pings.

## 3. Rules versus a model

### Rules (`research/job_pages/rules.py`)

- **Location → "in or open to Canada":**
  - The structured location fields are checked for "Canada", provinces, `City, ON`-style abbreviations and major cities.
  - In the description, only an explicit statement counts: located, based or working in Canada, or a Canadian city with "office", "remote" or "hybrid", or remote across North America. A bare mention of Canada doesn't.
- **Sponsorship**, in this order:
  1. In or open to Canada → `{sponsor_or_canadian}`, even if the posting refuses sponsorship.
  2. A refusal → `{no_sponsor}`. That covers "unable / will not / does not … sponsor", "without … sponsorship", "no sponsorship", "must be a U.S. citizen", "citizenship required", clearance requirements, "U.S. person required" (ITAR or export control), and "only employ / must be authorized to work in the US".
  3. An offer ("will sponsor", "sponsorship is available") → `{sponsor_or_canadian}`. If an offer and a refusal both appear in different sentences, the answer is both values.
  4. Otherwise `{unknown}`.
  5. **The posting wasn't read** → all three values.

  Application-form questions ("Will you require sponsorship?") don't match, by design.
- **Level:**
  1. Title words first: intern, co-op, work term, student researcher → `internship`; new grad, university grad, early career, entry level → `new_grad`; senior, staff, lead, manager, "Engineer II" → `other`.
  2. Then the minimum number of years of experience in the text: 2 or fewer → `new_grad`, otherwise `other`.
  3. Then "currently enrolled" together with intern words.
  4. Otherwise `{new_grad, other}`, as `docs/tags.md` says for a full-time title with no level.

### The model

The model was Qwen3-4B-Instruct-2507 (Q4_K_M GGUF, 2.5 GB), run by llama.cpp's `llama-server` (CPU build) on `127.0.0.1` with 6 threads. Its output was constrained by a JSON schema to the tag values, plus a quoted evidence sentence. The prompt states the `docs/tags.md` definitions.

- **Full mode** sends the first 9000 characters of the posting, a median of 706 to 749 prompt tokens.
- **Snippets mode** sends only the sentences that bear on sponsorship, location or level, a median of 163 tokens.

Two prompt versions were run:

- **Prompt 1:** just the definitions.
- **Prompt 2:** also says that a US location alone isn't `no_sponsor`. On top of that, code widens a `no_sponsor` answer with `unknown` when the model's quoted evidence isn't in the posting.

### Results on 22 labelled postings

"Risky" means the answer ruled out the labelled value, which under the #15 matching rules can become a missed ping or a wrong veto. "Wider" means it kept extra values, which fails open (extra pings).

| System | Sponsorship exact / wider / risky | Level exact / wider / risky | Canada wrong | Time per posting (6 threads) |
|---|---|---|---|---|
| **Rules** | **22 / 0 / 0** | **21 / 1 / 0** | 0 | < 10 ms |
| Model, full, prompt 1 | 7 / 1 / 14 | 20 / 0 / 2 | 1 | median 15.8 s, max 36 s |
| Model, snippets, prompt 1 | 7 / 1 / 14 | 20 / 0 / 2 | 2 | median 8.5 s, max 30 s |
| Model, full, prompt 2 | 14 / 2 / 6 | 20 / 0 / 2 | 1 | median 13.6 s, max 30 s |
| Model, snippets, prompt 2 | 17 / 1 / 4 | 21 / 0 / 1 | 2 | median 6.8 s, max 16 s |
| Rules ∪ model, full, prompt 2 | 14 / 8 / 0 | 21 / 1 / 0 | 1 | as the model |
| Rules ∪ model, snippets, prompt 2 | 17 / 5 / 0 | 20 / 2 / 0 | 2 | as the model |

On the timing desktop with 6 threads, prompt processing ran at about 80 to 87 tokens/s and generation at about 13 to 14 tokens/s. The output is about 60 to 100 tokens, so generation dominates the time.

**Memory** (`llama-server` resident set, 4B Q4_K_M): **4.5 GB at a 2k context and 5.6 GB at an 8k context.** That includes the mmapped weights and the CPU repacked copy. It fits a 4 to 6 GB budget only at the top end, and only with short prompts.

**What went wrong with the model** (paraphrased):

- It treated "the job is in the US" as "won't sponsor a Canadian", despite the instructions: 12 of 22 under prompt 1, and still several under prompt 2.
- It read an internship's "must be enrolled at a school in the US" as a sponsorship refusal. That's debatable, but it isn't what the posting says.
- Under prompt 2 it missed an explicit "H1B sponsorship is available" and a "U.S. person required (ITAR)" line, both of which the rules catch.
- It called an "Engineer I" role an internship in one mode.

What the model is good for: it reads awkward phrasings that no regex anticipates. The union keeps that benefit without letting it veto anything, at the cost of extra values.

### Conclusion for question 3

**Sponsorship:** the evidence is clear. Work-authorization language on postings is formulaic: a few dozen phrasings cover the common ATS templates. The rules beat the one model tried (a 4B model on CPU), cost nothing, and are easy to test. **Location and level:** neutral or untested. Level is almost always in the title (19 of 23), and the rules and the model's best run tie at 21 of 22. No posting in the sample was in or open to Canada. Rules are a reasonable cheap default for both, but the #6 eval set decides. Only one model was tried, so this doesn't show that every model would do worse.

Keep a small fail-open safety net instead:

- a posting the rules can't read → all values;
- refusal and offer phrases both present → both values;
- log each sentence that mentions sponsorship or work authorization but matched no rule (`work_auth_sentences()`), so new phrasings can be found and added.

## What to build (for #12, if #10 agrees)

1. `jobs.py` returns a `JobPosting` (title, company, locations, description text, `read_ok`, source) instead of `JobInfo`. Keep the current title and company fallbacks.
2. **Extractors**, in order:
   - Workday `cxs`, Greenhouse API, Ashby board API, Lever API: three short functions each, plus JSON fixtures in tests.
   - JSON-LD description, which reuses the existing `_ld_postings`.
   - The iCIMS `?in_iframe=1` variant.
   - Closed-posting detection.
   - Visible text as the last resort.

   Defer SmartRecruiters and app-state JSON until links show up. Add the `Sec-Fetch-*` navigation headers to the session.
3. **Guards:** keep all of them. Add the peer-address check, or connect to the pinned IP, against DNS rebinding. Cap API responses with the same 2 MB limit; an Ashby board can be large, so measure it.
4. **Rules module:** pure functions with no network, returning sets per `docs/tags.md`. The prototype's invented-phrase tests port directly.
5. **Requests per story:** at most 2 (page plus API), only for items with a job link, and only in a cycle that has new items. Same as today's single page fetch.

## Open questions

1. **RAM budget for a model service on the host.** Proposal: **0 GB for job pages.** The rules need none, and the 4B model measured here needs 4.5 to 5.6 GB. If #7 or #8 picks a local vision model anyway, budget that model, and leave the posting fields to the rules regardless.
2. **Is the sample representative?** 28 links from one account, all US roles. When #6 has labels, re-run `research/job_pages/run.py` and `evaluate.py` on archive links from more accounts, especially Canadian postings, which this sample didn't contain.
3. **A "must be enrolled at a school in the US" clause on internships:** should it count as `no_sponsor` for a Canadian student? The labels here say `unknown`, because it isn't a sponsorship statement. It needs a user decision in `docs/tags.md`.
4. **A "public trust" requirement** (not a security clearance): `unknown` or `no_sponsor`? In this sample it came with an explicit citizenship requirement, so it didn't matter.
