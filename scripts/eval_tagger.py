#!/usr/bin/env python3
"""Score a tagger against the labeled archive (#6).

    .venv/bin/python scripts/eval_tagger.py                          # rules classifier, ~/story-watch-data/archive
    .venv/bin/python scripts/eval_tagger.py /path/to/archive --tagger unsure
    .venv/bin/python scripts/eval_tagger.py --tagger my_prototype:make_tagger --json --out report.json

Reads <archive>/labels.jsonl (written by the labeling bot) and the archive's JSON sidecars; no
network and no Instagram or Discord requests. Tagger outputs are cached per (tagger, version, media
id) in ~/story-watch-data/eval-cache, so re-scoring is cheap; --refresh re-runs the tagger.
See story_watch/evaluation/ for the pieces and eval/ping_configs.toml for the user configs.
"""

import sys

from story_watch.evaluation.cli import main

if __name__ == "__main__":
    sys.exit(main())
