"""
Scraper for mubawab.ma — uses requests + BeautifulSoup (no browser needed).
Card-only scraping: extracts all data from search result cards, skips detail pages.

Sections scraped:
  sale              — all resale listings
  long_term_rental  — monthly rentals
  short_term_rental — vacation/daily rentals
  new_construction  — developer promotions (project cards)

Usage:
    python tools/scrape_mubawab.py                            # full auto-detected run
    python tools/scrape_mubawab.py --limit 50                 # cap listings (for testing)
    python tools/scrape_mubawab.py --pages 3                  # max 3 pages per section
    python tools/scrape_mubawab.py --sections sale long_term_rental

Output: .tmp/mubawab_<date>.json  (WAT format, ready for push_to_supabase.py)
source_id format: mu-<id>  (listings) or mu-p-<id>  (new construction projects)
"""

import argparse
import math
import random
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from scraper_base import save_results

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCE        = "mubawab"
SOURCE_PREFIX = "mu"
BASE_URL      = "https://www.mubawab.ma"

# Each section: (transaction_type, url, price_period_default, cards_per_page)
# price_period_default = what to use when the card text has no explicit period indicator
SECTIONS = [
    ("sale",
     BASE_URL + "/en/cc/sale-all:sc:apartment-sale,commercial-sale,"
     "farm-sale,house-sale,land-sale,office-sale,other-sale,riad-sale,villa-sale",
     "total", 30),

    ("long_term_rental",
     BASE_URL + "/en/cc/rent-all:sc:apartment-rent,commercial-rent,"
     "farm-rent,house-rent,land-rent,office-rent,other-rent,riad-rent,"
     "room-rent,villa-rent",
     "month", 30),

    ("short_term_rental",
     BASE_URL + "/en/cc/vacational-all:sc:apartment-vacational,house-vacational,"
     "other-vacational,riad-vacational,room-vacational,villa-vacational",
     "day", 30),

    ("new_construction",
     BASE_URL + "/en/listing-promotion",
     "total", 21),  # promotion page shows 21 cards per page
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

BASE_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL + "/",
}

CITY_ALIASES = {
    "casablanca": "Casablanca", "casa": "Casablanca", "dar el beida": "Casablanca",
    "marrakech": "Marrakech", "marrakesh": "Marrakech",
    "rabat": "Rabat",
    "salé": "Salé", "sale": "Salé",
    "tanger": "Tanger", "tangier": "Tanger", "tanja": "Tanger",
    "agadir": "Agadir",
    "fès": "Fès", "fes": "Fès",
    "meknès": "Meknès", "meknes": "Meknès",
    "oujda": "Oujda",
    "kénitra": "Kénitra", "kenitra": "Kénitra",
    "tétouan": "Tétouan", "tetouan": "Tétouan",
    "el jadida": "El Jadida",
    "essaouira": "Essaouira",
    "mohammedia": "Mohammedia",
    "temara": "Temara",
    "bouznika": "Bouznika",
    "benslimane": "Benslimane",
    "ifrane": "Ifrane",
    "ouarzazate": "Ouarzazate",
    "dakhla": "Dakhla",
    "laayoune": "Laâyoune",
    "nador": "Nador",
    "settat": "Settat",
    "berrechid": "Berrechid",
}

# (amenity_key, list_of_keywords_to_match)
AMENITIES_MAP = [
    ("pool",            ["pool", "piscine"]),
    ("garden",          ["garden", "jardin"]),
    ("terrace",         ["terrace", "terrasse"]),
    ("parking",         ["parking", "garage"]),
    ("elevator",        ["elevator", "ascenseur", "lift"]),
    ("concierge",       ["concierge", "gardien"]),
    ("security",        ["security", "sécurité", "gardienné"]),
    ("air_conditioning",["air conditioning", "climatisation", "climatisé"]),
    ("furnished",       ["furnished", "meublé"]),
    ("fireplace",       ["fireplace", "cheminée"]),
    ("gym",             ["gym", "salle de sport"]),
]


# ── HTTP ──────────────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 3) -> BeautifulSoup | None:
    headers = {**BASE_HEADERS, "User-Agent": random.choice(USER_AGENTS)}
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                return BeautifulSoup(resp.text, "html.parser")
            print(f"  [warn] HTTP {resp.status_code} → {url}")
        except Exception as exc:
            print(f"  [warn] Request error ({exc}), attempt {attempt + 1}/{retries}")
        time.sleep(3)
    return None


# ── Page detection ────────────────────────────────────────────────────────────

def detect_total_pages(soup: BeautifulSoup, cards_per_page: int) -> int | None:
    """
    Detect total pages from the listing count displayed on the search results page.
    Strategy 1: parse total count from page text → calculate pages.
    Strategy 2: fallback to max :p:N found in pagination hrefs (visible window only).
    """
    text = soup.get_text(" ")

    # Strategy 1: find all "N properties/listings/results" in page text, take the maximum
    # (the page may also show a smaller per-page count — we want the overall total)
    counts: list[int] = []
    for m in re.finditer(
        r"([\d][\d\s,]*)\s+(?:properties|listings|annonces|résultats|results)",
        text, re.I
    ):
        count_str = re.sub(r"[^\d]", "", m.group(1))
        if count_str:
            counts.append(int(count_str))
    if counts:
        total = max(counts)
        pages = math.ceil(total / cards_per_page)
        print(f"  [pages] {total:,} listings → {pages:,} pages")
        return pages

    # Strategy 2: max :p:N in pagination hrefs (only shows visible window)
    max_p = max(
        (int(m.group(1)) for a in soup.find_all("a", href=True)
         if (m := re.search(r":p:(\d+)", a["href"]))),
        default=0,
    )
    if max_p:
        print(f"  [pages] Detected {max_p} pages from pagination links (may be partial)")
        return max_p

    return None


def detect_price_period(text: str, section_default: str) -> str:
    """
    Detect how a price is quoted: per day, week, month, or total (one-off purchase).
    Falls back to the section default when no explicit indicator is found.

    Examples:
      '1,700 DH per day'  → 'day'
      '3,500 DH / mois'   → 'month'
      '690,000 DH'        → section_default (e.g. 'total' for sale)
    """
    t = text.lower()
    if any(w in t for w in ["per night", "par nuit", "/night", "/nuit", "nightly"]):
        return "day"  # night rate = daily rate for analysis
    if any(w in t for w in ["per day", "par jour", "/day", "/jour",
                             "à la journée", "per diem"]):
        return "day"
    if any(w in t for w in ["per week", "par semaine", "/week", "/semaine",
                             "à la semaine", "weekly"]):
        return "week"
    if any(w in t for w in ["per month", "par mois", "/month", "/mois",
                             "mensuel", "monthly"]):
        return "month"
    return section_default


# ── Extraction helpers ────────────────────────────────────────────────────────

def normalize_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


def extract_amenities(text: str) -> dict:
    t = text.lower()
    return {key: any(kw in t for kw in kws) for key, kws in AMENITIES_MAP}


def infer_property_type(text: str) -> str:
    t = text.lower()
    if "villa"    in t: return "villa"
    if "riad"     in t: return "riad"
    if "maison"   in t or "house" in t: return "house"
    if "terrain"  in t or "land"  in t: return "land"
    if "bureau"   in t or "local" in t or "commercial" in t: return "commercial"
    return "apartment"


def infer_currency(price_text: str) -> str:
    if "eur" in price_text.lower() or "€" in price_text:
        return "EUR"
    return "MAD"


# ── Card parser ───────────────────────────────────────────────────────────────

def parse_card(
    card: BeautifulSoup,
    transaction_type: str,
    price_period_default: str,
) -> dict | None:
    """
    Extract all fields from a single search result card.

    Handles two card types:
    - Regular listings: ID in <input class="adId">, URL in /en/a/ or /en/pa/ link
    - New-construction project cards: ID in promotion-id attribute, URL in linkref attribute
    """
    text = card.get_text(" ", strip=True)

    # ── Source ID & URL ───────────────────────────────────────────────────────

    # New-construction project card: <li promotion-id="4161" linkref="...">
    promo_id = card.get("promotion-id")
    if promo_id:
        source_id = f"{SOURCE_PREFIX}-p-{promo_id}"
        linkref = card.get("linkref", "")
        url = linkref if linkref.startswith("http") else BASE_URL + linkref
    else:
        # Regular listing card: <input class="adId" value="8163854">
        id_input = card.find("input", class_="adId")
        raw_id = id_input["value"] if id_input else None

        if not raw_id:
            a = card.find("a", href=re.compile(r"/en/(?:a|pa)/\d+"))
            if a:
                m = re.search(r"/en/(?:a|pa)/(\d+)", a["href"])
                raw_id = m.group(1) if m else None

        if not raw_id:
            return None

        source_id = f"{SOURCE_PREFIX}-{raw_id}"
        link_el = card.find("a", href=re.compile(r"/en/(?:a|pa)/\d+"))
        if link_el:
            href = link_el["href"]
            url = href if href.startswith("http") else BASE_URL + href
        else:
            url = f"{BASE_URL}/en/pa/{raw_id}"

    # ── Title ─────────────────────────────────────────────────────────────────

    title_el = card.find("h2") or card.find("h3")
    title = title_el.get_text(strip=True) if title_el else ""

    # ── Price & price_period ──────────────────────────────────────────────────

    price: float | None = None
    price_currency = "MAD"
    price_el = card.find(class_=re.compile(r"price|prix", re.I))
    price_text = price_el.get_text(" ", strip=True) if price_el else ""

    if not price_text:
        m = re.search(r"([\d][\d\s,\.]*)\s*(DH|MAD|EUR|€)", text, re.I)
        if m:
            price_text = m.group(0)

    if price_text:
        price_currency = infer_currency(price_text)
        nums = re.sub(r"[^\d]", "", price_text)
        if nums and len(nums) < 12:
            price = float(nums)

    price_period = detect_price_period(text, price_period_default)

    # ── Surface m² ────────────────────────────────────────────────────────────

    surface_m2: float | None = None
    m = re.search(r"(\d+)\s*m²", text, re.I)
    if m:
        surface_m2 = float(m.group(1))

    # ── Rooms ─────────────────────────────────────────────────────────────────

    bedrooms: int | None = None
    m = re.search(r"(\d+)\s*(?:Rooms?|Bedrooms?|Chambres?|Pièces?)", text, re.I)
    if m:
        bedrooms = int(m.group(1))

    bathrooms: int | None = None
    m = re.search(r"(\d+)\s*Bathrooms?", text, re.I)
    if m:
        bathrooms = int(m.group(1))

    # ── City & Neighborhood ───────────────────────────────────────────────────

    city = ""
    neighborhood = ""

    # Layer 1: location link — pin icon links to /en/sc/<city>-<neighborhood>:...
    # The link text contains "Neighborhood, City" (e.g. "Agdal, Marrakech")
    # Only use if the last part resolves to a known city (avoids matching price/category links)
    for a in card.find_all("a", href=re.compile(r"/en/(?:sc|ct)/[a-z]", re.I)):
        loc_text = re.sub(r"[^\w\s,À-ÿ\-']", "", a.get_text(strip=True)).strip()
        parts = [p.strip() for p in loc_text.split(",") if p.strip()]
        if len(parts) >= 2:
            candidate_city = normalize_city(parts[-1])
            if candidate_city:
                neighborhood = parts[0]
                city = candidate_city
                break
        elif len(parts) == 1:
            candidate_city = normalize_city(parts[0])
            if candidate_city:
                city = candidate_city
                break

    # Layer 2: CSS selector (conservative — no generic class names like tag/label)
    if not city:
        loc_el = card.find(class_=re.compile(
            r"location|localisation|city|adress|lieu|quartier|ville|where|pin", re.I
        ))
        if loc_el:
            parts = [p.strip() for p in loc_el.get_text(" ", strip=True).split(",") if p.strip()]
            if len(parts) >= 2:
                candidate_city = normalize_city(parts[-1])
                if candidate_city:
                    neighborhood = parts[0]
                    city = candidate_city
            elif parts:
                city = normalize_city(parts[0])

    # Layer 3: scan full card text for "Neighborhood, KnownCity"
    # Limit neighborhood to max 3 words to avoid matching description sentences
    if not city:
        city_pat = "|".join(
            re.escape(c) for c in sorted(set(CITY_ALIASES.values()), key=len, reverse=True)
        )
        m_loc = re.search(
            rf"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-]+(?:\s[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\-]+){{0,2}}),\s*({city_pat})\b",
            text,
        )
        if m_loc and m_loc.group(1)[0].isupper():
            neighborhood = m_loc.group(1).strip()
            city = normalize_city(m_loc.group(2))

    if not city:
        for alias, normalized in CITY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text, re.I):
                city = normalized
                break

    # ── Property type & amenities ─────────────────────────────────────────────

    property_type = infer_property_type(title or text)
    amenities = extract_amenities(text)

    # ── Description ───────────────────────────────────────────────────────────

    description = ""
    desc_el = card.find(class_=re.compile(r"desc|description|summary|excerpt", re.I))
    if desc_el:
        description = desc_el.get_text(" ", strip=True)[:800]

    # ── Floor number ──────────────────────────────────────────────────────────

    floor_number: int | None = None
    m_fl = re.search(
        r"[ÉéEe]tage[s]?\s*:?\s*(\d+)|(\d+)[eèème]{1,3}\s*[ÉéEe]tage|[Ff]loor\s*(\d+)",
        text, re.I
    )
    if m_fl:
        floor_number = int(next(g for g in m_fl.groups() if g))

    # ── Image URLs ────────────────────────────────────────────────────────────

    image_urls: list[str] = []
    for img in card.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")
        if src and src.startswith("http") and "placeholder" not in src.lower():
            image_urls.append(src)
            if len(image_urls) >= 5:
                break

    return {
        "source":           SOURCE,
        "source_id":        source_id,
        "url":              url,
        "transaction_type": transaction_type,
        "price_period":     price_period,
        "property_type":    property_type,
        "price":            price,
        "price_currency":   price_currency,
        "surface_m2":       surface_m2,
        "bedrooms":         bedrooms,
        "bathrooms":        bathrooms,
        "city":             city,
        "neighborhood":     neighborhood,
        "amenities":        amenities,
        "title":            title,
        "description":      description,
        "floor_number":     floor_number,
        "total_floors":     None,
        "image_urls":       image_urls,
    }


# ── Section scraper ───────────────────────────────────────────────────────────

def scrape_section(
    transaction_type: str,
    base_url: str,
    price_period_default: str,
    cards_per_page: int,
    max_pages: int | None,
    limit: int | None,
) -> list[dict]:
    print(f"\n[{SOURCE}] {transaction_type.upper()} → {base_url[:70]}")

    results: list[dict] = []
    seen_ids: set[str] = set()

    def add_cards(soup: BeautifulSoup) -> int:
        added = 0
        for card in soup.find_all(class_=re.compile(r"\blistingBox\b")):
            listing = parse_card(card, transaction_type, price_period_default)
            if listing and listing["source_id"] not in seen_ids:
                seen_ids.add(listing["source_id"])
                results.append(listing)
                added += 1
                if limit and len(results) >= limit:
                    return added
        return added

    # Page 1 — also detect total pages
    soup = fetch(base_url)
    if not soup:
        print(f"  [error] Could not load page 1 — skipping {transaction_type}")
        return results

    total_pages = detect_total_pages(soup, cards_per_page)
    if total_pages is None:
        print("  [warn] Could not detect total pages — will stop on empty page")
        total_pages = 9_999

    if max_pages:
        total_pages = min(total_pages, max_pages)

    n = add_cards(soup)
    print(f"  Page 1: {n} listings")
    if limit and len(results) >= limit:
        return results

    time.sleep(random.uniform(1.5, 3.0))

    # Pages 2 → end  (Mubawab pagination: append :p:N to the base URL)
    for page_num in range(2, total_pages + 1):
        soup = fetch(f"{base_url}:p:{page_num}")
        if not soup:
            print(f"  [warn] Failed to load page {page_num} — stopping section")
            break

        n = add_cards(soup)
        if n == 0 and not soup.find(class_=re.compile(r"\blistingBox\b")):
            print(f"  Page {page_num}: no cards — reached end")
            break

        print(f"  Page {page_num}: {n} new listings (total: {len(results)})")

        if limit and len(results) >= limit:
            print(f"  [limit] Capped at {limit}")
            break

        time.sleep(random.uniform(1.5, 3.0))

    return results


# ── Entry points ──────────────────────────────────────────────────────────────

SECTION_NAMES = [tt for tt, *_ in SECTIONS]


def run(
    max_pages: int | None = None,
    limit: int | None = None,
    sections: list[str] | None = None,
) -> list[dict]:
    active = [s for s in SECTIONS if sections is None or s[0] in sections]
    all_listings: list[dict] = []

    for transaction_type, base_url, price_period_default, cards_per_page in active:
        listings = scrape_section(
            transaction_type, base_url,
            price_period_default, cards_per_page,
            max_pages, limit,
        )
        all_listings.extend(listings)
        print(f"[{SOURCE}] {transaction_type}: {len(listings)} listings scraped\n")

    save_results(SOURCE, all_listings)
    return all_listings


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape mubawab.ma real estate listings")
    ap.add_argument("--pages",    type=int,  default=None,
                    help="Max pages per section (default: auto-detect all)")
    ap.add_argument("--limit",    type=int,  default=None,
                    help="Cap total listings for testing (e.g. --limit 50)")
    ap.add_argument("--sections", nargs="+", default=None,
                    choices=SECTION_NAMES,
                    help="Sections to scrape (default: all four)")
    args = ap.parse_args()
    run(max_pages=args.pages, limit=args.limit, sections=args.sections)
