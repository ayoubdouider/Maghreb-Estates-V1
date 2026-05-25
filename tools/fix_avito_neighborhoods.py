"""
One-time migration: extract clean neighborhood names from Avito listing URLs
and update the neighborhood column in Supabase for all existing Avito records.

URL format: /fr/<neighborhood_slug>/<category>/<title>_<id>.htm
Strategy: group source_ids by neighborhood → one UPDATE per neighborhood batch

Usage:
    python tools/fix_avito_neighborhoods.py
    python tools/fix_avito_neighborhoods.py --dry-run
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BATCH = 400  # source_ids per IN() clause


def slug_to_label(slug: str) -> str:
    return slug.replace("_", " ").strip().title()


def fetch_avito_urls(client) -> list[tuple[str, str]]:
    """Fetch all (source_id, url) pairs for avito listings from Supabase."""
    print("[migration] Fetching Avito URLs from Supabase...")
    pairs = []
    from_row = 0
    while True:
        resp = (
            client.table("listings")
            .select("source_id,url")
            .eq("source", "avito")
            .range(from_row, from_row + 999)
            .execute()
        )
        if not resp.data:
            break
        pairs.extend((r["source_id"], r["url"]) for r in resp.data)
        print(f"  Fetched {len(pairs)} rows...")
        if len(resp.data) < 1000:
            break
        from_row += 1000
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    pairs = fetch_avito_urls(client)
    print(f"[migration] {len(pairs)} Avito listings found")

    # Extract neighborhood from URL and group source_ids
    by_neighborhood: dict[str, list[str]] = defaultdict(list)
    skipped = 0
    for source_id, url in pairs:
        m = re.search(r"/fr/([^/]+)/([^/]+)/[^/]+_\d+\.htm", url or "")
        if m:
            neighborhood = slug_to_label(m.group(1))
            by_neighborhood[neighborhood].append(source_id)
        else:
            skipped += 1

    print(f"[migration] {len(by_neighborhood)} unique neighborhoods, {skipped} skipped (no URL match)")

    if args.dry_run:
        print("\n[dry-run] Sample neighborhoods:")
        for neigh, sids in list(by_neighborhood.items())[:15]:
            print(f"  {repr(neigh):35s} → {len(sids)} listings")
        return

    # Update Supabase: one request per neighborhood per batch of BATCH source_ids
    total_updated = 0
    for neigh, sids in by_neighborhood.items():
        for i in range(0, len(sids), BATCH):
            batch = sids[i : i + BATCH]
            client.table("listings").update({"neighborhood": neigh}).in_(
                "source_id", batch
            ).eq("source", "avito").execute()
            total_updated += len(batch)

        print(f"  Updated {len(sids):5d} × '{neigh}'")

    print(f"\n[migration] Done — {total_updated} listings updated")


if __name__ == "__main__":
    main()
