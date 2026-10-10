"""The local vision model behind the hybrid tagger: prompt, request body and HTTP client.

The model is Qwen3-VL-4B Q4_K_M served by llama.cpp's ``llama-server`` (story-watch-llm.service,
bound to 127.0.0.1) through its OpenAI-compatible chat endpoint (#8, #10). The request is #10's:
the prompt in ``prompts/tag_v1.txt`` with the company lists filled in, the still image, the story's
context text, temperature 0, at most 320 output tokens and a JSON schema that constrains the reply.

The prompt and the context carry story text, so neither is ever logged, and no exception raised
here includes them (LlmError messages are fixed strings or an HTTP status).
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from importlib import resources
from typing import Any, Optional

import requests

from .companies import prompt_list
from .tag_rules import ALL

PROMPT_NAME = "tag_v1.txt"
MAX_TOKENS = 320

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


class LlmError(Exception):
    """The model gave no usable reply (HTTP error, bad shape). The message never holds story text."""


class LlmUnavailable(LlmError):
    """The server can't be reached (connection refused, reset, or not ready)."""


class LlmTimeout(LlmError):
    """No reply within the timeout."""


@lru_cache(maxsize=4)
def system_prompt(name: str = PROMPT_NAME) -> str:
    text = resources.files("story_watch").joinpath("prompts", name).read_text(encoding="utf-8")
    return text.replace("{faang_list}", prompt_list("faang_plus")).replace("{quant_list}", prompt_list("quant"))


def prompt_hash() -> str:
    return hashlib.sha256(system_prompt().encode()).hexdigest()[:8]


def _mime(image: bytes) -> str:
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def chat_body(image: bytes, context: str, model: str = "") -> dict:
    """#10's request: system prompt, then the image and the context text."""
    b64 = base64.b64encode(image).decode()
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{_mime(image)};base64,{b64}"}},
                {"type": "text", "text": "The story image is attached.\n" + context + "\nReturn the JSON."},
            ]},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_schema", "json_schema": {"name": "tags", "schema": SCHEMA}},
    }
    if model:
        body["model"] = model
    return body


class LlmClient:
    """HTTP client for llama-server. ``complete`` returns the reply text or raises an LlmError."""

    def __init__(self, url: str, model: str = "", session: Optional[requests.Session] = None,
                 health_timeout: float = 3.0):
        self.url = url.rstrip("/")
        self.model = model
        self._http = session or requests.Session()
        self._http.trust_env = False  # a local server: never through a proxy from the environment
        self._health_timeout = health_timeout

    def healthy(self) -> bool:
        """True when ``/health`` answers 200 (llama-server answers 503 while it loads the model)."""
        try:
            resp = self._http.get(f"{self.url}/health", timeout=self._health_timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def complete(self, body: dict, timeout: float) -> str:
        """POST a chat completion. llama-server sends nothing until the reply is complete, so the
        read timeout bounds the whole call."""
        try:
            resp = self._http.post(f"{self.url}/v1/chat/completions", json=body, timeout=(3.0, timeout))
        except requests.Timeout:
            raise LlmTimeout("model timed out") from None
        except requests.ConnectionError:
            raise LlmUnavailable("model unreachable") from None
        except requests.RequestException as e:
            raise LlmError(type(e).__name__) from None
        if resp.status_code == 503:
            raise LlmUnavailable("model not ready (HTTP 503)")
        if resp.status_code != 200:
            raise LlmError(f"model answered HTTP {resp.status_code}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError("reply has no message content") from None
        if not isinstance(content, str):
            raise LlmError("reply content is not text")
        return content
