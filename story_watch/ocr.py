"""RapidOCR over a story's still image (#7, #10): text for the rules and for the fallback.

Loaded on first use, in the watcher process. It needs the ``ocr`` extra (``pip install
.[ocr]``; install.sh installs it). Without it, or if the engine fails, the tagger runs without OCR
text (the hybrid then misses a little more, see docs/research/tagger-comparison.md). Measured in
CT 120: about 1 s per image with 4 threads and 0.75 GiB peak; the engine stays resident after.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)


class RapidOcr:
    def __init__(self, threads: int = 4):
        self.threads = threads
        self._engine: Any = None
        self._broken = False
        self._lock = threading.Lock()

    def _load(self) -> Any:
        if self._engine is None and not self._broken:
            try:
                from rapidocr import RapidOCR

                self._engine = RapidOCR(params={"Global.log_level": "error",
                                                "EngineConfig.onnxruntime.intra_op_num_threads": self.threads})
            except Exception as e:  # ImportError without the extra, or a broken install
                self._broken = True
                log.warning("OCR unavailable (%s); tagging without OCR text", type(e).__name__)
        return self._engine

    def __call__(self, image: bytes) -> Optional[str]:
        """The image's text lines joined by newlines, or None when OCR isn't available."""
        with self._lock:
            engine = self._load()
            if engine is None:
                return None
            import numpy as np
            from PIL import Image

            rgb = Image.open(io.BytesIO(image)).convert("RGB")
            res = engine(np.asarray(rgb)[:, :, ::-1])
            return "\n".join(res.txts or ())
