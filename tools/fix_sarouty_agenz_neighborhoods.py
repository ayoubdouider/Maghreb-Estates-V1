"""
One-time migration: fix neighborhood/city for existing Sarouty and Agenz records.

Two bugs are fixed:
1. Sarouty breadcrumb bug: neighborhood = listing title, city = actual neighborhood
   → new_neighborhood = old city, new_city = extracted from URL
2. Agenz location-text bug: city/neighborhood were swapped ("Neighborhood, City" text
   was parsed as city=Neighborhood, neighborhood=City)
   → detected when neighborhood is a known city name → swap

Usage:
    python tools/fix_sarouty_agenz_neighborhoods.py --dry-run
    python tools/fix_sarouty_agenz_neighborhoods.py
    python tools/fix_sarouty_agenz_neighborhoods.py --source sarouty
    python tools/fix_sarouty_agenz_neighborhoods.py --source agenz
"""

import argparse
import os
import re
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BATCH = 400

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
KNOWN_CITIES = set(CITY_ALIASES.values())

# Patterns that indicate a listing title rather than a neighborhood name
DIRTY_PATTERN = re.compile(
    r"\b(appartement|studio|bureau|villa|maison|terrain|riad|local|duplex|penthouse|"
    r"à\s+vendre|à\s+louer|vente|location|louer|vendre|meublé|meuble|neuf|moderne|"
    r"standing|résidence|programme|appartements|haut|parking|titré|titr[eé])\b",
    re.I | re.UNICODE,
)

# Navigation/category words that are not neighborhood names
NAV_WORDS = {
    "maroc", "france", "accueil", "home", "vente", "location",
    "appartements", "villas", "terrains", "bureaux", "louer", "acheter",
    "sarouty", "agenz", "", "–", "-", "immobilier",
}


def normalize_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), "")


def city_from_text(text: str) -> str:
    """Find a known city name anywhere in text (URL slug, title, etc.)."""
    text_lower = text.lower()
    for alias in sorted(CITY_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text_lower):
            return CITY_ALIASES[alias]
    return ""


def is_dirty(text: str) -> bool:
    """Return True if text looks like a listing title rather than a neighborhood name."""
    return bool(DIRTY_PATTERN.search(text)) or len(text) > 50


def is_nav_word(text: str) -> bool:
    return text.lower().strip() in NAV_WORDS


def compute_fix(row: dict) -> tuple[str, str] | None:
    """
    Returns (new_neighborhood, new_city) or None if no change needed.
    new_neighborhood / new_city may be "" → will be stored as NULL.
    """
    url = row.get("url") or ""
    curr_city = (row.get("city") or "").strip()
    curr_neigh = (row.get("neighborhood") or "").strip()

    # Case B — Agenz swapped: neighborhood is a known city name
    if curr_neigh in KNOWN_CITIES and curr_city not in KNOWN_CITIES and not is_nav_word(curr_city):
        return (curr_city, curr_neigh)  # swap

    # Case A — dirty neighborhood (listing title stored as neighborhood)
    if is_dirty(curr_neigh):
        new_neigh = curr_city if not is_nav_word(curr_city) else ""
        new_city = (
            normalize_city(curr_city)
            or city_from_text(url)
            or city_from_text(curr_neigh)
        )
        return (new_neigh, new_city)

    return None  # already clean


def fetch_records(client, source: str) -> list[dict]:
    print(f"[migration] Fetching {source} records from Supabase...")
    rows = []
    from_row = 0
    while True:
        resp = (
            client.table("listings")
            .select("source_id,url,city,neighborhood")
            .eq("source", source)
            .range(from_row, from_row + 999)
            .execute()
        )
        if not resp.data:
            break
        rows.extend(resp.data)
        print(f"  Fetched {len(rows)} rows...")
        if len(resp.data) < 1000:
            break
        from_row += 1000
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source", choices=["sarouty", "agenz", "both"], default="both")
    args = parser.parse_args()

    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    sources = ["sarouty", "agenz"] if args.source == "both" else [args.source]

    for source in sources:
        rows = fetch_records(client, source)
        print(f"[migration] {len(rows)} {source} listings found")

        # Group by (new_neighborhood, new_city) for batch updates
        updates: dict[tuple[str, str], list[str]] = defaultdict(list)
        no_change = 0

        for row in rows:
            fix = compute_fix(row)
            if fix:
                updates[fix].append(row["source_id"])
            else:
                no_change += 1

        total_to_update = sum(len(v) for v in updates.values())
        print(f"[migration] {total_to_update} to update, {no_change} already OK")

        if args.dry_run:
            print(f"\n[dry-run] Sample updates for {source}:")
            for (neigh, city), sids in list(updates.items())[:20]:
                print(f"  neigh={repr(neigh)[:35]:37s} city={repr(city)[:20]:22s} → {len(sids)} listings")
            continue

        total_updated = 0
        for (neigh, city), sids in updates.items():
            for i in range(0, len(sids), BATCH):
                batch = sids[i : i + BATCH]
                client.table("listings").update({
                    "neighborhood": neigh or None,
                    "city": city or None,
                }).in_("source_id", batch).eq("source", source).execute()
                total_updated += len(batch)
            print(f"  Updated {len(sids):5d} × neigh={repr(neigh)[:30]:32s} city={repr(city)[:20]}")

        print(f"\n[migration] {source} done — {total_updated} listings updated")


if __name__ == "__main__":
    main()
