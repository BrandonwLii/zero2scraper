"""Scoring harness for taggers (#6): run a tagger over the labeled archive and report how it does.

Library layout (the CLI is ``scripts/eval_tagger.py``, wired in ``cli.py``):

- ``data``     labels + archive sidecars -> ``EvalStory`` (gold ``Tags`` and a ``StoryItem``)
- ``taggers``  the ``Tagger`` protocol, the adapter for ``classify.py``, a baseline, plug-in loading
- ``cache``    tagger outputs cached per (tagger, version, media id), outside the repo
- ``runner``   run a tagger over stories with the cache, timing and failure fallback
- ``metrics``  missed/extra pings, per-dimension accuracy, confusion matrices, "no post" drops, label counts
- ``report``   Markdown / JSON rendering

Nothing here touches the network. Stories, labels and tagger outputs live outside the repo.
"""
