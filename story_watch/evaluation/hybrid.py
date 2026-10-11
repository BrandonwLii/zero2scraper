"""Score the production hybrid classifier with the harness (``--tagger hybrid``).

The watcher fetches the posting and the still live; scoring must not, so this adapter is the test
hook: it reads the posting saved for each story (``<listings>/<media id>.json``, #9's extractor
output as #10 saved it) and the still from local files, and then runs the very
``HybridClassifier.classify`` the service runs. Model replies come from a llama-server
(``LLM_URL``, default the local one; on the workstation run it CPU-only), or, with ``replies``,
from saved replies (``<replies>/<media id>.json`` with a ``content`` field, as #10's
run_requests.py wrote them), which makes the score reproducible without a model. With ``ocr_dir``
the OCR text is read from saved files (``{"text": ...}``) instead of running RapidOCR.

Everything read here is private story data and stays outside the repo.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from .. import llm
from ..config import DEFAULT_LLM_MODEL, DEFAULT_LLM_URL
from ..hybrid import HybridClassifier
from ..jobs import JobPosting
from ..tags import Tags
from .data import EvalStory

DATA = Path.home() / "story-watch-data"
DEFAULT_LISTINGS = DATA / "listings"


def load_listing(path: Path) -> Optional[JobPosting]:
    """A saved listing as the JobPosting the watcher would have built; None when there is none."""
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))

    def s(key: str) -> str:
        v = d.get(key)
        return "" if v in (None, "None") else str(v)

    locs = d.get("locations") or []
    return JobPosting(
        title=s("title") or None,
        company=s("company") or None,
        status=d.get("status") or "failed",
        source=s("source") or "none",
        locations=tuple(str(x) for x in locs) if isinstance(locs, list) else (),
        employment_type=s("employment_type") or None,
        description=s("description"),
        is_event_page="event page" in s("reason"),
    )


class ReplayClient:
    """Saved model replies, one file per media id; ``current`` is set before each story."""

    model = "replay"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.current = ""

    def healthy(self) -> bool:
        return True

    def complete(self, body: dict, timeout: float) -> str:
        path = self.root / f"{self.current}.json"
        if not path.exists():
            raise llm.LlmUnavailable("no saved reply")
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("error"):
            raise llm.LlmError("saved reply is an error")
        return rec.get("content") or ""


class HybridTagger:
    def __init__(self, archive: Path, listings: Optional[Path] = None, images: Optional[Path] = None,
                 replies: Optional[Path] = None, ocr_dir: Optional[Path] = None, use_ocr: bool = True):
        self.archive = Path(archive)
        self.listings = Path(listings) if listings else DEFAULT_LISTINGS
        self.images = Path(images) if images else None
        self.ocr_dir = Path(ocr_dir) if ocr_dir else None
        self._current: Optional[EvalStory] = None
        if replies:
            client: Any = ReplayClient(Path(replies))
        else:
            client = llm.LlmClient(os.environ.get("LLM_URL", "").strip() or DEFAULT_LLM_URL,
                                   os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL)
        ocr = None
        if use_ocr:
            if self.ocr_dir is not None:
                ocr = self._saved_ocr
            else:
                from ..ocr import RapidOcr

                ocr = RapidOcr()
        # No cycle budget: each story is scored on its own, as if it arrived alone.
        self.classifier = HybridClassifier(client, self._image, ocr, cycle_budget=None)
        self.client = client
        mode = "|".join([f"replies={Path(replies).name}" if replies else f"live={client.model}",
                         f"ocr={'saved' if self.ocr_dir else ('rapidocr' if use_ocr else 'off')}"])
        self.name = "hybrid"
        self.version = f"{self.classifier.version}-{hashlib.sha256(mode.encode()).hexdigest()[:6]}-{_code_hash()}"

    def _image(self, item: Any) -> Optional[bytes]:
        mid = item.media_id
        candidates = [self.images / f"{mid}.jpg"] if self.images else []
        candidates += sorted(self.archive.glob(f"*/*_{mid}.jpg"))
        for path in candidates:
            if path.exists():
                return path.read_bytes()
        return None

    def _saved_ocr(self, image: bytes) -> Optional[str]:
        assert self._current is not None and self.ocr_dir is not None
        path = self.ocr_dir / f"{self._current.media_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get("text", "")

    def tag(self, story: EvalStory) -> Tags:
        self._current = story
        if isinstance(self.client, ReplayClient):
            self.client.current = story.media_id
        # The posting archived in the sidecar first; older sidecars fall back to the listings dir.
        posting = story.item.posting
        if posting is None and story.item.links:
            posting = load_listing(self.listings / f"{story.media_id}.json")
        item = replace(story.item, posting=posting)
        self.classifier.start_cycle()
        return self.classifier.classify(item)


def _code_hash() -> str:
    """Editing the tagger's code invalidates cached outputs."""
    here = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for name in ("hybrid.py", "tag_rules.py", "posting_rules.py", "companies.py", "llm.py", "ocr.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()[:8]
