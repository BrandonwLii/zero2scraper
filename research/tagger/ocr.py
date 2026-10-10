#!/usr/bin/env python3
"""RapidOCR over a folder of story stills: ``<out>/<media id>.json`` with the text and the latency.

Needs only rapidocr, onnxruntime, numpy and pillow (no story_watch import), so it runs inside CT 120
with #7's venv. Prints the peak RSS at the end; wrap it in ``/usr/bin/time -v`` for an outside view.

    python ocr.py <images dir> <out dir> [--threads 4]
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("images", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    from PIL import Image
    from rapidocr import RapidOCR

    t0 = time.perf_counter()
    engine = RapidOCR(params={"Global.log_level": "error", "EngineConfig.onnxruntime.intra_op_num_threads": args.threads})
    load_s = time.perf_counter() - t0
    times = []
    for img in sorted(args.images.glob("*.jpg")):
        start = time.perf_counter()
        res = engine(np.asarray(Image.open(img).convert("RGB"))[:, :, ::-1])
        latency = time.perf_counter() - start
        times.append(latency)
        lines = list(res.txts or ())
        (args.out / f"{img.stem}.json").write_text(json.dumps({"text": "\n".join(lines), "latency_s": latency}), encoding="utf-8")
    times.sort()
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({"images": len(times), "load_s": round(load_s, 2), "median_s": round(times[len(times) // 2], 3) if times else None,
                      "max_s": round(times[-1], 3) if times else None, "peak_rss_mb": round(peak_mb)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
