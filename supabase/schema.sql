-- ============================================================
-- Marokko Vastgoed Scraper — Supabase Schema
-- Run this once in the Supabase SQL editor to set up the database
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ------------------------------------------------------------
-- listings: one row per unique property listing
-- Updated on each scrape (price, last_seen_at, is_active)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
  id                UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  source            TEXT        NOT NULL,  -- 'mubawab' | 'avito' | 'sarouty' | 'agenz'
  source_id         TEXT        NOT NULL,  -- original listing ID on the source site
  url               TEXT,
  transaction_type  TEXT,                  -- 'sale' | 'long_term_rental' | 'short_term_rental' | 'new_construction'
  price_period      TEXT,                  -- 'total' | 'month' | 'week' | 'day'
  property_type     TEXT,                  -- 'apartment' | 'house' | 'villa' | 'land' | 'commercial' | 'riad'
  price             NUMERIC,
  price_currency    TEXT        DEFAULT 'MAD',
  surface_m2        NUMERIC,
  bedrooms          INTEGER,
  bathrooms         INTEGER,
  city              TEXT,
  neighborhood      TEXT,
  amenities         JSONB,                 -- {pool, gym, parking, elevator, garden, ...}
  title             TEXT,
  description       TEXT,                  -- full property description text
  floor_number      SMALLINT,              -- floor the unit is on (0 = ground floor)
  total_floors      SMALLINT,              -- total floors in the building
  image_urls        JSONB,                 -- array of image URLs
  first_seen_at     TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at      TIMESTAMPTZ DEFAULT NOW(),
  is_active         BOOLEAN     DEFAULT TRUE,
  UNIQUE(source, source_id)
);

-- Migration for existing databases (run manually if table already exists):
-- ALTER TABLE listings ADD COLUMN IF NOT EXISTS description   TEXT;
-- ALTER TABLE listings ADD COLUMN IF NOT EXISTS floor_number  SMALLINT;
-- ALTER TABLE listings ADD COLUMN IF NOT EXISTS total_floors  SMALLINT;
-- ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_urls    JSONB;

-- ------------------------------------------------------------
-- price_history: one row per price change detected
-- Lets us track price trends over time per listing
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_history (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  listing_id  UUID        REFERENCES listings(id) ON DELETE CASCADE,
  price       NUMERIC,
  recorded_at TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------
-- scrape_runs: audit log of each scraping session
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
  id                UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  source            TEXT,
  started_at        TIMESTAMPTZ DEFAULT NOW(),
  completed_at      TIMESTAMPTZ,
  listings_scraped  INTEGER     DEFAULT 0,
  listings_new      INTEGER     DEFAULT 0,
  listings_updated  INTEGER     DEFAULT 0,
  status            TEXT        DEFAULT 'running'  -- 'running' | 'success' | 'error'
);

-- ------------------------------------------------------------
-- Indexes for common dashboard queries
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_listings_city        ON listings(city);
CREATE INDEX IF NOT EXISTS idx_listings_source      ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_type        ON listings(transaction_type);
CREATE INDEX IF NOT EXISTS idx_listings_active      ON listings(is_active);
CREATE INDEX IF NOT EXISTS idx_listings_price       ON listings(price);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id);
CREATE INDEX IF NOT EXISTS idx_price_history_time   ON price_history(recorded_at);

-- ------------------------------------------------------------
-- Row Level Security: allow anonymous reads (safe for frontend)
-- Writes require the service role key (GitHub Actions only)
-- ------------------------------------------------------------
ALTER TABLE listings       ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history  ENABLE ROW LEVEL SECURITY;
ALTER TABLE scrape_runs    ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_read_listings"
  ON listings FOR SELECT
  TO anon
  USING (true);

CREATE POLICY "anon_read_price_history"
  ON price_history FOR SELECT
  TO anon
  USING (true);

CREATE POLICY "anon_read_scrape_runs"
  ON scrape_runs FOR SELECT
  TO anon
  USING (true);

-- Service role bypasses RLS automatically — no extra policy needed for writes.
