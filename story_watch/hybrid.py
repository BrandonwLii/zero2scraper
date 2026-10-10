"""The hybrid classifier (``CLASSIFIER=hybrid``), chosen in #10 and signed off on 2026-10-10.

Per story: download the still, run RapidOCR on it, ask the local model (llm.py) for the post type
and the role, and let the rules (tag_rules.py) decide company, sponsorship and level. When the
model can't answer (down, timed out, bad reply, no image), the story gets the fallback tags
(``listing_rules``, with the OCR text when there is some). If even that raises, the watcher uses
``Tags.unsure()``. Tagging never blocks a notification; it delays each new story by at most the
model timeout, and a cycle by at most ``cycle_budget`` seconds of model time (see ``start_cycle``).
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import replace
from typing import Any, Callable, Optional

import requests

from . import llm
from .instagram import StoryItem
from .jobs import _pinned_send, check_public
from .tag_rules import RULES_VERSION, StoryInputs, context_text, hybrid_tags, listing_rules, parse_model_json
from .tags import Tags

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 90.0
# Model time per watcher cycle. With the per-story timeout this bounds how long a burst of new
# stories waits: once it is spent, the rest of the cycle's stories get the fallback tags.
CYCLE_BUDGET = 600.0
MIN_CALL = 20.0  # don't start a model call with less budget left than this
MAX_IMAGE_BYTES = 10_000_000
IMAGE_TIMEOUT = 20.0


class CdnImages:
    """Downloads a story's still for tagging, with the archive's guards: a fresh request with no
    cookies (never the instaloader session), https only, a public address (pinned), no redirects,
    a size cap. The URL is signed, so it is never logged."""

    def __init__(self, timeout: float = IMAGE_TIMEOUT, resolve: Callable[..., list] = socket.getaddrinfo,
                 transport: Callable[..., Any] = _pinned_send):
        self.timeout, self.resolve, self.transport = timeout, resolve, transport

    def __call__(self, item: StoryItem) -> Optional[bytes]:
        if not item.thumbnail_url:
            return None
        deadline = time.monotonic() + self.timeout
        try:
            ip = check_public(item.thumbnail_url, self.resolve)
            resp = self.transport("GET", item.thumbnail_url, ip, {"User-Agent": "Mozilla/5.0", "Accept": "image/*"},
                                  self.timeout)
            try:
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                body = b""
                for chunk in resp.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_IMAGE_BYTES or time.monotonic() > deadline:
                        raise ValueError("over the size or time cap")
                return body or None
            finally:
                resp.close()
                adapter = getattr(resp, "_pinned_adapter", None)
                if adapter is not None:
                    adapter.close()
        except (requests.RequestException, ValueError, OSError) as e:
            log.warning("tagger: still image for %s not downloaded (%s)", item.media_id, type(e).__name__)
            return None


class HybridClassifier:
    def __init__(
        self,
        client: Any,
        images: Callable[[StoryItem], Optional[bytes]],
        ocr: Optional[Callable[[bytes], Optional[str]]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        cycle_budget: Optional[float] = CYCLE_BUDGET,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.images = images
        self.ocr = ocr
        self.timeout = timeout
        self.cycle_budget = cycle_budget
        self.clock = clock
        self._budget_left = cycle_budget
        self._skip_model = False  # set for the rest of a cycle after a failed health check or call
        self.version = f"{RULES_VERSION}-{llm.prompt_hash()}-{getattr(client, 'model', '')}"

    def start_cycle(self) -> bool:
        """Called by the watcher before each cycle: resets the cycle's model budget and checks the
        model's /health. Returns whether the model is up; when it isn't, this cycle's stories get
        the fallback tags without waiting on it. Never raises."""
        self._budget_left = self.cycle_budget
        try:
            up = bool(self.client.healthy())
        except Exception as e:
            log.warning("tagger health check failed (%s)", type(e).__name__)
            up = False
        self._skip_model = not up
        return up

    def classify(self, item: StoryItem) -> Tags:
        inp = StoryInputs.from_item(item)
        try:
            image = self.images(item)
        except Exception as e:  # CdnImages catches network errors; anything else still falls back
            log.warning("tagger: still image for %s failed (%s)", item.media_id, type(e).__name__)
            image = None
        ocr_text = None
        if image is not None and self.ocr is not None:
            try:
                ocr_text = self.ocr(image)
            except Exception as e:
                log.warning("tagger: OCR failed on %s (%s)", item.media_id, type(e).__name__)
        reply, why = self._ask_model(item, image, inp)
        if reply is not None:
            try:
                return hybrid_tags(reply, inp, ocr_text)
            except Exception as e:  # a reply shape the rules didn't expect: still the fallback
                log.warning("tagger: rules failed on the reply for %s (%s)", item.media_id, type(e).__name__)
                why = type(e).__name__
        tags = listing_rules(inp, ocr_text)
        log.info("tagger: %s used the fallback rules (%s)", item.media_id, why)
        return replace(tags, evidence=f"fallback: {why}")

    def _ask_model(self, item: StoryItem, image: Optional[bytes], inp: StoryInputs) -> tuple[Optional[dict], str]:
        """(the parsed reply, "") or (None, why the fallback is used)."""
        if image is None:
            return None, "no image"
        if self._skip_model:
            return None, "model unavailable"
        timeout = self.timeout
        if self._budget_left is not None:
            if self._budget_left < MIN_CALL:
                return None, "cycle model budget spent"
            timeout = min(timeout, self._budget_left)
        # #10 sent no OCR text to the model (it only feeds the rules); keep the request as scored.
        body = llm.chat_body(image, context_text(inp), getattr(self.client, "model", ""))
        start = self.clock()
        try:
            raw = self.client.complete(body, timeout)
            reply = parse_model_json(raw)
        except (llm.LlmTimeout, llm.LlmUnavailable) as e:
            # A timed-out request may still hold the server's only slot: skip the model for the
            # rest of the cycle instead of queueing more stories behind it.
            self._skip_model = True
            log.warning("tagger: model call for %s failed (%s); fallback tags", item.media_id, type(e).__name__)
            return None, type(e).__name__
        except (llm.LlmError, ValueError) as e:
            log.warning("tagger: model reply for %s unusable (%s); fallback tags", item.media_id, type(e).__name__)
            return None, type(e).__name__
        finally:
            elapsed = self.clock() - start
            if self._budget_left is not None:
                self._budget_left -= elapsed
        log.info("tagger: model tagged %s in %.1fs", item.media_id, elapsed)
        return reply, ""


def from_config(cfg: Any = None) -> HybridClassifier:
    """The production classifier: the local llama-server, CDN stills and (optionally) RapidOCR."""
    from .config import DEFAULT_LLM_MODEL, DEFAULT_LLM_URL

    url = getattr(cfg, "llm_url", DEFAULT_LLM_URL)
    model = getattr(cfg, "llm_model", DEFAULT_LLM_MODEL)
    timeout = float(getattr(cfg, "llm_timeout", DEFAULT_TIMEOUT))
    ocr = None
    if getattr(cfg, "tagger_ocr", True):
        from .ocr import RapidOcr

        ocr = RapidOcr()
    return HybridClassifier(llm.LlmClient(url, model), CdnImages(), ocr, timeout=timeout)
