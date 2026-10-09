"""Run a tagger over stories: cache lookup, timing, and the fail-open fallback."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..tags import Tags
from .cache import CacheEntry, TagCache
from .data import EvalStory
from .taggers import Tagger, TaggerOutput

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Prediction:
    media_id: str
    tags: Tags
    latency_s: float  # from the run that filled the cache, when cached
    cost_usd: Optional[float] = None
    cached: bool = False
    error: Optional[str] = None  # exception type name only; messages may carry story content


def run_tagger(
    tagger: Tagger,
    stories: Iterable[EvalStory],
    cache: Optional[TagCache] = None,
    refresh: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> list[Prediction]:
    """One Prediction per story, in order. ``refresh`` ignores cached entries (and overwrites them)."""
    out: list[Prediction] = []
    for story in stories:
        if cache is not None and not refresh:
            hit = cache.get(tagger.name, tagger.version, story.media_id)
            if hit is not None:
                out.append(Prediction(story.media_id, hit.tags, hit.latency_s, hit.cost_usd, cached=True))
                continue
        start = clock()
        try:
            result = tagger.tag(story)
        except Exception as e:
            latency = clock() - start
            log.warning("tagger %s failed on a story (%s); scoring it as unsure", tagger.name, type(e).__name__)
            out.append(Prediction(story.media_id, Tags.unsure(), latency, error=type(e).__name__))
            continue
        latency = clock() - start
        output = result if isinstance(result, TaggerOutput) else TaggerOutput(result)
        if not isinstance(output.tags, Tags):
            out.append(Prediction(story.media_id, Tags.unsure(), latency, error="BadOutput"))
            continue
        if cache is not None:
            cache.put(tagger.name, tagger.version, story.media_id, CacheEntry(output.tags, latency, output.cost_usd))
        out.append(Prediction(story.media_id, output.tags, latency, output.cost_usd))
    return out
