"""Guarded HTTP fetch for the job-page prototypes, mirroring story_watch/jobs.py.

Same guards as the service: a fresh requests session (never Instagram cookies),
https only, a public-address check before every hop (redirects are followed by
hand), and a size cap. Two additions the service doesn't have yet:

- the response's peer address is checked too. This only DETECTS, best effort, a
  connection to a private address after urllib3's own getaddrinfo() (DNS rebinding):
  the request has already been sent by then, and the check is skipped when urllib3
  doesn't expose the socket. The real fix (for #12) is to connect to the IP that
  check_public() already approved;
- an optional JSON body, for the ATS APIs.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5


class Blocked(ValueError):
    pass


def check_public(url: str, resolve: Callable[..., list] = socket.getaddrinfo) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise Blocked("not an https URL")
    for info in resolve(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise Blocked("resolves to a non-public address")


def _peer_ip(resp: requests.Response) -> str | None:
    """Address urllib3 actually connected to (best effort; None if not exposed)."""
    try:
        sock = resp.raw._connection.sock  # type: ignore[attr-defined]
        return sock.getpeername()[0]
    except Exception:
        return None


class Fetcher:
    def __init__(self, timeout: float = 15, resolve: Callable[..., list] = socket.getaddrinfo):
        self.http = requests.Session()  # fresh: no cookies from anywhere else
        # Sec-Fetch-* are what any browser sends on navigation; one big-tech careers site answers 400 without them.
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.5",
                                  "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none"})
        self.timeout = timeout
        self.resolve = resolve
        self.requests = 0

    def get(self, url: str, accept: str = "text/html,*/*;q=0.8", data: Any = None) -> tuple[int, str, str]:
        """(status, final url, body text). Raises Blocked or requests errors."""
        for _ in range(MAX_REDIRECTS + 1):
            check_public(url, self.resolve)
            self.requests += 1
            headers = {"Accept": accept}
            if data is None:
                resp = self.http.get(url, timeout=self.timeout, allow_redirects=False, stream=True, headers=headers)
            else:
                resp = self.http.post(url, timeout=self.timeout, allow_redirects=False, stream=True,
                                      headers=headers, json=data)
            try:
                # Detection only, best effort: the request is already sent, and None (socket not exposed) skips it.
                peer = _peer_ip(resp)
                if peer is not None and not ipaddress.ip_address(peer).is_global:
                    raise Blocked("connected to a non-public address")
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("Location", ""))
                    continue
                body = b""
                for chunk in resp.iter_content(65536):
                    body += chunk
                    if len(body) >= MAX_BYTES:
                        break
                return resp.status_code, url, body.decode(resp.encoding or "utf-8", "replace")
            finally:
                resp.close()
        raise Blocked("too many redirects")

    def get_json(self, url: str, data: Any = None) -> tuple[int, Any]:
        status, _, body = self.get(url, accept="application/json", data=data)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None
