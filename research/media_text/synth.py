"""Generate invented, story-like images with known text (issue #7).

Nothing here comes from a real story. Companies are real names (they matter for the FAANG+ /
Quant lists in docs/tags.md); every role, sentence and number is made up.

The output has the same layout as the service archive (story_watch/archive.py), so bench.py
reads both the same way:

    OUT/synthetic/<stem>.json     sidecar (media_id, files, links, ...)
    OUT/synthetic/<stem>.jpg      media
    OUT/media_text_truth.jsonl    reference text and facts, one line per media_id

    .venv/bin/python synth.py --out ~/story-watch-data/media-text-research/synthetic \
        --fonts ~/story-watch-data/media-text-research/fonts
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
HARD = False  # --hard: busy backgrounds, low-contrast text, small and heavily compressed

# (company, group) -- group is only for the report's breakdown.
COMPANIES = [
    ("Google", "faang_plus"), ("Meta", "faang_plus"), ("Amazon", "faang_plus"),
    ("Netflix", "faang_plus"), ("Microsoft", "faang_plus"), ("Apple", "faang_plus"),
    ("Jane Street", "quant"), ("Citadel", "quant"), ("Hudson River Trading", "quant"),
    ("Two Sigma", "quant"), ("Shopify", "other"), ("Wealthsimple", "other"),
    ("Cohere", "other"), ("RBC", "other"), ("Stripe", "other"), ("Databricks", "other"),
]
ROLES = [
    "Software Engineer Intern", "Machine Learning Engineer Intern", "Product Manager Intern",
    "New Grad Software Engineer", "Quantitative Developer Intern", "Data Scientist, New Grad",
    "Associate Product Manager", "Software Engineer I", "ML Research Intern",
]
LOCATIONS = ["Toronto, ON", "Waterloo, ON", "Vancouver, BC", "New York, NY", "Seattle, WA",
             "San Francisco, CA", "Remote (Canada)", "London, UK", "Chicago, IL"]
SPONSOR = [
    "Must be authorized to work in the US without sponsorship",
    "Visa sponsorship available",
    "US citizenship required",
    "Open to candidates in Canada",
    None, None, None,
]
TERMS = ["Summer 2027", "Fall 2027", "Winter 2027", "2027 start"]
EMOJI = ["\U0001F680", "\U0001F525", "\U0001F4BC", "✨", "\U0001F440", "\U0001F4E2", "\U0001F64C"]
MISC_LINES = [
    "leg day again", "who else is studying for midterms", "coffee count: 4",
    "rate my desk setup", "weekend vibes", "new blog post up",
]
PROCESS_LINES = [
    ("{c} OA", "3 questions, 70 minutes, no partial credit"),
    ("{c} final round", "2 coding + 1 behavioural, all virtual"),
    ("How I prepped for {c}", "6 weeks of mock interviews"),
]


class Fonts:
    def __init__(self, folder: Path | None):
        found = {p.stem: p for p in folder.glob("*.ttf")} if folder else {}
        fallback = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        self.display = [str(found[n]) for n in ("Pacifico", "Anton", "BebasNeue", "Caveat",
                                                "PlayfairDisplay", "Montserrat") if n in found] or [fallback]
        self.ui = str(found.get("Inter", fallback))
        self.emoji = str(found["NotoColorEmoji"]) if "NotoColorEmoji" in found else None
        self.emoji_ok = False
        if self.emoji:  # Pillow renders CBDT bitmap emoji, not the COLRv1 build
            try:
                ImageFont.truetype(self.emoji, 109)
                tile = Image.new("RGBA", (140, 140))
                ImageDraw.Draw(tile).text((10, 10), EMOJI[0], font=ImageFont.truetype(self.emoji, 109), embedded_color=True)
                self.emoji_ok = tile.getbbox() is not None
            except OSError:
                pass

    def get(self, path: str, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(path, size)


def photo_background(rng: random.Random) -> Image.Image:
    """A blurry, noisy 'photo': colour blobs over a gradient."""
    base = np.zeros((H, W, 3), np.float32)
    c1, c2 = np.array([rng.randint(0, 255) for _ in range(3)]), np.array([rng.randint(0, 255) for _ in range(3)])
    t = np.linspace(0, 1, H)[:, None, None]
    base[:] = c1 * (1 - t) + c2 * t
    img = Image.fromarray(base.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(rng.randint(6, 14)):
        x, y, r = rng.randint(-200, W), rng.randint(-200, H), rng.randint(80, 500)
        d.ellipse((x, y, x + r, y + r), fill=tuple(rng.randint(0, 255) for _ in range(3)))
    img = img.filter(ImageFilter.GaussianBlur(rng.randint(15, 40)))
    if HARD:  # sharp clutter, like a real photo behind the text
        d = ImageDraw.Draw(img)
        for _ in range(400):
            x, y = rng.randint(0, W), rng.randint(0, H)
            d.line((x, y, x + rng.randint(-120, 120), y + rng.randint(-120, 120)),
                   fill=tuple(rng.randint(0, 255) for _ in range(3)), width=rng.randint(1, 6))
    noise = np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, 12, (H, W, 3))
    return Image.fromarray(np.clip(np.asarray(img, np.float32) + noise, 0, 255).astype(np.uint8))


def draw_text(img: Image.Image, xy, text, font, style: str, rng: random.Random, angle: float = 0.0):
    """Instagram-ish text styles: plain, outline, shadow, or a coloured box behind the text."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fill = (255, 255, 255, 255) if style != "box" else (20, 20, 20, 255)
    if HARD and style == "plain":
        fill = (235, 235, 220, 255)
    x, y = xy
    bbox = d.textbbox((x, y), text, font=font)
    if style == "box":
        pad = 18
        d.rounded_rectangle((bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad), 20,
                            fill=(255, 255, 255, 235))
    if style == "shadow":
        d.text((x + 5, y + 5), text, font=font, fill=(0, 0, 0, 160))
    if style == "outline":
        d.text((x, y), text, font=font, fill=fill, stroke_width=4, stroke_fill=(0, 0, 0, 255))
    else:
        d.text((x, y), text, font=font, fill=fill)
    if angle:
        layer = layer.rotate(angle, center=((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2),
                             resample=Image.BICUBIC)
    img.paste(layer, (0, 0), layer)
    return bbox[3]


def fit_font(fonts: Fonts, path: str, text: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > 20:
        f = fonts.get(path, size)
        if f.getlength(text) <= max_w:
            return f
        size -= 4
    return fonts.get(path, size)


def add_emoji(img: Image.Image, fonts: Fonts, rng: random.Random, n: int) -> None:
    if not fonts.emoji or not fonts.emoji_ok:
        # No bitmap emoji font: draw sticker-like shapes so there is still clutter.
        d = ImageDraw.Draw(img)
        for _ in range(n):
            s, x, y = rng.randint(80, 180), rng.randint(0, W - 180), rng.randint(0, H - 180)
            colour = tuple(rng.randint(0, 255) for _ in range(3))
            if rng.random() < 0.5:
                d.ellipse((x, y, x + s, y + s), fill=colour, outline=(255, 255, 255), width=8)
            else:
                pts = [(x + s / 2 + s / 2 * math.cos(a * math.pi / 5) * (1 if a % 2 == 0 else 0.45),
                        y + s / 2 + s / 2 * math.sin(a * math.pi / 5) * (1 if a % 2 == 0 else 0.45)) for a in range(10)]
                d.polygon(pts, fill=colour, outline=(255, 255, 255))
        return
    f = ImageFont.truetype(fonts.emoji, 109)  # the only size a CBDT emoji font renders
    for _ in range(n):
        tile = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((10, 10), rng.choice(EMOJI), font=f, embedded_color=True)
        tile = tile.resize((s := rng.randint(80, 180), s))
        img.paste(tile, (rng.randint(0, W - s), rng.randint(0, H - s)), tile)


def job_card(fonts: Fonts, company, role, location, sponsor, term, rng) -> Image.Image:
    """A job-board-style screenshot card (white, small grey text)."""
    cw = 900
    card = Image.new("RGB", (cw, 700), (255, 255, 255))
    d = ImageDraw.Draw(card)
    d.rectangle((40, 40, 140, 140), fill=tuple(rng.randint(30, 200) for _ in range(3)))
    d.text((165, 50), company, font=fonts.get(fonts.ui, 40), fill=(30, 30, 30))
    d.text((165, 100), f"{location} · {rng.randint(2, 30)} days ago", font=fonts.get(fonts.ui, 28), fill=(110, 110, 110))
    d.text((40, 180), role, font=fit_font(fonts, fonts.ui, role, cw - 80, 52), fill=(10, 10, 10))
    d.text((40, 260), term, font=fonts.get(fonts.ui, 30), fill=(60, 60, 60))
    y = 320
    for line in ["About the role", "You will build systems used by millions of people.",
                 "Pursuing a degree in CS or a related field."] + ([sponsor] if sponsor else []):
        d.text((40, y), line, font=fonts.get(fonts.ui, 26), fill=(90, 90, 90))
        y += 50
    d.rounded_rectangle((40, 600, 300, 670), 35, fill=(10, 102, 194))
    d.text((110, 615), "Apply", font=fonts.get(fonts.ui, 34), fill=(255, 255, 255))
    return card


def make_story(kind: str, fonts: Fonts, rng: random.Random) -> tuple[Image.Image, dict]:
    img = photo_background(rng)
    company, _group = rng.choice(COMPANIES)
    role, location, sponsor, term = rng.choice(ROLES), rng.choice(LOCATIONS), rng.choice(SPONSOR), rng.choice(TERMS)
    facts: dict[str, list[str]] = {"company": [], "role": [], "level": [], "location": [], "sponsorship": []}
    lines: list[str] = []
    style = rng.choice(["plain", "plain", "shadow", "box"] if HARD else ["plain", "outline", "shadow", "box"])
    display = rng.choice(fonts.display)
    angle = rng.choice([-9, -5, 0, 7] if HARD else [0, 0, -6, 4])

    if kind == "overlay":
        head = f"{company} is hiring!"
        sub = f"{role} · {term}"
        y = draw_text(img, (80, 380), head, fit_font(fonts, display, head, W - 160, 130), style, rng, angle)
        y = draw_text(img, (80, y + 80), sub, fit_font(fonts, display, sub, W - 160, 70), style, rng)
        draw_text(img, (80, y + 70), location, fit_font(fonts, display, location, W - 160, 64), style, rng)
        lines = [head, sub, location]
        facts.update(company=[company], role=[role], location=[location])
    elif kind == "screenshot":
        card = job_card(fonts, company, role, location, sponsor, term, rng)
        scale = rng.uniform(0.55, 0.85)
        card = card.resize((int(card.width * scale), int(card.height * scale)), Image.LANCZOS)
        img.paste(card, ((W - card.width) // 2, rng.randint(500, 900)))
        note = rng.choice(["apply asap", "new posting", "just dropped", "link below"])
        draw_text(img, (90, 260), note, fit_font(fonts, display, note, W - 180, 90), style, rng)
        lines = [note, company, location, role, term] + ([sponsor] if sponsor else [])
        facts.update(company=[company], role=[role], location=[location], sponsorship=[sponsor] if sponsor else [])
    elif kind == "roundup":
        picks = rng.sample(COMPANIES, 3)
        level = rng.choice(["new grad", "intern"])
        head = f"3 {level} roles open now"
        y = draw_text(img, (70, 250), head, fit_font(fonts, display, head, W - 140, 100), style, rng)
        f = fonts.get(fonts.ui, 46)
        rows = []
        for c, _g in picks:
            loc = rng.choice(LOCATIONS)
            row = f"{c} — {loc}"
            y = draw_text(img, (90, y + 70), row, f, rng.choice(["box", "shadow"]), rng)
            rows.append(row)
            facts["company"].append(c)
            facts["location"].append(loc)
        lines = [head] + rows
        facts["level"] = [level]
    elif kind == "process":
        title, detail = rng.choice(PROCESS_LINES)
        title = title.format(c=company)
        y = draw_text(img, (80, 600), title, fit_font(fonts, display, title, W - 160, 110), style, rng, angle)
        draw_text(img, (80, y + 80), detail, fit_font(fonts, fonts.ui, detail, W - 160, 56), "box", rng)
        lines = [title, detail]
        facts["company"] = [company]
    elif kind == "misc":
        text = rng.choice(MISC_LINES)
        draw_text(img, (80, rng.randint(300, 1400)), text, fit_font(fonts, display, text, W - 160, 110), style, rng, angle)
        lines = [text]
    if facts["role"] and not facts["level"]:
        facts["level"] = [w for w in ("Intern", "New Grad") if w.lower() in facts["role"][0].lower()]
    add_emoji(img, fonts, rng, rng.randint(0, 3))
    return img, {"kind": kind, "text": "\n".join(lines), "facts": {k: v for k, v in facts.items() if v},
                 "style": style, "font": Path(display).stem, "angle": angle}


def cdn_like(img: Image.Image, rng: random.Random) -> Image.Image:
    """Down-scale and JPEG-compress like the CDN does (archived widths are 1080 or 640)."""
    width = 640 if HARD else rng.choice([1080, 720, 640])
    if width != W:
        img = img.resize((width, int(H * width / W)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=55 if HARD else rng.choice([70, 80, 90]))
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


PLAN = (["overlay"] * 7 + ["screenshot"] * 8 + ["roundup"] * 4 + ["process"] * 4 + ["misc"] * 4)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fonts", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--hard", action="store_true", help="busy backgrounds, low contrast, 640 px, JPEG q55")
    args = ap.parse_args()
    global HARD
    HARD = args.hard
    rng = random.Random(args.seed)
    fonts = Fonts(args.fonts)
    folder = args.out / ("synthetic-hard" if HARD else "synthetic")
    folder.mkdir(parents=True, exist_ok=True)
    truth = []
    t0 = 1_800_000_000

    def sidecar(stem, media_id, files):
        doc = {"media_id": media_id, "target": folder.name, "taken_at": "2026-10-08T00:00:00+00:00",
               "is_video": False, "links": [], "mentions": [], "job_title": None, "company": None,
               "classifier": "", "category": "misc", "files": files, "download_error": None,
               "node": {"synthetic": True}, "page_node": None}
        (folder / f"{stem}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

    for i, kind in enumerate(PLAN):
        img, meta = make_story(kind, fonts, rng)
        img = cdn_like(img, rng)
        media_id, stem = str(t0 + i), f"20261008T0000{i:02d}Z_{t0 + i}"
        img.save(folder / f"{stem}.jpg", quality=95)
        sidecar(stem, media_id, [f"{stem}.jpg"])
        truth.append({"media_id": media_id, **meta})

    with open(args.out / "media_text_truth.jsonl", "w", encoding="utf-8") as fh:
        for t in truth:
            fh.write(json.dumps(t) + "\n")
    print(f"wrote {len(truth)} items to {args.out}")


if __name__ == "__main__":
    main()
