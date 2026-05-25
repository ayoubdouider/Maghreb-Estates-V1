"""Shared utilities for all Moroccan real estate scrapers."""

import json
import random
import re
import time
from datetime import date
from pathlib import Path

TMP_DIR = Path(__file__).parent.parent / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

# Short prefix used to namespace source_ids so mu-1234 ≠ av-1234
SOURCE_PREFIXES = {
    "mubawab": "mu",
    "avito":   "av",
    "sarouty": "sa",
    "agenz":   "ag",
}

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def random_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def parse_price(text: str) -> float | None:
    """Extract numeric price from strings like '1 200 000 MAD' or '8 500 DH/mois'."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    return float(cleaned) if cleaned else None


def parse_surface(text: str) -> float | None:
    """Extract m² value from strings like '120 m²' or '85m2'."""
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", text.replace("\xa0", ""))
    return float(match.group(1).replace(",", ".")) if match else None


def parse_int(text: str) -> int | None:
    """Extract first integer from a string."""
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def detect_total_pages(page, href_page_regex: str | None = None) -> int | None:
    """
    Detect the total number of result pages on the currently loaded search page.

    Strategy (in order):
    1. Parse page numbers out of pagination link hrefs using href_page_regex
       (e.g. r':p:(\d+)' for Mubawab, r'[?&]page=(\d+)' for Sarouty/Agenz)
    2. Read numeric text from pagination link labels (most universal)
    3. Look for French text patterns like "sur 45 pages" or "/ 30"

    Returns None when detection fails — callers should fall back to the
    "stop when page returns no listings" guard.
    """
    nums: list[int] = []

    # Strategy 1 — parse page numbers from href attributes
    if href_page_regex:
        hrefs: list[str] = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
        for href in hrefs:
            m = re.search(href_page_regex, href)
            if m:
                nums.append(int(m.group(1)))

    if nums:
        return max(nums)

    # Strategy 2 — find the maximum integer in pagination link text
    pagination_selectors = [
        "[class*='pagination'] a",
        "[class*='pager'] a",
        "nav a",
        ".pages a",
        "[class*='page-link']",
    ]
    for sel in pagination_selectors:
        links = page.query_selector_all(sel)
        for link in links:
            txt = (link.inner_text() or "").strip()
            if re.fullmatch(r"\d+", txt):
                nums.append(int(txt))
        if nums:
            return max(nums)

    # Strategy 3 — text patterns in the visible page content
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    for pattern in [
        r"sur\s+(\d+)\s*pages?",
        r"/\s*(\d+)\s*pages?",
        r"(\d+)\s+pages?\s+(?:au total|disponibles?)",
        r"page\s+\d+\s+(?:sur|of|/)\s+(\d+)",
    ]:
        m = re.search(pattern, body_text, re.IGNORECASE)
        if m:
            return int(m.group(1))

    return None


def save_results(source: str, listings: list[dict]) -> Path:
    """Save scraped listings to .tmp/<source>_<date>.json and return the path."""
    today = date.today().isoformat()
    path = TMP_DIR / f"{source}_{today}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False, indent=2)
    print(f"[{source}] Saved {len(listings)} listings → {path}")
    return path
