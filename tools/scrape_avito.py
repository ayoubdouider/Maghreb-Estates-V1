"""
Scraper for avito.ma — uses requests + BeautifulSoup (no browser needed).
Card-only scraping: each listing card is wrapped in one <a> tag that holds all data.

Sections scraped:
  mixed             — all resale + long-term rental (avito.ma/fr/maroc/immobilier)
  short_term_rental — vacation/daily rentals        (avito.ma/fr/maroc/locations_de_vacances)
  new_construction  — new developer projects        (avito.ma/fr/maroc/immobilier_neuf)

Usage:
    python tools/scrape_avito.py                             # full auto-detected run
    python tools/scrape_avito.py --limit 50                  # cap listings (for testing)
    python tools/scrape_avito.py --pages 3                   # force max 3 pages per section
    python tools/scrape_avito.py --sections mixed new_construction

Output: .tmp/avito_<date>.json  (WAT format, ready for push_to_supabase.py)
source_id format: av-<id>   e.g. av-57581858

Pagination: ?o=N  (page number, not offset)
  Page 1: <base_url>       Page N: <base_url>?o=N
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

SOURCE         = "avito"
SOURCE_PREFIX  = "av"
BASE_URL       = "https://www.avito.ma"
CARDS_PER_PAGE = 38  # measured: Avito shows ~38 listing cards per page

# Each section: (section_key, url, price_period_default)
# 'mixed' = sale + long_term_rental combined — transaction_type detected per card from text
SECTIONS = [
    ("mixed",             BASE_URL + "/fr/maroc/immobilier",           "total"),
    ("short_term_rental", BASE_URL + "/fr/maroc/locations_de_vacances", "day"),
    ("new_construction",  BASE_URL + "/fr/maroc/immobilier_neuf",       "total"),
]

SECTION_NAMES = [s[0] for s in SECTIONS]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

BASE_HEADERS = {
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL + "/",
}

CITY_ALIASES = {
    "casablanca": "Casablanca", "casa": "Casablanca", "dar el beida": "Casablanca",
    "marrakech": "Marrakech", "marrakesh": "Marrakech",
    "rabat": "Rabat",
    "salé": "Salé", "sale": "Salé",
    "tanger": "Tanger", "tangier": "Tanger",
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
    "ifrane": "Ifrane",
    "ouarzazate": "Ouarzazate",
    "dakhla": "Dakhla",
    "laâyoune": "Laâyoune", "laayoune": "Laâyoune",
    "nador": "Nador",
    "settat": "Settat",
    "berrechid": "Berrechid",
    "benslimane": "Benslimane",
}

# URL category slug → property type
CATEGORY_MAP = {
    "appartements":           "apartment",
    "studios":                "apartment",
    "villas_et_riads":        "villa",
    "villas":                 "villa",
    "riads":                  "riad",
    "maisons":                "house",
    "terrains":               "land",
    "locaux_commerciaux":     "commercial",
    "bureaux":                "commercial",
    "local":                  "commercial",
    "autre_immobilier":       "other",
    "colocations":            "apartment",
    "residences_etudiantes":  "apartment",
}

AMENITIES_MAP = [
    ("pool",            ["piscine", "pool"]),
    ("garden",          ["jardin", "garden"]),
    ("terrace",         ["terrasse", "terrace"]),
    ("parking",         ["parking", "garage"]),
    ("elevator",        ["ascenseur", "elevator", "lift"]),
    ("concierge",       ["concierge", "gardien"]),
    ("security",        ["sécurité", "gardienné", "security"]),
    ("air_conditioning",["climatisation", "climatisé", "air conditionné"]),
    ("furnished",       ["meublé", "furnished"]),
    ("fireplace",       ["cheminée", "fireplace"]),
    ("gym",             ["salle de sport", "gym"]),
]


def detect_price_period(text: str, section_default: str) -> str:
    """Detect price period from card text; fall back to section default."""
    t = text.lower()
    if any(w in t for w in ["par nuit", "/nuit", "per night", "nightly"]):
        return "day"
    if any(w in t for w in ["par jour", "/jour", "per day", "à la journée"]):
        return "day"
    if any(w in t for w in ["par semaine", "/semaine", "per week", "à la semaine"]):
        return "week"
    if any(w in t for w in ["par mois", "/mois", "per month", "mensuel", "monthly"]):
        return "month"
    return section_default


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

def detect_total_pages(soup: BeautifulSoup) -> int | None:
    """
    Detect total pages from "N annonces" in the page text.
    Avito shows e.g. "(241844) annonces" or "241 844 annonces".
    """
    text = soup.get_text(" ")

    counts: list[int] = []
    for m in re.finditer(
        r"([\d][\d\s,]*)\s+(?:annonces?|résultats?|offres?|biens?)",
        text, re.I
    ):
        cleaned = re.sub(r"[^\d]", "", m.group(1))
        if cleaned:
            counts.append(int(cleaned))

    if counts:
        total = max(counts)
        pages = math.ceil(total / CARDS_PER_PAGE)
        print(f"  [pages] {total:,} listings → {pages:,} pages")
        return pages

    # Fallback: max ?o=N from pagination links (visible window only)
    max_o = max(
        (int(m.group(1)) for a in soup.find_all("a", href=True)
         if (m := re.search(r"[?&]o=(\d+)", a["href"]))),
        default=0,
    )
    if max_o:
        print(f"  [pages] Detected page {max_o} from pagination (may be partial)")
        return max_o

    return None


# ── Extraction helpers ────────────────────────────────────────────────────────

def normalize_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


def slug_to_label(slug: str) -> str:
    """Convert URL slug to readable label: 'route_de_tahanaoute' → 'Route de Tahanaoute'."""
    return slug.replace("_", " ").strip().title()


def infer_transaction_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["à louer", "location", "à la journée", "à la semaine", "louer"]):
        return "long_term_rental"
    if any(w in t for w in ["à vendre", "vente", "vendre", "acheter", "achat"]):
        return "sale"
    return "sale"  # default for Avito immobilier (majority are sales)


def infer_property_type(category_slug: str, title: str) -> str:
    if category_slug in CATEGORY_MAP:
        return CATEGORY_MAP[category_slug]
    t = title.lower()
    if "villa"    in t: return "villa"
    if "riad"     in t: return "riad"
    if "maison"   in t: return "house"
    if "terrain"  in t: return "land"
    if "bureau"   in t or "local" in t: return "commercial"
    return "apartment"


def extract_amenities(text: str) -> dict:
    t = text.lower()
    return {key: any(kw in t for kw in kws) for key, kws in AMENITIES_MAP}


def parse_price(text: str) -> float | None:
    """
    Extract price from Avito card text.

    Uses grouped-digit pattern (1-3 digits then groups of 3) to avoid
    grabbing floor numbers like "Etage 8" before the actual price.
    Returns the largest valid price found (main price, not monthly equivalent).
    """
    candidates = re.findall(
        r"(\d{1,3}(?:\s\d{3})*)\s*(?:DH|MAD)",
        text,
    )
    nums = [int(re.sub(r"\s", "", p)) for p in candidates if len(re.sub(r"\s", "", p)) >= 3]
    return float(max(nums)) if nums else None


# ── Card parser ───────────────────────────────────────────────────────────────

def parse_card(
    anchor: BeautifulSoup,
    section_key: str,
    price_period_default: str,
) -> dict | None:
    """
    Extract all fields from one Avito listing anchor tag.
    URL format: /fr/<neighborhood_slug>/<category_slug>/<title>_<id>.htm
    """
    href = anchor.get("href", "")
    m = re.search(r"/fr/([^/]+)/([^/]+)/[^/]+_(\d+)\.htm$", href)
    if not m:
        return None

    neighborhood_slug, category_slug, raw_id = m.groups()
    source_id = f"{SOURCE_PREFIX}-{raw_id}"
    url = href if href.startswith("http") else BASE_URL + href

    text = anchor.get_text(" ", strip=True)

    # Title — from image alt or text after "dans City, Neighborhood"
    title = ""
    m_loc = re.search(r"dans\s+[^,]+,\s+[^\n]+?([A-Z][^\d\n]{5,}?)(?:\d|\s*$)", text)
    if m_loc:
        title = m_loc.group(1).strip()
    if not title:
        img = anchor.find("img", alt=True)
        if img and img.get("alt"):
            title = img["alt"].strip()

    # City & Neighborhood
    # Neighborhood: always from URL slug (reliable, clean)
    # City: from "dans <City>" pattern in card text, fallback to alias scan
    neighborhood = slug_to_label(neighborhood_slug)
    city = ""
    m_loc = re.search(r"dans\s+([^,\n]+?)(?:,|\.|\s{2,}|\d|$)", text)
    if m_loc:
        city = normalize_city(m_loc.group(1).strip())

    if not city:
        for alias, normalized in CITY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text, re.I):
                city = normalized
                break

    # Price & price_period
    price = parse_price(text)
    price_currency = "MAD"
    if "€" in text or "EUR" in text.upper():
        price_currency = "EUR"
    price_period = detect_price_period(text, price_period_default)

    # Surface m²
    surface_m2: float | None = None
    m = re.search(r"(\d+)\s*m²", text)
    if m:
        surface_m2 = float(m.group(1))

    # Bedrooms
    bedrooms: int | None = None
    m = re.search(r"(\d+)\s*(?:pièces?|chambres?)", text, re.I)
    if m:
        bedrooms = int(m.group(1))

    # Bathrooms
    bathrooms: int | None = None
    m = re.search(r"(\d+)\s*(?:sdbs?|salles?\s+de\s+bain)", text, re.I)
    if m:
        bathrooms = int(m.group(1))

    # Transaction type:
    # - new_construction section → always "new_construction"
    # - short_term_rental section → always "short_term_rental"
    # - mixed section → detect from card text (sale vs long_term_rental)
    if section_key == "new_construction":
        transaction_type = "new_construction"
    elif section_key == "short_term_rental":
        transaction_type = "short_term_rental"
    else:
        transaction_type = infer_transaction_type(text)

    property_type = infer_property_type(category_slug, title)
    amenities = extract_amenities(text)

    # ── Floor number ──────────────────────────────────────────────────────────

    floor_number: int | None = None
    m_fl = re.search(
        r"[ÉéEe]tage[s]?\s*:?\s*(\d+)|(\d+)[eèème]{1,3}\s*[ÉéEe]tage|[Ff]loor\s*(\d+)",
        text, re.I
    )
    if m_fl:
        floor_number = int(next(g for g in m_fl.groups() if g))

    # ── Image URL ─────────────────────────────────────────────────────────────

    image_urls: list[str] = []
    img_tag = anchor.find("img")
    if img_tag:
        src = img_tag.get("data-src") or img_tag.get("src", "")
        if src and src.startswith("http"):
            image_urls = [src]

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
        "description":      "",
        "floor_number":     floor_number,
        "total_floors":     None,
        "image_urls":       image_urls,
    }


# ── Page scraper ──────────────────────────────────────────────────────────────

def extract_cards_from_page(soup: BeautifulSoup) -> list[BeautifulSoup]:
    """Return all listing anchor tags from a search result page."""
    return [
        a for a in soup.find_all("a", href=re.compile(r"/fr/.+_\d+\.htm$"))
        if re.search(r"/fr/([^/]+)/([^/]+)/[^/]+_(\d+)\.htm$", a.get("href", ""))
    ]


def scrape_section(
    section_key: str,
    base_url: str,
    price_period_default: str,
    max_pages: int | None,
    limit: int | None,
) -> list[dict]:
    print(f"\n[{SOURCE}] {section_key.upper()} → {base_url}")

    results: list[dict] = []
    seen_ids: set[str] = set()

    def add_cards(soup: BeautifulSoup) -> int:
        added = 0
        for anchor in extract_cards_from_page(soup):
            listing = parse_card(anchor, section_key, price_period_default)
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
        print(f"  [error] Could not load page 1 — skipping {section_key}")
        return results

    total_pages = detect_total_pages(soup)
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

    # Pages 2 → end
    for page_num in range(2, total_pages + 1):
        soup = fetch(f"{base_url}?o={page_num}")
        if not soup:
            print(f"  [warn] Failed to load page {page_num} — stopping")
            break

        anchors = extract_cards_from_page(soup)
        if not anchors:
            print(f"  Page {page_num}: no cards found — reached end")
            break

        n = add_cards(soup)
        print(f"  Page {page_num}: {n} new listings (total: {len(results)})")

        if limit and len(results) >= limit:
            print(f"  [limit] Capped at {limit}")
            break

        time.sleep(random.uniform(1.5, 3.0))

    return results


# ── Entry points ──────────────────────────────────────────────────────────────

def run(
    max_pages: int | None = None,
    limit: int | None = None,
    sections: list[str] | None = None,
) -> list[dict]:
    active = [s for s in SECTIONS if sections is None or s[0] in sections]
    all_listings: list[dict] = []

    for section_key, base_url, price_period_default in active:
        listings = scrape_section(section_key, base_url, price_period_default, max_pages, limit)
        all_listings.extend(listings)
        print(f"[{SOURCE}] {section_key}: {len(listings)} listings scraped\n")

    print(f"[{SOURCE}] Total scraped: {len(all_listings)} listings")
    save_results(SOURCE, all_listings)
    return all_listings


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape avito.ma real estate listings")
    ap.add_argument("--pages",    type=int,  default=None,
                    help="Max pages per section (default: auto-detect all)")
    ap.add_argument("--limit",    type=int,  default=None,
                    help="Cap total listings for testing (e.g. --limit 50)")
    ap.add_argument("--sections", nargs="+", default=None,
                    choices=SECTION_NAMES,
                    help="Sections to scrape (default: all three)")
    args = ap.parse_args()
    run(max_pages=args.pages, limit=args.limit, sections=args.sections)
