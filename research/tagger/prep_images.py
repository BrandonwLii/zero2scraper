"""Copy one still image per labelled story into the private work dir (``$TAGGER_WORK/images``).

Stories are classified from their still image. A story archived before the archive kept stills only
(#28) may have just its clip; for those, the first frame is saved as its still (one frame, no
sampling). Needs ``imageio-ffmpeg`` for that case only.

    .venv/bin/python prep_images.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from story_watch.evaluation.data import load_stories

from common import ARCHIVE, IMAGES


def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True, mode=0o700)
    stories = load_stories(ARCHIVE).stories
    copied = extracted = missing = 0
    for s in stories:
        out = IMAGES / f"{s.media_id}.jpg"
        if out.exists():
            continue
        jpgs = sorted(ARCHIVE.glob(f"*/*_{s.media_id}.jpg"))
        if jpgs:
            shutil.copyfile(jpgs[0], out)
            copied += 1
            continue
        clips = sorted(ARCHIVE.glob(f"*/*_{s.media_id}.mp4"))
        if not clips:
            missing += 1
            continue
        import imageio_ffmpeg

        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(clips[0]),
                        "-frames:v", "1", "-q:v", "2", str(out)], check=True)
        extracted += 1
    print(f"{len(stories)} stories: {copied} copied, {extracted} first frames extracted, {missing} without media")
    return 0


if __name__ == "__main__":
    sys.exit(main())
