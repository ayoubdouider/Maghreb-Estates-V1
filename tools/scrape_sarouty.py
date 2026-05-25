"""
Scraper for sarouty.ma — established Moroccan real estate aggregator.
Heavy JS rendering; uses Playwright networkidle wait.

Usage:
    python tools/scrape_sarouty.py                  # full auto-detected run
    python tools/scrape_sarouty.py --limit 20       # cap listings (for testing)
    python tools/scrape_sarouty.py --pages 3        # force max 3 pages (testing)

source_id format: sa-<original_id>  e.g. sa-55512
"""

import argparse
import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).parent))
from scraper_base import (
    random_delay, random_user_agent,
    parse_price, parse_surface, parse_int,
    save_results, detect_total_pages,
)

SOURCE = "sarouty"
SOURCE_PREFIX = "sa"
BASE_URL = "https://www.sarouty.ma"

SECTIONS = [
    ("sale", f"{BASE_URL}/fr/recherche?tr=Vente&page={{page}}"),
    ("rent", f"{BASE_URL}/fr/recherche?tr=Location&page={{page}}"),
]


def scrape_listing_page(page, url: str) -> dict | None:
    try:
        page.goto(url, wait_until="networkidle", timeout=40_000)
        random_delay(2.0, 4.0)
    except PWTimeout:
        print(f"  [warn] Timeout on {url}")
        return None

    def txt(selector: str) -> str:
        el = page.query_selector(selector)
        return el.inner_text().strip() if el else ""

    def txt_all(selector: str) -> list[str]:
        return [el.inner_text().strip() for el in page.query_selector_all(selector)]

    title_raw = txt("h1") or txt(".property-title")
    price_raw = txt(".price") or txt("[data-cy='price']") or txt(".listing-price")
    price = parse_price(price_raw)

    surface_m2 = bedrooms = bathrooms = None
    for row in page.query_selector_all(".property-feature, .stats-item, li.amenity"):
        text = row.inner_text().lower()
        if "m²" in text or "surface" in text:
            surface_m2 = parse_surface(text)
        elif "chambre" in text or "pièce" in text:
            bedrooms = parse_int(text)
        elif "bain" in text or "sdb" in text or "douche" in text:
            bathrooms = parse_int(text)

    crumbs = txt_all(".breadcrumb li") or txt_all("[class*='breadcrumb'] span")
    # Breadcrumb structure: [..., City, Neighborhood, Listing Title]
    # Last item is always the title — skip it; use [-2] for neighborhood, [-3] for city
    if len(crumbs) >= 3:
        neighborhood = crumbs[-2].strip()
        city = crumbs[-3].strip()
    elif len(crumbs) == 2:
        city = crumbs[-2].strip()
        neighborhood = ""
    elif len(crumbs) == 1:
        city = crumbs[-1].strip()
        neighborhood = ""
    else:
        city = txt("[class*='city']")
        neighborhood = txt("[class*='district'], [class*='quarter']")

    desc_raw = txt(".property-description, .description, .listing-description, [class*='description']")
    description = desc_raw[:1000] if desc_raw else ""

    combined = (desc_raw + " " + " ".join(txt_all(".amenity-label, .tag"))).lower()
    amenities = {
        "pool":      "piscine" in combined,
        "gym":       "salle de sport" in combined or "gym" in combined,
        "parking":   "parking" in combined or "garage" in combined,
        "elevator":  "ascenseur" in combined,
        "garden":    "jardin" in combined,
        "terrace":   "terrasse" in combined,
        "concierge": "gardien" in combined or "concierge" in combined,
        "security":  "sécurité" in combined or "gardienné" in combined,
    }

    floor_number: int | None = None
    total_floors: int | None = None
    for row in page.query_selector_all(".property-feature, .stats-item, li.amenity"):
        row_text = row.inner_text().lower()
        m_fl = re.search(r"[ée]tage[s]?\s*:?\s*(\d+)|(\d+)[eè]\s*[ée]tage", row_text)
        if m_fl and floor_number is None:
            floor_number = int(next(g for g in m_fl.groups() if g))
        m_tot = re.search(r"(\d+)\s*[ée]tages?\s+(?:au\s+total|sur\s+\d+|en\s+tout)", row_text)
        if m_tot and total_floors is None:
            total_floors = int(m_tot.group(1))

    image_urls: list[str] = page.eval_on_selector_all(
        ".gallery img, .property-images img, .slider img, [class*='photo'] img, [class*='gallery'] img",
        "els => [...new Set(els.map(e => e.getAttribute('src') || e.getAttribute('data-src')).filter(s => s && s.startsWith('http')))].slice(0,10)"
    )

    title_low = (title_raw or "").lower()
    prop_type = "apartment"
    if "villa" in title_low:                         prop_type = "villa"
    elif "maison" in title_low:                      prop_type = "house"
    elif "terrain" in title_low:                     prop_type = "land"
    elif "bureau" in title_low or "local" in title_low: prop_type = "commercial"
    elif "riad" in title_low:                        prop_type = "riad"

    return {
        "title":         title_raw,
        "price":         price,
        "surface_m2":    surface_m2,
        "bedrooms":      bedrooms,
        "bathrooms":     bathrooms,
        "city":          city,
        "neighborhood":  neighborhood,
        "property_type": prop_type,
        "amenities":     amenities,
        "description":   description,
        "floor_number":  floor_number,
        "total_floors":  total_floors,
        "image_urls":    image_urls,
    }


def extract_cards(page) -> list[tuple[str, str]]:
    cards = page.query_selector_all("a.property-card-link, a[href*='/fr/annonce/']")
    if not cards:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href')).filter(h => h && h.includes('/annonce/'))"
        )
        seen: set[str] = set()
        pairs = []
        for href in hrefs:
            full_url = href if href.startswith("http") else BASE_URL + href
            if full_url in seen:
                continue
            seen.add(full_url)
            m = re.search(r"/(\d+)(?:[/-]|$)", full_url)
            raw_id = m.group(1) if m else full_url.split("/")[-1]
            pairs.append((f"{SOURCE_PREFIX}-{raw_id}", full_url))
        return pairs

    pairs = []
    seen: set[str] = set()
    for card in cards:
        href = card.get_attribute("href") or ""
        full_url = href if href.startswith("http") else BASE_URL + href
        if full_url in seen:
            continue
        seen.add(full_url)
        m = re.search(r"/(\d+)(?:[/-]|$)", full_url)
        raw_id = m.group(1) if m else full_url.split("/")[-1]
        pairs.append((f"{SOURCE_PREFIX}-{raw_id}", full_url))
    return pairs


def scrape_listing_urls(page, section_url_tpl: str, max_pages: int | None) -> list[tuple[str, str]]:
    results = []

    first_url = section_url_tpl.format(page=1)
    try:
        page.goto(first_url, wait_until="networkidle", timeout=40_000)
    except PWTimeout:
        print(f"  [warn] Timeout on page 1, aborting section")
        return results

    random_delay(2.0, 4.5)

    # Sarouty URL pattern: ?page=N
    total = detect_total_pages(page, href_page_regex=r"[?&]page=(\d+)")
    if total is None:
        print("  [warn] Could not detect total pages — will stop when page returns no listings")
        total = 9999

    if max_pages is not None:
        total = min(total, max_pages)

    print(f"  Pages to scrape: {total}")

    pairs = extract_cards(page)
    results.extend(pairs)
    print(f"  Page 1: {len(pairs)} listings (total: {len(results)})")

    if not pairs:
        return results

    for p in range(2, total + 1):
        try:
            page.goto(section_url_tpl.format(page=p), wait_until="networkidle", timeout=40_000)
        except PWTimeout:
            print(f"  [warn] Timeout on page {p}, stopping")
            break

        random_delay(2.0, 4.5)
        pairs = extract_cards(page)
        if not pairs:
            print(f"  [info] Page {p} returned no listings — reached end")
            break

        results.extend(pairs)
        print(f"  Page {p}: {len(pairs)} listings (total: {len(results)})")

    return results


def run(max_pages: int | None = None, limit: int | None = None) -> list[dict]:
    listings = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=random_user_agent(),
            viewport={"width": 1440, "height": 900},
            locale="fr-FR",
        )
        page = context.new_page()
        detail_page = context.new_page()

        for transaction_type, section_tpl in SECTIONS:
            print(f"\n[{SOURCE}] Scraping {transaction_type} listings...")
            url_pairs = scrape_listing_urls(page, section_tpl, max_pages)

            if limit:
                url_pairs = url_pairs[:limit // len(SECTIONS)]

            for i, (source_id, url) in enumerate(url_pairs, 1):
                print(f"  [{i}/{len(url_pairs)}] {url}")
                detail = scrape_listing_page(detail_page, url)
                if detail:
                    listings.append({
                        "source":           SOURCE,
                        "source_id":        source_id,   # e.g. sa-55512
                        "url":              url,
                        "transaction_type": transaction_type,
                        **detail,
                        "price_currency":   "MAD",
                    })
                random_delay()

        browser.close()

    save_results(SOURCE, listings)
    return listings


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=None,
                        help="Force max pages per section (default: auto-detect)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap total listings for testing")
    args = parser.parse_args()
    run(max_pages=args.pages, limit=args.limit)
