"""Rate-limited HTTP session with retries + UA rotation.

Used by every scraper. Politeness defaults: 1 req every 2s, exponential
backoff on 429/5xx, conservative max retries. Saves raw response to disk
so a parser change doesn't require a re-fetch.
"""
from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
]


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def fetch(
    session: requests.Session,
    url: str,
    cache_dir: Path | None = None,
    min_delay_s: float = 2.0,
    timeout: int = 30,
) -> str:
    """GET url, return text. Cache to disk if cache_dir given.

    Sleeps min_delay_s before the request (politeness) and rotates UA.
    """
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        cached = cache_dir / f"{digest}.html"
        if cached.exists() and cached.stat().st_size > 1000:
            return cached.read_text()

    time.sleep(min_delay_s + random.uniform(0, 0.5))
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    text = r.text
    if cache_dir is not None:
        cached.write_text(text)
    return text
