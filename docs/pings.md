# Ping matching rules

Status: **draft, waiting for user sign-off** (#15). Items marked **Sign-off** are recommendations, not decisions.

Given a story's tags ([tags.md](tags.md), `story_watch/tags.py`) and one user's lists, decide whether to ping that user. The code is `should_ping(prefs, tags)` in `story_watch/pings.py`: pure, no I/O. Storage and Discord wiring are #16 and #17.

**Governing principle: fail open.** A missed job posting the user wanted is the worst outcome; an extra ping is the acceptable one. Every rule below that involves doubt resolves toward pinging.

## Preferences

A user has two lists, each a set of values per dimension (`post_type`, `sponsorship`, `company`, `role`, `level`):

- **Ping me**: what they want.
- **Don't ping me**: what they never want.

In config the values are written `<dimension>:<value>`, such as `role:swe` (`PingPrefs.from_keys`).

## Rules

Each dimension of a story has a set of *possible values* ([the contract](tags.md#the-output-contract)): one value is confident, several is unsure or a multi-job story, and `None` is not applicable.

1. **Opt-in.** If the "ping me" list is empty, don't ping. A "don't ping me" list alone never pings.
   **Default post type** (decided by the user, 2026-10-08): if the "ping me" list has values but no post type, it defaults to `post_type:job_posting`. Someone who wants events, process info or Misc posts must list those post types. The default is written into the preferences when they are built, so it appears in `to_keys()` and storage, UI and matching agree. An empty "ping me" list gets no default, and "don't ping me" never gets one.
2. **OR within a dimension, AND across dimensions.** For every dimension the user listed under "ping me", the story must match it. A dimension the user didn't list matches anything.
3. **An unsure dimension matches if any possible value is listed.** `role = {swe, pm}` matches "ping me: pm". Several values from a multi-job story behave the same way (the contract treats both alike).
4. **"Don't ping me" is a veto, but only for confident tags.** A dimension vetoes only when *every* possible value is on "don't ping me". With one value that is a plain veto; with several, one value off the list is enough to keep the ping. Unsure dimensions therefore can't veto unless the user lists all the values they could be.
5. **A veto beats a match.**
6. **Unknown is a real tag.** `sponsorship = {unknown}` means the posting doesn't say; it matches "ping me: unknown" only, and "don't ping me: unknown" vetoes it. It is not the tagger being unsure (`{sponsor_or_canadian, no_sponsor, unknown}`), which matches any of the three and vetoes only if the user listed all three.
7. **Not applicable (`None`) matches and never vetoes**, in both lists. Event and Process info have no sponsorship, so a user who lists `post_type:event` and `sponsorship:sponsor_or_canadian` is still pinged for events, and no sponsorship veto applies.
8. **Only the value sets are read.** `confidence` and `evidence` are ignored.

Algorithm: if "ping me" is empty, false. For each dimension in turn: skip it if not applicable; false if the user listed it under "ping me" and the possible values share nothing with that list; false if "don't ping me" lists it and every possible value is on that list. Otherwise true.

## Worked examples

Users (the `tags` column lists only what matters; unmentioned dimensions are unsure, i.e. every value, and for non-job posts the dimensions that don't apply are N/A):

| User | Ping me | Don't ping me |
|---|---|---|
| A | post_type job_posting; role swe, ml; level internship | |
| B | post_type job_posting | sponsorship no_sponsor; company other |
| B2 | post_type event | sponsorship no_sponsor |
| C | post_type job_posting | sponsorship no_sponsor, unknown |
| D | sponsorship sponsor_or_canadian (post type defaults to job_posting) | |
| D2 | post_type process_info; sponsorship sponsor_or_canadian | |
| E | level internship (post type defaults to job_posting) | |
| E2 | post_type event; level internship | |
| F | role swe | company quant |
| G | | company quant |
| H | post_type job_posting | company faang_plus, quant, other |

| # | User | Story | Ping? | Why |
|---|---|---|---|---|
| 1 | A | job; role swe; level internship | yes | all dimensions match |
| 2 | A | job; role ml; level internship | yes | OR within role |
| 3 | A | job; role pm; level internship | no | role misses |
| 4 | A | job; role swe; level new_grad | no | level misses (AND) |
| 5 | A | job; role {swe, pm}; level internship | yes | several roles, one listed |
| 6 | A | job; role unsure; level internship | yes | unsure matches |
| 7 | A | job; role swe; level {internship, new_grad} | yes | unsure level, one listed |
| 8 | A | event; role swe; level internship | no | post type is confidently event |
| 9 | A | {job, misc}; role swe; level internship | yes | post type might be a job |
| 10 | A | misc (everything else N/A) | no | post type misses |
| 11 | B | job; sponsorship no_sponsor; company quant | no | confident veto |
| 12 | B | job; sponsorship {no_sponsor, unknown}; company quant | yes | unsure can't veto |
| 13 | B | job; sponsorship unknown; company quant | yes | Unknown is a fact, not on the list |
| 14 | B | job; sponsorship unsure; company quant | yes | no idea can't veto |
| 15 | B | job; sponsorship unknown; company other | no | company veto |
| 16 | B | job; sponsorship unknown; company {faang_plus, other} | yes | one value off the list |
| 17 | C | job; sponsorship {no_sponsor, unknown} | no | every possible value is vetoed |
| 18 | C | job; sponsorship {unknown, sponsor_or_canadian} | yes | one value off the list |
| 19 | B2 | event; company quant | yes | sponsorship is N/A, so no veto |
| 20 | D | job; sponsorship sponsor_or_canadian | yes | match |
| 21 | D | job; sponsorship unknown | no | Unknown isn't "sponsor or Canadian" |
| 22 | D | job; sponsorship no_sponsor | no | miss |
| 23 | D | job; sponsorship {unknown, sponsor_or_canadian} | yes | unsure matches |
| 24 | D2 | process_info | yes | sponsorship N/A matches |
| 25 | E | misc | no | level is N/A, but the defaulted post type (job posting) misses |
| 26 | empty | job; role swe | no | opt-in |
| 27 | G | job; role swe; company other | no | no "ping me" list |
| 28 | F | job; role swe; company quant | no | veto beats match |
| 29 | F | job; role swe; company {quant, other} | yes | company unsure, no veto |
| 30 | A | nothing known (`Tags.unsure()`, the classifier fallback) | yes | post type might be a job, everything else unsure |
| 31 | H | nothing known | no | the user vetoed every company value (see open question 3) |
| 32 | H | job; company {quant, other} | no | every possible value is vetoed |
| 33 | D | process_info | no | no post type listed, so job postings only |
| 34 | E | job; level internship | yes | the default admits job postings |
| 35 | E | event; level internship | no | events must be listed explicitly |
| 36 | E2 | event; level internship | yes | explicit `post_type:event` pings for events |
| 37 | E2 | job; level internship | no | an explicit post type replaces the default |

Each row is a parametrized case in `tests/test_pings.py` with the same number.

## Open questions

Question 1 is decided. The rest are **Sign-off** (no answer yet); the code follows the recommendation.

1. **Decided by the user, 2026-10-08: post type defaults to job postings.** Not applicable still always matches, so without a default a user listing only `level:internship` would be pinged for every Misc post. A non-empty "ping me" list with no post type now means `{job_posting}`; other post types must be listed explicitly (rule 1).
2. **Unknown doesn't match "sponsor or Canadian".** A user who wants the maybe-sponsored jobs lists `sponsorship:unknown` as well. *Recommendation:* keep this; it matches the definition in tags.md and keeps Unknown a real tag. #14's UI could offer "Sponsor or Canadian + Unknown" as one choice.
3. **Listing every value of a dimension under "don't ping me"** vetoes even a no-idea story, including the classifier-failure fallback, and means "never ping me for anything". *Recommendation:* allow it in the matcher (consistent, and it is the only reading of the all-values rule), and let #16 reject or warn on a list that covers a whole dimension.
4. **Multi-job stories give extra pings.** "SWE interns and PM new grads" matches a user who wants PM interns, because values are tagged per dimension (tags.md). *Recommendation:* accept it (fail open) and revisit once the #6 eval set shows how often it happens.
5. **Opt-in with a "don't ping me"-only user** is never pinged. *Recommendation:* keep; a veto-only user is unlikely to be meant as "everything except", and that can be added later as an explicit mode.
