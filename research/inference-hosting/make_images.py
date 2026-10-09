"""Write synthetic story-sized images for latency and memory benchmarks.

No real story media is used: latency and memory depend on the image size and
the number of output tokens, not on what the story says. The text is invented
filler in the shape of a typical story (a headline over a photo-like
background, and a dense screenshot of a job post).

    .venv/bin/python make_images.py OUT_DIR
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920  # Instagram story resolution

HEADLINE = ["EXAMPLE CORP", "is hiring", "Software Engineer Intern", "Summer 2027", "Toronto, ON", "link below"]
POST = [
    "Example Corp", "Software Engineering Intern (Summer 2027)", "Toronto, Ontario - Hybrid",
    "About the role", "You will build services used by millions of people.",
    "Work with a team on backend systems and tooling.", "Qualifications",
    "Currently enrolled in a CS or related degree.", "Experience with Python, Go or Java.",
    "Graduating between Dec 2027 and Jun 2028.", "Visa sponsorship is not available for this role.",
    "Apply by November 30.", "Posted 2 days ago - 300 applicants",
]


def font(size: int) -> ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def overlay(rng: random.Random) -> Image.Image:
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(0, H, 4):  # noisy gradient stands in for a photo
        for x in range(0, W, 4):
            c = (int(80 + 100 * x / W) + rng.randint(-30, 30), int(60 + 120 * y / H) + rng.randint(-30, 30), 140)
            for dy in range(4):
                for dx in range(4):
                    px[x + dx, y + dy] = c
    d = ImageDraw.Draw(img)
    y = 500
    for i, line in enumerate(HEADLINE):
        f = font(96 if i == 0 else 60)
        d.text((80, y), line, font=f, fill="white", stroke_width=4, stroke_fill="black")
        y += 140
    d.rounded_rectangle((300, 1500, 780, 1600), radius=30, fill="white")
    d.text((340, 1525), "apply here", font=font(48), fill="black")
    return img


def screenshot() -> Image.Image:
    img = Image.new("RGB", (W, H), (20, 20, 20))
    d = ImageDraw.Draw(img)
    d.rectangle((60, 300, W - 60, 1600), fill="white")
    y = 340
    for i, line in enumerate(POST):
        d.text((100, y), line, font=font(44 if i < 2 else 32), fill="black")
        y += 80 if i < 3 else 62
    return img


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    overlay(random.Random(1)).save(out / "overlay.jpg", quality=85)
    screenshot().save(out / "screenshot.jpg", quality=85)
    print(f"wrote 2 images to {out}")


if __name__ == "__main__":
    main()
