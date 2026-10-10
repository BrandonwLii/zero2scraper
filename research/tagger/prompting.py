"""Prompt, output schema and request bodies for llama-server (OpenAI-compatible chat endpoint).

The same body is sent from the workstation (development) and from CT 120 (benchmarks) by
``run_requests.py``, so the accuracy and the latency numbers come from identical requests.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Optional

from common import ALL, REPO, StoryInputs, context_text

PROMPTS = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT = "tag_v1.txt"

SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "job_titles": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        **{name: {"type": "array", "items": {"type": "string", "enum": values}, "minItems": 1, "uniqueItems": True}
           for name, values in ALL.items()},
        "evidence": {"type": "string", "maxLength": 160},
    },
    "required": ["companies", "job_titles", *ALL, "evidence"],
    "additionalProperties": False,
}


def _list_for_prompt(heading: str) -> str:
    """'Meta (Facebook, Instagram, ...); Apple; ...' from a company table in docs/tags.md."""
    doc = (REPO / "docs" / "tags.md").read_text(encoding="utf-8")
    section = doc.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    items = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Company", "") or set(cells[0]) <= set("-"):
            continue
        aliases = re.sub(r"\(.*?\)", "", cells[1]).strip().strip(",").strip()
        items.append(f"{cells[0]} ({aliases})" if aliases else cells[0])
    return "; ".join(items) + "."


def system_prompt(name: str = DEFAULT_PROMPT) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    return text.replace("{faang_list}", _list_for_prompt("### FAANG+ list")).replace("{quant_list}", _list_for_prompt("### Quant list"))


def user_text(inp: StoryInputs, ocr_text: Optional[str] = None, with_image: bool = True) -> str:
    head = "The story image is attached.\n" if with_image else "There is no image; use the OCR text below.\n"
    return head + context_text(inp, ocr_text) + "\nReturn the JSON."


def chat_body(inp: StoryInputs, prompt: str = DEFAULT_PROMPT, with_image: bool = True, ocr_text: Optional[str] = None,
              max_tokens: int = 320) -> dict:
    content: list[dict] = []
    if with_image:
        if inp.image is None:
            raise FileNotFoundError("story has no still image")
        b64 = base64.b64encode(inp.image.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": user_text(inp, ocr_text, with_image)})
    return {
        "messages": [{"role": "system", "content": system_prompt(prompt)}, {"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "tags", "schema": SCHEMA}},
    }
