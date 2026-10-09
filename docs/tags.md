# Story tags

Status: **fully signed off** (#4). The user decided the Sponsorship reading, the Level values and the company lists on 2026-10-08, and then the uncertainty contract and the not-applicable representation the same day.

Every story gets tags in five dimensions. The tags decide whether a story is posted (#13) and who gets pinged (#3). The worst outcome is a user missing a job posting they wanted, so every rule below leans toward keeping more values when in doubt ("fail open").

The code is `story_watch/tags.py`. Each value has a stable lowercase string, used in env vars, the database and user config. When dimensions are mixed (for example in a user's ping list), write `<dimension>:<value>`, such as `role:swe`, because `other` exists in three dimensions.

| Dimension (`name`) | Values (`string`) |
|---|---|
| Post type (`post_type`) | Event (`event`) · Job posting (`job_posting`) · Process info (`process_info`) · Misc (`misc`) |
| Sponsorship (`sponsorship`) | Sponsor or Canadian (`sponsor_or_canadian`) · No sponsor (`no_sponsor`) · Unknown (`unknown`) |
| Company (`company`) | FAANG+ (`faang_plus`) · Quant (`quant`) · Other (`other`) |
| Role (`role`) | ML (`ml`) · SWE (`swe`) · PM (`pm`) · Other (`other`) |
| Level (`level`) | Internship (`internship`) · New grad (`new_grad`) · Other (`other`) |

All examples below are invented and paraphrased. None is taken from a real story.

## The output contract

**Decided by the user, 2026-10-08.**

A classifier returns, **for each dimension, the set of values it can't rule out**:

| What the classifier knows | What it returns | Example |
|---|---|---|
| Confident | one value | `level = {internship}` |
| Unsure between some values | those values | `level = {new_grad, other}` |
| The story really has several values | those values | `role = {swe, pm}` for "hiring SWE and PM interns" |
| No idea | every value | `role = {ml, swe, pm, other}` |
| The dimension doesn't apply to this post type | `None` (not applicable) | `sponsorship = None` on a Misc post |

Plus two optional, informational fields: `confidence` (a number from 0 to 1, overall) and `evidence` (a short string for logs, such as "title says co-op"). Matching never reads them; a classifier expresses doubt by **widening the set**, not by lowering the confidence.

Rules:

- **An empty set is invalid.** `Tags` rejects it. This is deliberate: code like `any(v in ping_list for v in values)` is False on an empty set, so an empty set would silently fail closed.
- **"Not applicable" is `None`, and only the post type decides it** (table below; **decided by the user, 2026-10-08**). The classifier doesn't choose it. If the post type is itself unsure, a dimension applies when it applies to *any* of the possible post types. For example, `post_type = {job_posting, misc}` keeps all five dimensions.
- **A dimension the classifier leaves out becomes "every value"** if it applies. So a classifier that only knows the post type (today's `rules` classifier) produces a valid, fail-open result. `Tags.unsure()` (everything unsure) is the fallback when a classifier raises.
- If a classifier gives values for a dimension that doesn't apply, they're dropped.
- **For the matching rules (#15):** an unsure dimension matches "ping me" if any of its values is on the list, and vetoes through "don't ping me" only if all its values are on the list. A `None` dimension always matches and never vetoes. Several values (unsure, or a story with several jobs) are treated the same way, which is what both cases need.
- Helpers: `values(dim)`, `is_applicable(dim)`, `is_certain(dim)` (applies and has one value), `certain_value(dim)`, `is_unsure(dim)` (applies and has every value), `to_dict()` / `from_dict()` for storage.
- "Drop only when confident" rules (#13) should use `is_certain`. A `None` dimension is never certain.

### Alternatives rejected

- **One value plus a confidence score, with a threshold in matching.** Scores from different models (and from one model across prompts) aren't calibrated, so the threshold would need retuning for every model. When the single value is wrong, there's nothing to fail open to. And it can't say "SWE or ML, but not PM".
- **One value plus an "unsure" value per dimension.** It can't express partial knowledge ("internship or new grad, not experienced"). It also adds a value that users could put on their lists, and it gets confused with Sponsorship's real "Unknown".
- **A probability for every value.** Same calibration problem. A set is what you get after thresholding probabilities anyway, and the classifier can do that itself.
- **Empty set for "not applicable".** Fails closed with `any()`, as above.
- **Every value for "not applicable".** It would let a user veto a post through a dimension that doesn't exist for it (by listing every value as "don't ping me"), and the embed couldn't tell "no idea" from "doesn't apply".
- **A list of per-job tag tuples for multi-job stories.** More precise ("SWE intern" and "PM new grad" stay separate), but it complicates the classifier, the storage and the matching. Rejected for now; see [Several jobs in one story](#several-jobs-in-one-story).
- **A per-dimension confidence and evidence.** Useful for debugging, but nothing would read it. It can be added later without breaking the contract.

## Which dimensions apply

| Post type | Sponsorship | Company | Role | Level |
|---|---|---|---|---|
| Job posting | yes | yes | yes | yes |
| Event | — | yes | yes | yes |
| Process info | — | yes | yes | yes |
| Misc | — | — | — | — |

"—" means not applicable (`None`). Post type always applies.

- **Event and Process info** keep Company, Role and Level, because they're often specific: "a quant firm's info session for interns", "how the PM new-grad interviews work at a big tech company". When the post doesn't narrow a dimension down, that dimension gets every value it doesn't rule out. For example, a quant firm's general interview-process post is `company = {quant}` and Role and Level unsure. Users who want quant content get pinged, which is the intent.
- **Sponsorship doesn't apply** to Event and Process info. Work authorization is a property of a job.
- **Misc** has nothing but the post type. Personal posts and memes have no company, role or level.

## Post type

| Value | Definition |
|---|---|
| `event` | Something to attend at a set time: info session, hackathon, career fair, webinar, coffee chat, conference, workshop. |
| `job_posting` | A role or program someone can apply to now, or one with a stated opening date. |
| `process_info` | How hiring works: interview stages and format, online assessments, timelines, offer or application season news, interview tips. |
| `misc` | Anything else: personal posts, memes, polls, promotions, general content not about hiring. |

Examples:

- `event`: "A big tech company is hosting a virtual info session for students next Thursday, sign up here." Not `event`: "Applications for our summer internship program are open" (that's `job_posting`, even though the program has a start date).
- `job_posting`: a story showing an intern role title with a link sticker to a job board; a roundup listing several companies' new-grad roles; "this new-grad program opens applications on Monday". Not `job_posting`: "I just got my offer from X!" (`misc`, or `process_info` if it explains the process).
- `process_info`: "The OA for this trading firm is three math questions in 30 minutes"; "most fall internship postings come out in August"; "here's what the final round looked like". Not `process_info`: a general productivity tip unrelated to hiring (`misc`).
- `misc`: a gym selfie, a meme about leetcode, a promo for a course, a poll asking followers which city they live in.

Edge cases:

- **A story with a job link** (a link sticker to a job board or careers page) always keeps `job_posting` in its post-type set, unless the linked page was read and is clearly something else (for example an event registration page on a careers site, which is `event`). This backs the hard rule in #13 that a story that could be a job posting is never dropped.
- **Hiring events** ("apply to attend our hiring day") are `event`. If the story also links a specific role, use `{event, job_posting}`.
- **Fellowships, scholarships with a work term, and student programs** are `job_posting` if they include paid work; otherwise `event` or `misc`.
- **Meme or joke about a real posting** with the posting linked: `job_posting` (the link is the useful part).
- When torn between two post types, return both.

## Sponsorship

**Decided by the user, 2026-10-08.** The spec line was "Sponsor or Canadian / No sponsor / Unknown". It's read as three values, from the point of view of a **Canadian student**: can they take this job? In the user's words, in effect: we're Canadian, so we care about jobs in Canada or jobs that sponsor visas.

| Value | Definition |
|---|---|
| `sponsor_or_canadian` | A Canadian student can take the job: it's located in Canada, open to people in Canada (e.g. remote in Canada or North America), or the company says it sponsors work visas. |
| `no_sponsor` | The posting says it won't sponsor, or it requires citizenship, permanent residence, a security clearance or existing work authorization in another country, **and** it isn't located in or open to Canada. |
| `unknown` | The posting says nothing about sponsorship or work authorization, and the location doesn't settle it (it isn't in Canada). |

Examples:

- `sponsor_or_canadian`: a role in Toronto; a remote role open across North America; a US role that says "we sponsor visas for this position".
- `no_sponsor`: a US role saying "must be authorized to work in the US without sponsorship now or in the future"; a role requiring US citizenship for a clearance.
- `unknown`: a US role whose page never mentions sponsorship or work authorization.

**Expected distribution.** Most postings should be `sponsor_or_canadian` (explicitly in Canada) or `unknown` (the listing doesn't say). `no_sponsor` should be mostly for listings that explicitly say they won't sponsor.

Edge cases:

- **Located in Canada but "no sponsorship"**: `sponsor_or_canadian`. A Canadian doesn't need sponsorship to work in Canada.
- **Several locations, one of them in Canada**: `sponsor_or_canadian`.
- **Several jobs with different answers**: the union, e.g. `{sponsor_or_canadian, no_sponsor}`.
- **Don't infer from the company.** "This company usually sponsors" isn't in the posting, so it's `unknown`. Don't infer US treaty-visa eligibility either.
- **"Unknown" is a fact, not doubt.** `{unknown}` means the tagger read the posting and it doesn't say. If the tagger couldn't read the posting (the page failed to load, the image text was unreadable), it doesn't know whether the posting mentions sponsorship, so it returns every value it can't rule out, usually all three. With fail-open matching, a user who said "don't ping me: no_sponsor" is still pinged in both cases, but the embed and the eval set can tell them apart.
- **Only job postings have it.** It's not applicable to the other post types.

## Company

| Value | Definition |
|---|---|
| `faang_plus` | The hiring (or hosting) company is on the FAANG+ list below, including its listed aliases and subsidiaries. |
| `quant` | The company is on the Quant list below. |
| `other` | A named company on neither list, or a post that isn't about any specific company (general advice, a roundup of other companies). |

Examples:

- `faang_plus`: an intern posting at a big social-media company referred to by its old name; a cloud subsidiary's new-grad role.
- `quant`: a proprietary trading firm's quant-developer internship.
- `other`: a mid-size startup's SWE intern role; a bank's trading-desk analyst role; "five tips for any technical interview".

Edge cases:

- **Banks and asset managers** not on the Quant list are `other`, even for quant-titled roles. The dimension is about the company, not the role.
- **Subsidiaries** count as the parent only when listed as an alias below.
- **Several companies in one story**: the union.
- **A company that exists but can't be identified** (unreadable logo, no name in text or link): every value it can't rule out. That's different from a post with no company, which is `other`.
- **Recruiting agencies** posting for an unnamed client: unsure, unless the client is clearly not on either list.

### FAANG+ list

**Approved by the user, 2026-10-08**, with all candidates added. Matching is case-insensitive on the company name or alias as it appears in the story or posting.

| Company | Aliases and subsidiaries |
|---|---|
| Meta | Facebook, Instagram, WhatsApp, Reality Labs |
| Apple | |
| Amazon | AWS, Amazon Web Services |
| Netflix | |
| Google | Alphabet, Google DeepMind, DeepMind, YouTube |
| Microsoft | LinkedIn, GitHub |
| NVIDIA | |
| OpenAI | |
| Anthropic | |
| Stripe | |
| Databricks | |
| Uber | |
| Airbnb | |
| Tesla | |
| Waymo | (an Alphabet company) |
| Shopify | |
| Salesforce | |
| Adobe | |
| Snowflake | |
| Palantir | |
| Bloomberg | |

### Quant list

**Approved by the user, 2026-10-08**, with all candidates added.

| Company | Aliases |
|---|---|
| Jane Street | |
| Citadel | Citadel Securities |
| Two Sigma | |
| Hudson River Trading | HRT |
| Jump Trading | Jump |
| D. E. Shaw | DE Shaw, D.E. Shaw |
| Optiver | |
| IMC Trading | IMC |
| Susquehanna | SIG, Susquehanna International Group |
| Five Rings | |
| Tower Research Capital | Tower Research |
| DRW | |
| Akuna Capital | Akuna |
| Virtu Financial | Virtu |
| XTX Markets | XTX |
| Renaissance Technologies | RenTech |
| Millennium | Millennium Management |
| Point72 | Cubist |
| Squarepoint Capital | Squarepoint |
| Old Mission Capital | Old Mission |
| Radix Trading | Radix |
| Flow Traders | |
| Headlands Technologies | Headlands |
| G-Research | |
| Bridgewater | Bridgewater Associates |
| AQR | AQR Capital Management |
| Balyasny | Balyasny Asset Management, BAM |
| Man Group | Man AHL, AHL |
| PDT Partners | PDT |
| Voleon | The Voleon Group |
| Qube Research & Technologies | QRT |
| Maven Securities | Maven |
| Belvedere Trading | Belvedere |
| Chicago Trading Company | CTC |

## Role

| Value | Definition |
|---|---|
| `ml` | Building or researching machine-learning or AI systems: ML engineer, applied or research scientist in ML/AI, data scientist. |
| `swe` | Building software: backend, frontend, full-stack, mobile, infrastructure, SRE/DevOps, embedded software, data engineering, security engineering, quant developer. |
| `pm` | Product management (PM, APM, product owner) and technical program management. |
| `other` | Everything else: quant trader or researcher, hardware, IT support, data or business analyst, design, consulting, sales, finance. |

Examples:

- `ml`: "Machine Learning Engineer Intern"; "Research Scientist, AI, new grad".
- `swe`: "Software Engineer Intern, Backend"; "Quant Developer Co-op".
- `pm`: "Associate Product Manager, new grad".
- `other`: "Quantitative Trader Intern"; "Hardware Engineering Intern"; "Data Analyst Co-op".

Edge cases:

- **A software role on an ML team** ("Software Engineer, Machine Learning"; "ML infrastructure engineer"): `{ml, swe}`. Both groups of users want it.
- **Several jobs**: the union.
- **Generic "tech internships" with no role named**: every value the post doesn't rule out (often `{ml, swe, pm}`, or all four).
- **Process info or an event for one track** ("SWE intern interview tips"): that role. Otherwise unsure.

## Level

**Decided by the user, 2026-10-08: keep `other`**, as epic #2 has it, for senior, experienced and manager roles. The user notes these aren't expected in the data sources; the value exists for completeness. (Epic #2 lists Level as "Internship · New grad · Other", while #4 and #3 list only Internship and New grad, so those two tables need "Other" added.) Without it, an experienced posting has nowhere correct to go. Forcing it into `internship` or `new_grad` is wrong, and calling the dimension "not applicable" would make it match every user's ping list (not applicable always matches), so intern-seekers would be pinged for every senior role.

| Value | Definition |
|---|---|
| `internship` | A fixed-term student role: internship, co-op, work term, placement, including PhD and research internships. |
| `new_grad` | A full-time role for recent or upcoming graduates: "new grad", "university grad", "entry level", "early career", or roughly 0–2 years of experience required. |
| `other` | Every other level: experienced (needs more than about 2 years), senior, staff, manager, part-time non-student roles. |

Examples:

- `internship`: "Summer 2027 SWE Intern"; "8-month co-op, software".
- `new_grad`: "Software Engineer, University Graduate 2027"; "Entry-level data scientist, 0–1 years".
- `other`: "Senior Software Engineer, 5+ years"; "Engineering Manager".

Edge cases:

- **Experienced full-time posting**: `{other}`.
- **No level stated**: the set of values the posting doesn't rule out. Internships are nearly always labelled as such, so a full-time title with no level and no experience requirement is usually `{new_grad, other}`. If the tagger couldn't see the title or posting at all, all three.
- **"Software Engineer I" or "Level 1"** with no years: `{new_grad, other}`; with "0–2 years" or "recent graduate": `{new_grad}`.
- **Several jobs**: the union, e.g. "hiring interns and new grads" is `{internship, new_grad}`.
- **Event or process info for students in general** (e.g. an info session for "students and recent grads"): `{internship, new_grad}`.

## Several jobs in one story

**Decided by the user, 2026-10-08** (part of the uncertainty contract).

Tag the story with the **union** of the jobs' values in each dimension. "Hiring SWE and PM interns" is `role = {swe, pm}`, `level = {internship}`.

With the matching rules in #15 this pings everyone who wants any of the jobs, and only vetoes when every job is on a user's "don't ping me" list. The cost is cross-combinations: "SWE interns and PM new grads" is `{swe, pm} × {internship, new_grad}`, so a user who wants PM internships is pinged too. That's an extra ping, which is the acceptable failure. If the eval set (#6) shows this happens often, the contract can grow a per-job list later.

## Answers to the open questions in #4

| # | Question | Answer | Status |
|---|---|---|---|
| 1 | Sponsorship reading | Three values, from a Canadian student's view: `sponsor_or_canadian` (in Canada, open to Canadians, or sponsors), `no_sponsor`, `unknown` (the posting doesn't say). Expected: mostly `sponsor_or_canadian` or `unknown`, with `no_sponsor` mostly for explicit refusals. "Unknown" is a fact about the posting; unsure is a set with several values (usually all three). | **Decided by the user, 2026-10-08** |
| 2 | Level values | Keep `other` (as epic #2 has it) for senior, experienced and manager roles, which aren't expected in the data sources. No level stated: the values the posting doesn't rule out, usually `{new_grad, other}`. | **Decided by the user, 2026-10-08** |
| 3 | Company lists | The lists above, with every candidate added to both and with aliases. | **Decided by the user, 2026-10-08** |
| 4 | Uncertainty contract | A non-empty set of values per dimension, plus optional confidence and evidence. Several jobs in one story: union per dimension. | **Decided by the user, 2026-10-08** |
| 5 | Not-applicable representation | `None`, decided by the post type alone (Event and Process info: no Sponsorship; Misc: only the post type); always matches in #15. | **Decided by the user, 2026-10-08** |
