# Workflow: Scrape Moroccan Real Estate Data

## Objective
Collect weekly property listings from 4 Moroccan real estate sites, store them in Supabase, and keep the dashboard at `maghreb-estates.com/marktanalyse.html` up to date with fresh trend data.

---

## Inputs Required
- Supabase project URL and keys (in `.env` and GitHub Secrets)
- This project pushed to a GitHub repository (for GitHub Actions)

## Outputs
- Updated `listings` table in Supabase (all active properties with latest price)
- Updated `price_history` table (one row per price change detected)
- Updated `scrape_runs` audit log
- `.tmp/<source>_<date>.json` files (temporary, auto-uploaded as GitHub Actions artifacts)

---

## Automated Run (every Monday)

GitHub Actions handles the full pipeline automatically:
1. Triggers at 07:00 UTC = 09:00 Morocco time every Monday
2. Runs all scrapers → pushes to Supabase → archives `.tmp/` as artifact
3. Monitor at: `https://github.com/<your-repo>/actions`

To trigger manually: Actions tab → "Weekly Real Estate Scrape" → Run workflow.

---

## Manual Run (local)

### Full run (all 4 sites)
```bash
pip install -r requirements.txt
playwright install chromium

python tools/run_all_scrapers.py
python tools/push_to_supabase.py
```

### Test run (10 listings per site, 2 pages)
```bash
python tools/run_all_scrapers.py --limit 10 --pages 2
python tools/push_to_supabase.py --dry-run   # prints stats, no DB writes
```

### Single site
```bash
python tools/run_all_scrapers.py --sources mubawab
python tools/push_to_supabase.py
```

### Individual scraper directly
```bash
python tools/scrape_mubawab.py --limit 20
```

---

## Sites Covered

| Source    | URL              | Type         | Notes |
|-----------|------------------|--------------|-------|
| mubawab   | mubawab.ma       | Classifieds  | Largest Moroccan RE site; proven scrapable |
| avito     | avito.ma         | Marketplace  | Strong real estate section; numeric pagination |
| sarouty   | sarouty.ma       | Classifieds  | JS-heavy; uses `networkidle` wait |
| agenz     | agenz.ma         | Aggregator   | Includes new-build (programme neuf) section |

---

## Handling Common Failures

### Scraper returns 0 listings / blocked
1. Check if the site is down: open the URL in a browser
2. Try with a shorter delay: the site may have changed its rate limits
3. Inspect the page HTML: selector names change when sites redeploy
   - Open Playwright in non-headless mode to debug:
     ```python
     browser = pw.chromium.launch(headless=False)
     ```
4. Update the CSS selectors in `tools/scrape_<site>.py` to match the new HTML
5. Document the selector change in this workflow

### Pagination stops early
- Check the "next page" selector in `scrape_listing_urls()` — it's site-specific
- Some sites use `?page=N` others use `?o=<offset>` (Avito)
- Inspect the "Next" button's HTML and update the selector

### Price parsing returns None
- Use `parse_price()` from `scraper_base.py` — it strips all non-digits
- If currency format changed (e.g. "€" instead of "DH"), update `parse_price()` in `scraper_base.py`

### GitHub Actions times out
- Default timeout is 360 minutes (6 hours)
- If a single scraper hangs: check if the site added a bot challenge (CAPTCHA)
- Next step if blocking is persistent: upgrade to ScraperAPI or Bright Data proxy
  - Replace `playwright.sync_api` calls with proxy-routed requests
  - Store the API key in GitHub Secrets as `SCRAPER_API_KEY`

### Supabase write fails
- Check that `SUPABASE_SERVICE_KEY` is set correctly (not the anon key)
- Verify RLS policies allow service role to INSERT/UPDATE on `listings` table
- Check Supabase project is not paused (free tier pauses after 1 week of inactivity)

---

## Adding a New Site

1. Copy `tools/scrape_mubawab.py` → `tools/scrape_newsite.py`
2. Update `SOURCE`, `BASE_URL`, `SECTIONS`
3. Adjust CSS selectors in `scrape_listing_page()` and `scrape_listing_urls()`
4. Test locally: `python tools/scrape_newsite.py --limit 5`
5. Add `"scrape_newsite"` to the `SCRAPERS` list in `tools/run_all_scrapers.py`
6. Update this workflow table above

---

## Adding AirDNA Data (future)

AirDNA exports can be added as a separate JSON file in `.tmp/` with a matching schema:
- Map `daily_rate` → `price`, `property_type` → `"short_term_rental"`, `city`, `neighborhood`
- Add `"airbnb"` and `"booking"` as source values in `listings`
- Run `python tools/push_to_supabase.py` normally — it will pick up the new source files

---

## Dashboard Maintenance

The dashboard (`marktanalyse.html` in the website repo) reads directly from Supabase via the anon key. No rebuild needed — charts update automatically when new data is in Supabase.

If the dashboard stops loading data:
1. Check browser console for Supabase API errors
2. Verify `SUPABASE_ANON_KEY` in the dashboard JS is still valid
3. Check Supabase RLS policies allow anonymous SELECT
