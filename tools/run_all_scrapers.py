"""
Orchestrator — runs all real estate scrapers in sequence.
One failed scraper does not stop the others.

Usage:
    python tools/run_all_scrapers.py                          # full run, page count auto-detected per site
    python tools/run_all_scrapers.py --limit 20               # test: cap at 20 listings per scraper
    python tools/run_all_scrapers.py --pages 3                # test: force max 3 pages per section
    python tools/run_all_scrapers.py --sources mubawab avito  # run specific scrapers only
"""

import argparse
import importlib
import sys
import traceback
from datetime import datetime
from pathlib import Path

SCRAPERS = ["scrape_mubawab", "scrape_avito", "scrape_sarouty", "scrape_agenz"]


def run_scraper(name: str, pages: int | None, limit: int | None) -> dict:
    start = datetime.now()
    print(f"\n{'='*60}")
    print(f"[orchestrator] Starting {name} at {start.strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    try:
        mod = importlib.import_module(name)
        listings = mod.run(max_pages=pages, limit=limit)
        elapsed = (datetime.now() - start).seconds
        print(f"[orchestrator] {name} done — {len(listings)} listings in {elapsed}s")
        return {"source": name, "status": "success", "count": len(listings)}
    except Exception as exc:
        print(f"[orchestrator] {name} FAILED: {exc}")
        traceback.print_exc()
        return {"source": name, "status": "error", "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages",   type=int, default=None,
                        help="Force max pages per section (default: auto-detect from site pagination)")
    parser.add_argument("--limit",   type=int, default=None, help="Cap listings per scraper (testing)")
    parser.add_argument("--sources", nargs="*", default=None,
                        choices=[s.replace("scrape_", "") for s in SCRAPERS],
                        help="Run only specific scrapers (e.g. --sources mubawab avito)")
    args = parser.parse_args()

    # Add tools/ directory to path so scrapers can import each other
    tools_dir = Path(__file__).parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))

    targets = SCRAPERS
    if args.sources:
        targets = [f"scrape_{s}" for s in args.sources]

    summary = []
    total_start = datetime.now()

    for name in targets:
        result = run_scraper(name, pages=args.pages, limit=args.limit)
        summary.append(result)

    elapsed = (datetime.now() - total_start).seconds
    print(f"\n{'='*60}")
    print(f"[orchestrator] All scrapers finished in {elapsed}s")
    print(f"{'='*60}")
    for r in summary:
        icon = "✓" if r["status"] == "success" else "✗"
        count = r.get("count", "—")
        err = f" [{r.get('error', '')}]" if r["status"] == "error" else ""
        print(f"  {icon} {r['source']}: {count} listings{err}")

    failures = [r for r in summary if r["status"] == "error"]
    if failures:
        print(f"\n[orchestrator] {len(failures)} scraper(s) failed — see logs above")
        sys.exit(1)


if __name__ == "__main__":
    main()
