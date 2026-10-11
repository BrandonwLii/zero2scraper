"""The company lists live in story_watch/companies.py; docs/tags.md shows them. They must not drift."""

import re
from pathlib import Path

from story_watch.companies import FAANG_PLUS, QUANT, prompt_list

DOC = Path(__file__).resolve().parents[1] / "docs" / "tags.md"


def doc_table(heading):
    """[(company, (aliases...))] from a table in docs/tags.md; parenthesised notes are ignored."""
    section = DOC.read_text(encoding="utf-8").split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 2 or cells[0] in ("Company", "") or set(cells[0]) <= set("-"):
            continue
        aliases = tuple(a.strip() for a in re.split(r",\s*", re.sub(r"\(.*?\)", "", cells[1])) if a.strip())
        rows.append((cells[0], aliases))
    return rows


def test_doc_tables_match_the_code():
    assert doc_table("### FAANG+ list") == list(FAANG_PLUS)
    assert doc_table("### Quant list") == list(QUANT)


def test_prompt_lists():
    assert prompt_list("faang_plus").startswith("Meta (Facebook, Instagram, WhatsApp, Reality Labs); Apple; ")
    assert "Google (Alphabet, Google DeepMind, DeepMind, YouTube, Wing); " in prompt_list("faang_plus")
    assert prompt_list("quant").endswith("Schonfeld (Schonfeld Strategic Advisors); Marshall Wace.")


def test_markdown_tables_are_the_doc_tables_verbatim():
    """The Claude prompt embeds markdown_table(); docs/tags.md must show the very same tables."""
    from story_watch.companies import markdown_table

    doc = DOC.read_text(encoding="utf-8")
    for group in ("faang_plus", "quant"):
        assert "\n" + markdown_table(group) + "\n" in doc
