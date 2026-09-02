# Uganda Business Lead Generator — Max Coverage / Stable Cloud Build

This build is designed for lead generation for a debt-recovery business. It searches public Uganda directories/registries plus OpenStreetMap and combines records across sources.

## Important design goals
- Maximum public records exposed by each source, rather than a fixed 5/7/10 result cap.
- Search any business sector/keyword in Kampala, Wakiso, Mukono, Masaka, Jinja and Western Uganda.
- Western Uganda is expanded through multiple cities/districts and multiple OSM geographic areas.
- Phone extraction supports Ugandan mobile and landline formats and collects multiple public numbers when available.
- Missing phone/address fields are enriched from public business profile pages when possible.
- Physical address is never replaced with the region name. District is a separate field.
- Results are deduplicated and enriched when the same business appears in multiple sources.
- Each source reports its own result count and errors instead of silently failing.
- No paid Google Maps/Places API is used.

## Cloud stability
The previous build could launch several Chromium processes while also making broad Overpass queries. On Streamlit Community Cloud that can exhaust memory during a large Western Uganda search.

This build therefore:
- uses normal HTTP scraping by default;
- makes Chromium fallback optional with `ENABLE_BROWSER_FALLBACK=1`;
- limits concurrent directory workers;
- uses compact Overpass queries instead of thousands of regex clauses;
- keeps per-source and overall search limits while allowing large result sets.

## Files
- `app.py` — Streamlit interface
- `scraper.py` — source crawling, contact enrichment, OSM, normalization and dedupe
- `database.py` — SQLite storage and enrichment merge
- `requirements.txt` — Python dependencies
- `packages.txt` — Linux package dependency (`chromium`)

## Fresh database
If old test data is not needed, delete `uganda_leads.db` before the first run. It will be recreated automatically.
