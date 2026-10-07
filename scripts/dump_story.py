"""One-off: dump raw story item JSON for a target so we can find link-sticker fields.

    .venv/bin/python scripts/dump_story.py IG_USER TARGET|USERID [OUT_DIR]

Writes OUT_DIR/<mediaid>.graphql.json (the reels_media node; no extra requests)
and, for the first item only, OUT_DIR/<mediaid>.iphone.json (one extra request).
Never writes the session or cookies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import instaloader


def main() -> None:
    ig_user, target = sys.argv[1], sys.argv[2].lstrip("@").lower()
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "story-dump")
    out.mkdir(parents=True, exist_ok=True)

    L = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
    L.load_session_from_file(ig_user)
    # A numeric target skips web_profile_info, which is the endpoint that 429s.
    uid = int(target) if target.isdigit() else instaloader.Profile.from_username(L.context, target).userid

    n = 0
    for story in L.get_stories(userids=[uid]):
        # Story.get_items() also hits the iPhone reels_media endpoint and raises
        # KeyError when the reel is missing there, so read the GraphQL items directly.
        for node in story._node["items"]:
            (out / f"{node['id']}.graphql.json").write_text(json.dumps(node, indent=2, default=str))
            hits = [k for k in json.dumps(node).split('"') if "link" in k.lower() or "cta" in k.lower()]
            print(node["id"], node.get("taken_at_timestamp"), node.get("__typename"), sorted(set(hits)))
            n += 1
        try:
            data = L.context.get_iphone_json(f"api/v1/feed/reels_media/?reel_ids={uid}", params={})
            (out / "reel.iphone.json").write_text(json.dumps(data, indent=2, default=str))
            print("iphone reels_media keys:", sorted(data), "reels:", sorted(data.get("reels", {})))
        except Exception as e:  # noqa: BLE001 - diagnostic script
            print(f"iphone reels_media failed: {type(e).__name__}")
    print(f"{n} item(s) -> {out}/")


if __name__ == "__main__":
    main()
