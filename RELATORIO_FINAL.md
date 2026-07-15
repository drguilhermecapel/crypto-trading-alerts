# Release verification report — v3.0

Version 3.0 expands the advisor from eight fixed tokens to the active, bounded spot-USDT
union discovered from the public OKX and Binance APIs. The original eight tokens remain a
mandatory health-check core.

## Implemented contract

- strict active spot-USDT discovery from explicit exchange base/quote fields;
- stablecoin and leveraged-token exclusion with regression coverage for `JUP`;
- deterministic union, provenance ordering, universe cardinality cap, and audit hash;
- validated 192-hour candle analysis from OKX or Binance, with venue fallback but no
  cross-venue candle splicing;
- one local fuzzy recommendation for every analyzable token and an explicit `NOT_RATED`
  record for each unavailable non-core token;
- no more than five global BUY candidates;
- optional non-authoritative OpenAI review of at most 12 deterministic candidates;
- complete schema-v3 JSON plus bounded Markdown suitable for notifications;
- no generic-news replication across the full token universe;
- spot-only advisory policy with no credentials, leverage, order, or execution surface.

## Failure behavior

The run stops without committing daily state if both discovery sources fail, a mandatory
core token cannot be analyzed, the discovered universe exceeds its cap, or market coverage
falls below the configured threshold. Isolated non-core failures remain auditable in the
successful artifact as `NOT_RATED`.

## Verification

The maintained CI matrix runs Python 3.11 and 3.12 tests, Ruff, Bandit, dependency audit,
package build/install validation, strict configuration validation, and Windows portability.
See `README.md`, `CHANGELOG.md`, and `SECURITY.md` for the operating and security contracts.

No predictive-accuracy, profitability, or autonomous-trading claim is made.
