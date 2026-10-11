"""Score the production Claude classifier with the harness (``--tagger claude``).

Like ``evaluation/hybrid.py``: the posting comes from the listing saved per story and the still
from local files, and then the very ``ClaudeClassifier.classify`` the service runs tags the story.
With ``replies`` the Claude calls are replayed from saved records (``<replies>/<media id>.json``,
as the bake-off's ``research/tagger/claude_bakeoff.py run`` wrote them): each record is turned back
into a stream-json transcript, so the production parser, validation and post-processing are what
is scored, with no Claude call. Without ``replies`` it calls ``claude -p`` live with the token in
``CLAUDE_CODE_OAUTH_TOKEN``, which uses the owner's plan: ask first.

Everything read here is private story data and stays outside the repo.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from ..claude import ClaudeClassifier, ClaudeCli, ClaudeUnavailable, RunResult, default_binary
from ..tags import Tags
from .data import EvalStory
from .hybrid import DATA, DEFAULT_LISTINGS, load_listing


class ReplayRunner:
    """Saved ``claude -p`` records, one file per media id; ``current`` is set before each story."""

    model = "replay"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.current = ""

    def run(self, message: str, timeout: float) -> RunResult:
        path = self.root / f"{self.current}.json"
        if not path.exists():
            raise ClaudeUnavailable("no saved reply")
        rec = json.loads(path.read_text(encoding="utf-8"))
        return RunResult(int(rec.get("returncode", 1)), transcript(rec))


def transcript(rec: dict) -> str:
    """The stream-json lines a saved record came from (the fields the parser reads)."""
    lines: list[dict[str, Any]] = []
    if rec.get("tools") is not None:
        lines.append({"type": "system", "subtype": "init", "tools": rec["tools"], "model": rec.get("resolved_model")})
    lines += [{"type": "rate_limit_event", "rate_limit_info": info} for info in rec.get("rate_limit") or [] if info]
    if "subtype" in rec:
        lines.append({"type": "result", "subtype": rec.get("subtype"), "is_error": rec.get("is_error"),
                      "duration_ms": rec.get("duration_ms"), "api_error_status": rec.get("api_error_status"),
                      "structured_output": rec.get("structured_output")})
    return "".join(json.dumps(x) + "\n" for x in lines)


class ClaudeTagger:
    def __init__(self, archive: Path, listings: Optional[Path] = None, images: Optional[Path] = None,
                 replies: Optional[Path] = None, ocr_dir: Optional[Path] = None, use_ocr: bool = True):
        self.archive = Path(archive)
        self.listings = Path(listings) if listings else DEFAULT_LISTINGS
        self.images = Path(images) if images else None
        self.ocr_dir = Path(ocr_dir) if ocr_dir else None
        self._current: Optional[EvalStory] = None
        if replies:
            runner: Any = ReplayRunner(Path(replies))
        else:
            token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            runner = ClaudeCli(default_binary() or "claude", token, DATA / "eval-work" / "claude-home",
                               os.environ.get("CLAUDE_MODEL", "").strip() or "opus")
        ocr = None
        if use_ocr:
            if self.ocr_dir is not None:
                ocr = self._saved_ocr
            else:
                from ..ocr import RapidOcr

                ocr = RapidOcr()
        # No cycle budget: each story is scored on its own, as if it arrived alone.
        self.classifier = ClaudeClassifier(runner, self._image, ocr, cycle_budget=None)
        self.runner = runner
        mode = "|".join([f"replies={Path(replies).name}" if replies else f"live={runner.model}",
                         f"ocr={'saved' if self.ocr_dir else ('rapidocr' if use_ocr else 'off')}"])
        self.name = "claude"
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
        if isinstance(self.runner, ReplayRunner):
            self.runner.current = story.media_id
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
    for name in ("claude.py", "tag_rules.py", "posting_rules.py", "companies.py", "ocr.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()[:8]
