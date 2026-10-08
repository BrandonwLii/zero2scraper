"""One-off: dump raw story JSON for a target so we can find link stickers and other text.

    .venv/bin/python scripts/dump_story.py IG_USER USERNAME[:USERID] [OUT_DIR]

Writes OUT_DIR/<mediaid>.graphql.json (instaloader's GraphQL reels_media node)
and OUT_DIR/page.html + OUT_DIR/page.items.json (the story items embedded in the
www.instagram.com/stories/<username>/ page, which carry story_link_stickers).
Two requests, plus a username lookup if no USERID is given. Fetching the HTML
runs no JavaScript, so it doesn't mark stories seen. Never writes the session
or cookies.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import instaloader
import requests

SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def walk(obj, found: list[dict]) -> None:
    """Collect every dict that looks like an api/v1 story item."""
    if isinstance(obj, dict):
        if "story_link_stickers" in obj or ("pk" in obj and "taken_at" in obj and "media_type" in obj):
            found.append(obj)
        for v in obj.values():
            walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, found)


def unwrap(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname == "l.instagram.com":
        return parse_qs(parts.query).get("u", [url])[0]
    return url


def main() -> None:
    ig_user = sys.argv[1]
    username, _, uid_s = sys.argv[2].lstrip("@").lower().partition(":")
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "story-dump")
    out.mkdir(parents=True, exist_ok=True)

    L = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
    L.load_session_from_file(ig_user)
    # A known userid skips web_profile_info, which is the endpoint that 429s.
    uid = int(uid_s) if uid_s else instaloader.Profile.from_username(L.context, username).userid

    n = 0
    for story in L.get_stories(userids=[uid]):
        # Story.get_items() also hits the iPhone reels_media endpoint and raises
        # KeyError when the reel is missing there, so read the GraphQL items directly.
        for node in story._node["items"]:
            (out / f"{node['id']}.graphql.json").write_text(json.dumps(node, indent=2, default=str))
            n += 1
    print(f"graphql: {n} item(s)")

    # Plain page load like a browser navigation: same cookies and user agent, but
    # none of instaloader's XHR headers, which make Instagram skip the page HTML.
    page = requests.Session()
    page.cookies.update(L.context._session.cookies)
    page.headers.update(
        {
            "User-Agent": L.context.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    try:
        # Instagram bounces the first load to ?r=1 (setting cookies), so follow redirects.
        resp = page.get(f"https://www.instagram.com/stories/{username}/", timeout=30)
    except requests.RequestException as e:
        print(f"page fetch failed: {type(e).__name__}")
        return
    for hop in resp.history:
        set_cookies = sorted(c.name for c in hop.cookies)
        print(f"  redirect: HTTP {hop.status_code} -> {hop.headers.get('Location', '')[:150]}  sets={set_cookies}")
    print(f"page: HTTP {resp.status_code}, {resp.url[:150]}, {resp.headers.get('Content-Type', '')}, {len(resp.content)} bytes")
    if any(p in urlsplit(resp.url).path for p in ("/accounts/login", "/challenge", "/auth_platform")):
        print("landed on a login/challenge page; the session isn't accepted for page loads")
        return
    (out / "page.html").write_text(resp.text)
    print("page mentions story_link_stickers:", "story_link_stickers" in resp.text)

    items: list[dict] = []
    blocks = SCRIPT_JSON.findall(resp.text)
    for block in blocks:
        try:
            walk(json.loads(block), items)
        except ValueError:
            continue
    print(f"page: {len(blocks)} JSON script block(s), {len(items)} story item(s)")
    (out / "page.items.json").write_text(json.dumps(items, indent=2, default=str))
    if items:
        print("first item keys:", sorted(items[0]))
    for it in items:
        links = [unwrap((s.get("story_link") or {}).get("url", "")) for s in it.get("story_link_stickers") or []]
        alt = (it.get("accessibility_caption") or "")[:100]
        print(it.get("pk") or it.get("id"), "links=" + repr(links) if links else "", f"alt={alt!r}" if alt else "")
    print(f"-> {out}/")


if __name__ == "__main__":
    main()
