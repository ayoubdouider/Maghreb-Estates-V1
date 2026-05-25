"""
Data pipeline — loads scraped JSON files from .tmp/ and pushes them to Supabase.

What it does per listing:
  1. Upsert into `listings` on (source, source_id) conflict — updates price & last_seen_at
  2. If price changed since last scrape → insert a row into `price_history`
  3. After all files processed → mark listings not seen today as is_active = FALSE

Usage:
    python tools/push_to_supabase.py
    python tools/push_to_supabase.py --date 2025-05-20   # process files from a specific date
    python tools/push_to_supabase.py --dry-run           # print stats without writing
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TMP_DIR = Path(__file__).parent.parent / ".tmp"

CITY_ALIASES: dict[str, str] = {
    # Normalize common alternate spellings to a canonical form
    "casablanca":       "Casablanca",
    "casa":             "Casablanca",
    "dar el beida":     "Casablanca",
    "rabat":            "Rabat",
    "marrakech":        "Marrakech",
    "marrakesh":        "Marrakech",
    "fes":              "Fès",
    "fès":              "Fès",
    "fez":              "Fès",
    "tanger":           "Tanger",
    "tangier":          "Tanger",
    "agadir":           "Agadir",
    "meknes":           "Meknès",
    "meknès":           "Meknès",
    "oujda":            "Oujda",
    "kenitra":          "Kénitra",
    "kénitra":          "Kénitra",
    "tetouan":          "Tétouan",
    "tétouan":          "Tétouan",
    "el jadida":        "El Jadida",
    "safi":             "Safi",
    "mohammedia":       "Mohammedia",
    "nador":            "Nador",
    "beni mellal":      "Béni Mellal",
    "essaouira":        "Essaouira",
    "ifrane":           "Ifrane",
    "ouarzazate":       "Ouarzazate",
    "laayoune":         "Laâyoune",
    "dakhla":           "Dakhla",
}


def normalize_city(raw: str) -> str:
    if not raw:
        return ""
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


_DIRTY_NEIGH = re.compile(
    r"\b(appartement|studio|bureau|villa|maison|terrain|riad|local|duplex|"
    r"à\s+vendre|à\s+louer|vente|location|louer|vendre|meublé|meuble|"
    r"standing|résidence|programme|appartements)\b",
    re.I | re.UNICODE,
)


def _slug_to_label(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def clean_neighborhood(listing: dict) -> str | None:
    """Return a clean neighborhood value, or None if it can't be determined reliably."""
    source = listing.get("source", "")
    url = listing.get("url") or ""
    neighborhood = (listing.get("neighborhood") or "").strip()

    if source == "avito":
        # Always derive from URL slug — more reliable than scraper regex output
        m = re.search(r"/fr/([^/]+)/[^/]+/[^/]+_\d+\.htm", url)
        if m:
            return _slug_to_label(m.group(1))

    # For all sources: reject values that look like listing titles
    if not neighborhood or _DIRTY_NEIGH.search(neighborhood) or len(neighborhood) > 50:
        return None

    return neighborhood


def load_json_files(target_date: str) -> list[dict]:
    """Load all <source>_<date>.json files from .tmp/ for the given date."""
    files = list(TMP_DIR.glob(f"*_{target_date}.json"))
    if not files:
        print(f"[pipeline] No JSON files found in .tmp/ for date {target_date}")
        return []

    all_listings = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        print(f"[pipeline] Loaded {len(data)} listings from {f.name}")
        all_listings.extend(data)

    return all_listings


def get_existing_prices(client: Client, source_ids: list[tuple[str, str]]) -> dict[tuple[str, str], float | None]:
    """Fetch current prices for a batch of (source, source_id) pairs."""
    if not source_ids:
        return {}

    sources = list({s for s, _ in source_ids})
    ids = list({sid for _, sid in source_ids})

    resp = (
        client.table("listings")
        .select("source, source_id, price, id")
        .in_("source", sources)
        .in_("source_id", ids)
        .execute()
    )
    return {(r["source"], r["source_id"]): (r["price"], r["id"]) for r in resp.data}


def push_listings(client: Client, listings: list[dict], dry_run: bool, target_date: str) -> dict:
    stats = {"new": 0, "updated": 0, "price_changes": 0, "skipped": 0}

    # Fetch existing prices in bulk (batch of 500)
    BATCH = 500
    existing: dict[tuple[str, str], tuple[float | None, str]] = {}
    keys = [(l["source"], l["source_id"]) for l in listings]
    for i in range(0, len(keys), BATCH):
        existing.update(get_existing_prices(client, keys[i:i + BATCH]))

    upsert_rows = []
    price_history_rows = []
    seen_source_ids: set[tuple[str, str]] = set()

    for listing in listings:
        source = listing.get("source", "")
        source_id = str(listing.get("source_id", ""))
        key = (source, source_id)

        if key in seen_source_ids:
            stats["skipped"] += 1
            continue
        seen_source_ids.add(key)

        city = normalize_city(listing.get("city", ""))

        row = {
            "source":           source,
            "source_id":        source_id,
            "url":              listing.get("url"),
            "transaction_type": listing.get("transaction_type"),
            "price_period":     listing.get("price_period"),
            "property_type":    listing.get("property_type"),
            "price":            listing.get("price"),
            "price_currency":   listing.get("price_currency", "MAD"),
            "surface_m2":       listing.get("surface_m2"),
            "bedrooms":         listing.get("bedrooms"),
            "bathrooms":        listing.get("bathrooms"),
            "city":             city,
            "neighborhood":     clean_neighborhood(listing),
            "amenities":        listing.get("amenities"),
            "title":            listing.get("title", ""),
            "description":      listing.get("description") or None,
            "floor_number":     listing.get("floor_number"),
            "total_floors":     listing.get("total_floors"),
            "image_urls":       listing.get("image_urls") or None,
            "last_seen_at":     datetime.now().isoformat(),
            "is_active":        True,
        }

        prev = existing.get(key)
        if prev is None:
            row["first_seen_at"] = datetime.now().isoformat()
            stats["new"] += 1
        else:
            prev_price, listing_db_id = prev
            stats["updated"] += 1
            new_price = listing.get("price")
            if new_price is not None and prev_price != new_price:
                price_history_rows.append({
                    "listing_id": listing_db_id,
                    "price":      new_price,
                    "recorded_at": datetime.now().isoformat(),
                })
                stats["price_changes"] += 1

        upsert_rows.append(row)

    if not dry_run:
        # Upsert listings in batches of 500
        for i in range(0, len(upsert_rows), BATCH):
            client.table("listings").upsert(
                upsert_rows[i:i + BATCH],
                on_conflict="source,source_id",
            ).execute()

        # Insert price history rows
        for i in range(0, len(price_history_rows), BATCH):
            client.table("price_history").insert(price_history_rows[i:i + BATCH]).execute()

        # Mark listings not seen today as inactive
        client.table("listings").update({"is_active": False}).lt("last_seen_at", target_date).execute()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",    default=date.today().isoformat(),
                        help="Process files from this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print stats without writing to Supabase")
    args = parser.parse_args()

    listings = load_json_files(args.date)
    if not listings:
        sys.exit(0)

    print(f"[pipeline] {len(listings)} total listings to process (dry_run={args.dry_run})")

    if not args.dry_run:
        client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    else:
        client = None  # type: ignore[assignment]

    stats = push_listings(client, listings, dry_run=args.dry_run, target_date=args.date)

    print(f"\n[pipeline] Done:")
    print(f"  New listings:     {stats['new']}")
    print(f"  Updated listings: {stats['updated']}")
    print(f"  Price changes:    {stats['price_changes']}")
    print(f"  Duplicates skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
