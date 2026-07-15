# Changelog

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
