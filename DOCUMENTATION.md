# Technical documentation

The maintained technical and operational documentation is in [README.md](README.md),
with security policy in [SECURITY.md](SECURITY.md) and release history in
[CHANGELOG.md](CHANGELOG.md).

The August 2025 document was replaced because it described unverified accuracy,
performance, backtesting, and production capabilities that were not reproducible
from the repository.

Version 3.0 discovers the bounded active spot-USDT union from OKX and Binance at runtime.
The local fuzzy engine covers every token with sufficient validated history; unavailable
tokens remain explicit as `NOT_RATED`. The optional OpenAI contract reviews only a
deterministic, configurable shortlist and remains non-authoritative. Neither layer executes
orders or carries a verified accuracy or profitability claim.

The schema-v3 JSON audit trail contains the frozen universe hash, exchange coverage,
per-token analysis status, complete local recommendations, optional model metadata, event
evidence, and effective risk caps. The Markdown digest is intentionally a bounded summary;
`digest.json` is the complete artifact.
