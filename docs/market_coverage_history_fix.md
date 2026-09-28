# Market coverage and history corrections — 2026-09-28

## Changed behavior

- Regular, deep and full apartment searches have no implicit 80 million KZT
  ceiling and no photo-only filter. An explicit caller-supplied `max_price`
  remains supported for a deliberately restricted query.
- Rental service and bot background loop share one-page collection logic.
  Each category has a durable `RENTAL_CRAWL_V1_<TYPE>` cursor in
  `app_settings`, with raw page-ID fingerprints. There is no five-page cap.
  Categories rotate after each attempt; index refreshes do not wait for a
  potentially long full pass. Existing 5–15 minute pacing remains.
- Unbounded rental scans stop on a recognised empty search or repeated page,
  including a repeated non-adjacent page. HTTP errors and unrecognised empty
  responses raise and leave the cursor unchanged. Cards rejected by price or
  field validation do not falsely signal the end of pagination.
- Historical asking price is reconstructed from the latest change at/before
  `as_of`, otherwise the first later event's `old_price`. Current price is
  used only when there are no price events. One active ad per property is
  selected deterministically for price statistics.
- DOM excludes known archive gaps and merges overlapping activity. A
  property's disappearance is a dated exit while all its observed IDs are
  inactive. `within_30d` means an exit within 30 days from first observation,
  among properties with a fully elapsed window; a later return does not
  erase that exit. It does not mean a confirmed sale.
- A confirmed relist requires a new ID after every older ID was archived.
  Parallel ads do not count, and same-ID returns remain reactivations.
  Market Profile and Property Timeline share this definition. Profile counts
  properties with relists; Timeline counts relist events.
- AI adapter versions for history/profile are v2 because metric semantics
  changed; existing endpoint URLs remain unchanged.

## Deployment and limits

No schema migration or historical data rewrite is included. Deploy the code
and restart the rental/apartment collectors, bot and web process as applicable.
The service and bot use the same durable rental cursors; run one rental
collector at a time to avoid duplicate requests. `RENTAL_MAX_PAGES` is no
longer used by a full rental cycle. The initial complete scan may take days
at the existing request rate; monitor cursor progress and successful runs.

The first apartment deep-sweep may finish its old cached boundary before
starting a new unrestricted pass. A controlled `full_sweep.py --reset` run
can discover the newly included price/photo segments sooner. Do not infer
100% market coverage merely from removing filters or page limits.

Historical calculations reconstruct events retrospectively with today's
Property Identity links. Area, rooms and complex attributes are not fully
versioned, so historical price-per-square-metre still uses the current area.
These limits, missing archive timestamps and price fallbacks are reported in
`data_quality`. This is not a leakage-free model-training dataset.

## Verification

The regression tests cover scans beyond page 10, repeated last pages, rejected
cards, HTTP/challenge failures, durable retry after a save failure, unlimited
sale query parameters, dated 30-day exits, reactivation, historical archive
gaps, parallel ads, relists and one price vote per property.

`tests/test_complex_market_profile.py` additionally verifies SQL historical
price reconstruction before/on/after price changes and closed archive gaps
against the isolated PostgreSQL database used by CI.
