# Changelog

## 3.0.0 - 2026-07-15

- Replaced the fixed recommendation universe with bounded daily discovery of the active
  spot-USDT union on OKX and Binance.
- Added strict exchange metadata parsers, deterministic cross-venue deduplication,
  stablecoin/leveraged-token filters, source counts, and a universe audit hash.
- Added a public Binance hourly-candle client and per-token OKX-to-Binance fallback without
  combining candle histories from different venues.
- Added bounded concurrent collection, mandatory core coverage, configurable global
  coverage, and explicit `NOT_RATED` records for unavailable non-core tokens.
- Generalized the fuzzy advisor to arbitrary canonical universes while preserving the
  global maximum of five BUY candidates and every non-execution invariant.
- Limited the optional OpenAI second opinion to a deterministic dynamic shortlist of at
  most 12 tokens; every other token retains its local result and an explicit review status.
- Upgraded the complete JSON artifact to schema v3 with universe and venue metadata, while
  bounding the human Markdown digest for safe notification delivery.
- Removed generic-news fan-out across the token universe and added discovery, Binance,
  scale, fallback, and dynamic-model tests.

## 2.1.0 - 2026-07-15

- Added an explainable fuzzy expert recommendation for every allowlisted token on
  every valid daily run.
- Added 24/72-hour trend and momentum, RSI, realized volatility, and drawdown
  features derived from the existing confirmed-candle history.
- Added conservative technical/fundamental integration and a maximum of five BUY
  candidates while keeping all output non-executable.
- Added an optional one-batch OpenAI Responses API second opinion with strict JSON
  output, public derived inputs, `store: false`, no tools, and deterministic fallback.
- Upgraded digest JSON to schema v2 with separate recommendations and material alerts.
- Propagated stricter configured risk caps into every recommendation and report.
- Added model name, prompt/hash, and model-cited event IDs to the audit trail.
- Corrected realized-volatility aggregation and resolved-incident/flexion handling.
- Added input freshness/source reconciliation, whole-run concurrency locking, and
  non-destructive same-day suppression receipts.

## 2.0.0 - 2026-07-15

- Rebuilt the project as a read-only material-event monitor.
- Added the fixed BTC, ETH, SOL, XRP, ADA, SEI, APT, and AVAX universe.
- Added confirmed-candle price/volume materiality, catalyst RSS classification,
  source quality, structured impact/risk analysis, and deterministic deduplication.
- Added optional Telegram/SMTP delivery and daily GitHub Actions execution.
- Added strict configuration, advisory portfolio guardrails, tests, packaging,
  security checks, and truthful documentation.
- Removed the unvalidated technical score, misleading simulated order surface,
  optional-model claims, and unsupported performance/accuracy metrics.
