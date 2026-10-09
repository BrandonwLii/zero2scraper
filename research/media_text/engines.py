"""Text readers for bench.py. Each engine turns a list of images into text and, optionally, facts.

An engine is a class with `name`, `load()` (timed separately as cold start) and
`read(images) -> Result`. `images` is a list holding one PIL image: stories are classified
from a single still image. Heavy imports happen inside `load()` so `bench.py --engines rapidocr` doesn't need
torch installed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field

import requests
from PIL import Image

OLLAMA = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
LLAMA_SERVER = os.environ.get("LLAMA_SERVER", "http://127.0.0.1:8080")
THREADS = int(os.environ.get("MEDIA_TEXT_THREADS", "0")) or None  # None: the library's default

FACT_KEYS = ("companies", "roles", "levels", "locations", "work_authorization")

TRANSCRIBE_PROMPT = (
    "Transcribe all text visible in this Instagram story image, including small text inside "
    "screenshots. Output only the text, one line per line of text. Skip emoji and decorations. "
    "If there is no text, output nothing."
)
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "companies": {"type": "array", "items": {"type": "string"}},
        "roles": {"type": "array", "items": {"type": "string"}},
        "levels": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "work_authorization": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", *FACT_KEYS],
}
EXTRACT_RULES = (
    "Fill these fields, copying wording exactly as written:\n"
    "- text: all visible text.\n"
    "- companies: company names that are written or shown.\n"
    "- roles: job titles.\n"
    "- levels: seniority words such as intern, co-op, new grad, entry level.\n"
    "- locations: cities, countries, 'remote' wording.\n"
    "- work_authorization: sentences about visas, sponsorship, citizenship or work authorization.\n"
    "Use an empty list when the story doesn't say. Never guess a company from colours or style."
)
EXTRACT_IMAGE_PROMPT = "You read Instagram stories about tech jobs. " + EXTRACT_RULES
EXTRACT_TEXT_PROMPT = (
    "Below is OCR output from an Instagram story about tech jobs. OCR may split or garble lines.\n"
    + EXTRACT_RULES + "\n\nOCR text:\n"
)


@dataclass
class Result:
    text: str
    facts: dict[str, list[str]] | None = None
    extra: dict = field(default_factory=dict)


def _png_b64(img: Image.Image, max_side: int) -> str:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        s = max_side / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_facts(raw: str) -> tuple[str, dict[str, list[str]] | None]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return raw, None
        try:
            doc = json.loads(m.group(0))
        except json.JSONDecodeError:
            return raw, None
    facts = {k: [str(v) for v in doc.get(k) or [] if str(v).strip()] for k in FACT_KEYS}
    return str(doc.get("text") or ""), facts


# -- classic OCR ---------------------------------------------------------------------------


class RapidOCR:
    """PaddleOCR's PP-OCR detection + recognition models, run through onnxruntime."""

    name = "rapidocr"

    def load(self):
        from rapidocr import RapidOCR as R

        params = {"Global.log_level": "error"}
        if THREADS:
            params.update({"EngineConfig.onnxruntime.intra_op_num_threads": THREADS})
        self.r = R(params=params)

    def read(self, images):
        import numpy as np

        lines = []
        for im in images:
            out = self.r(np.asarray(im.convert("RGB"))[:, :, ::-1])
            if out.txts:
                lines.extend(out.txts)
        return Result("\n".join(lines))


class EasyOCR:
    name = "easyocr"

    def load(self):
        import easyocr
        import torch

        if THREADS:
            torch.set_num_threads(THREADS)
        self.r = easyocr.Reader(["en"], gpu=False, verbose=False)

    def read(self, images):
        import numpy as np

        lines = []
        for im in images:
            lines.extend(self.r.readtext(np.asarray(im.convert("RGB")), detail=0, paragraph=False))
        return Result("\n".join(lines))


class DocTR:
    name = "doctr"

    def load(self):
        import torch
        from doctr.models import ocr_predictor

        if THREADS:
            torch.set_num_threads(THREADS)
        self.m = ocr_predictor(det_arch="db_resnet50", reco_arch="crnn_vgg16_bn", pretrained=True)

    def read(self, images):
        import numpy as np

        doc = self.m([np.asarray(im.convert("RGB")) for im in images])
        lines = [" ".join(w.value for w in line.words)
                 for page in doc.pages for block in page.blocks for line in block.lines]
        return Result("\n".join(lines))


class Florence2:
    """Florence-2 (0.23B base / 0.77B large) <OCR> task, CPU, via transformers."""

    def __init__(self, size="base"):
        self.name = f"florence2-{size}"
        self.repo = f"florence-community/Florence-2-{size}"

    def load(self):
        import torch
        from transformers import AutoProcessor, Florence2ForConditionalGeneration

        if THREADS:
            torch.set_num_threads(THREADS)
        self.torch = torch
        self.proc = AutoProcessor.from_pretrained(self.repo)
        self.m = Florence2ForConditionalGeneration.from_pretrained(self.repo, torch_dtype=torch.float32).eval()

    def read(self, images):
        lines = []
        for im in images:
            inputs = self.proc(text="<OCR_WITH_REGION>", images=im.convert("RGB"), return_tensors="pt")
            with self.torch.inference_mode():
                ids = self.m.generate(**inputs, max_new_tokens=512, num_beams=3, do_sample=False)
            raw = self.proc.batch_decode(ids, skip_special_tokens=False)[0]
            parsed = self.proc.post_process_generation(raw, task="<OCR_WITH_REGION>", image_size=im.size)
            lines.extend(parsed["<OCR_WITH_REGION>"]["labels"])
        return Result("\n".join(l.replace("</s>", "").strip() for l in lines))


# -- models served by Ollama ----------------------------------------------------------------


def _ollama(payload: dict, timeout=900) -> dict:
    opts = payload.setdefault("options", {})
    opts.setdefault("temperature", 0)
    opts.setdefault("num_ctx", 8192)
    if THREADS:
        opts["num_thread"] = THREADS
    r = requests.post(f"{OLLAMA}/api/chat", json={**payload, "stream": False, "keep_alive": "10m"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def unload_all() -> None:
    """Unload every model Ollama has in memory (no-op when Ollama isn't running)."""
    try:
        loaded = requests.get(f"{OLLAMA}/api/ps", timeout=5).json().get("models") or []
        for m in loaded:
            requests.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=60)
    except requests.RequestException:
        pass


class OllamaVLM:
    """A vision-language model reading the image directly.

    mode "transcribe": plain text. mode "extract": text + facts as JSON, in one call.
    """

    def __init__(self, model: str, mode: str = "extract", max_side: int = 1280):
        self.model, self.mode, self.max_side = model, mode, max_side
        self.name = f"vlm:{model}:{mode}"

    def load(self):
        _ollama({"model": self.model, "messages": [{"role": "user", "content": "hi"}],
                 "options": {"num_predict": 1}})

    def read(self, images):
        imgs = [_png_b64(im, self.max_side) for im in images]
        if self.mode == "transcribe":
            out = _ollama({"model": self.model, "think": False,
                           "messages": [{"role": "user", "content": TRANSCRIBE_PROMPT, "images": imgs}],
                           "options": {"num_predict": 768}})
            return Result(out["message"]["content"], extra={"eval_count": out.get("eval_count")})
        out = _ollama({"model": self.model, "think": False, "format": EXTRACT_SCHEMA,
                       "messages": [{"role": "user", "content": EXTRACT_IMAGE_PROMPT, "images": imgs}],
                       "options": {"num_predict": 1024}})
        text, facts = _parse_facts(out["message"]["content"])
        return Result(text, facts, extra={"eval_count": out.get("eval_count")})


class OcrThenLLM:
    """Classic OCR, then a text-only model pulls the facts out of the OCR text."""

    def __init__(self, ocr, model: str):
        self.ocr, self.model = ocr, model
        self.name = f"{ocr.name}+llm:{model}"

    def load(self):
        self.ocr.load()
        _ollama({"model": self.model, "messages": [{"role": "user", "content": "hi"}],
                 "options": {"num_predict": 1}})

    def read(self, images):
        text = self.ocr.read(images).text
        out = _ollama({"model": self.model, "think": False, "format": EXTRACT_SCHEMA,
                       "messages": [{"role": "user", "content": EXTRACT_TEXT_PROMPT + text}],
                       "options": {"num_predict": 1024}})
        _t, facts = _parse_facts(out["message"]["content"])
        return Result(text, facts)


# -- models served by llama.cpp's llama-server (what the target box runs) -------------------
# Start the server yourself with the thread count and model under test, e.g.
#   llama-server -m Qwen3VL-4B-Instruct-Q4_K_M.gguf --mmproj mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf \
#       -t 6 -c 8192 --host 127.0.0.1 --port 8080
# The engine label is only a name for the results.


def _llamacpp(messages: list, schema: dict | None, max_tokens: int, timeout=1800) -> str:
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "facts", "schema": schema}}
    r = requests.post(f"{LLAMA_SERVER}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def _wait_llamacpp(timeout=900) -> None:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if requests.get(f"{LLAMA_SERVER}/health", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("llama-server not healthy")


class LlamaCppVLM:
    def __init__(self, label: str, mode: str = "extract", max_side: int = 1280):
        self.label, self.mode, self.max_side = label, mode, max_side
        self.name = f"vlmcpp:{label}:{mode}"

    def load(self):
        _wait_llamacpp()

    def read(self, images):
        content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _png_b64(im, self.max_side)}}
                   for im in images]
        if self.mode == "transcribe":
            prompt = [{"role": "user", "content": content + [{"type": "text", "text": TRANSCRIBE_PROMPT}]}]
            return Result(_llamacpp(prompt, None, 768))
        prompt = [{"role": "user", "content": content + [{"type": "text", "text": EXTRACT_IMAGE_PROMPT}]}]
        text, facts = _parse_facts(_llamacpp(prompt, EXTRACT_SCHEMA, 1024))
        return Result(text, facts)


class OcrThenLlamaCpp:
    def __init__(self, ocr, label: str):
        self.ocr, self.label = ocr, label
        self.name = f"{ocr.name}+llmcpp:{label}"

    def load(self):
        self.ocr.load()
        _wait_llamacpp()

    def read(self, images):
        text = self.ocr.read(images).text
        _t, facts = _parse_facts(_llamacpp([{"role": "user", "content": EXTRACT_TEXT_PROMPT + text}],
                                           EXTRACT_SCHEMA, 1024))
        return Result(text, facts)


def build(spec: str):
    """'rapidocr', 'easyocr', 'doctr', 'florence2-base', 'florence2-large',
    'vlm:<ollama model>[:transcribe|:extract]', 'rapidocr+llm:<ollama model>',
    'vlmcpp:<label>[:transcribe|:extract]', 'rapidocr+llmcpp:<label>' (llama-server at LLAMA_SERVER)."""
    simple = {"rapidocr": RapidOCR, "easyocr": EasyOCR, "doctr": DocTR}
    if spec in simple:
        return simple[spec]()
    if "+llm:" in spec:
        ocr, model = spec.split("+llm:", 1)
        return OcrThenLLM(build(ocr), model)
    if "+llmcpp:" in spec:
        ocr, label = spec.split("+llmcpp:", 1)
        return OcrThenLlamaCpp(build(ocr), label)
    if spec.startswith("vlmcpp:"):
        rest, mode = spec[7:], "extract"
        if rest.endswith((":transcribe", ":extract")):
            rest, mode = rest.rsplit(":", 1)
        return LlamaCppVLM(rest, mode)
    if spec.startswith("florence2-"):
        return Florence2(spec.split("-", 1)[1])
    if spec.startswith("vlm:"):
        rest = spec[4:]
        mode = "extract"
        if rest.endswith((":transcribe", ":extract")):
            rest, mode = rest.rsplit(":", 1)
        return OllamaVLM(rest, mode)
    raise SystemExit(f"unknown engine {spec!r}")
