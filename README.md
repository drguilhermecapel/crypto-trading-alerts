# Crypto Trading Alerts

A read-only daily crypto advisor that discovers the active spot-USDT token universe on
OKX and Binance, analyzes every eligible token with an explainable fuzzy expert system,
and emits evidence-backed material alerts.

The program never accepts exchange credentials, places orders, opens short positions,
uses leverage, or makes autonomous investment decisions. Suggestions are decision
support, not instructions sent to an exchange and not financial advice.

## Dynamic exchange universe

Each run freezes a deterministic union of public exchange metadata:

- OKX `GET /api/v5/public/instruments?instType=SPOT`;
- Binance `GET /api/v3/exchangeInfo`;
- only active spot pairs with quote asset exactly `USDT`;
- stablecoins and exchange-encoded leveraged tokens are excluded;
- duplicate base symbols are consolidated while preserving both venue instruments;
- the eight original assets remain a mandatory health-check core;
- discovery is bounded at 2,000 assets and fails instead of silently truncating.

The runtime prefers OKX candles when that listing exists and falls back to Binance when
needed. A candle series is never spliced across exchanges. Individual non-core failures
remain visible as `NOT_RATED`; a core failure or coverage below the configured 90% stops
the run without advancing daily state.

Exchange discovery uses only the official public APIs documented by
[OKX](https://www.okx.com/docs-v5/en/) and
[Binance](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints).
No API key is needed.

## AI-assisted analysis

The authoritative local engine is a deterministic fuzzy expert system. For every token
with 192 validated closed hourly candles it combines:

- 24-hour and 72-hour momentum;
- EMA(24)/EMA(72) trend spread;
- RSI(14);
- current quote volume versus the median of seven previous 24-hour blocks;
- 24-hour realized volatility and seven-day drawdown;
- recent, token-specific catalyst evidence with conservative source weighting.

Generic crypto headlines are not copied to every token. This prevents one broad article
from manufacturing hundreds of fundamental signals.

| Action | Meaning |
|---|---|
| `BUY` | Candidate for portfolio review; never executable |
| `HOLD` | Valid data, but signals are weak or mixed |
| `REDUCE` | Consider lowering an existing spot position |
| `SELL` | Consider exiting an existing position; never open a short |
| `NOT_RATED` | The token was discovered but trustworthy history was unavailable |

At most five tokens receive `BUY` across the whole universe. Risk remains capped at 1%
per trade and 40% per asset, with a weekly -6% circuit breaker. Stricter configured caps
are propagated into every result.

An optional OpenAI second opinion reviews at most 12 deterministically selected strong
signals per run. The request contains only validated derived metrics and opaque event
IDs. It uses a strict JSON schema, `store: false`, no tools, and no web access. Tokens
outside the budget are marked `not_selected_budget`. A model result is always shown
separately and cannot change the effective local action, score, risk limits, or program
flow. The configured model is pinned to `gpt-5.6`.

The fuzzy scores and model opinions are heuristics, not calibrated probabilities or
evidence of future profitability.

## Material alerts

A price/volume event qualifies only when both inclusive conditions are met:

- absolute 24-hour return is at least 5.0%; and
- 24-hour quote volume is at least 1.5 times the seven-day block median.

Recent token-specific RSS/Atom evidence may also produce one of six catalyst classes:
on-chain/ecosystem, exchange/liquidity, network upgrade, outage/exploit,
ETF/institutional, or regulatory/legal. Low-quality sources cannot independently create
a news alert.

## Quick start

Python 3.11 or newer is required.

```bash
python -m pip install -r requirements.txt
python -m crypto_alerts validate-config --config config.example.json
python -m unittest discover -s tests -v
python -m crypto_alerts run --config config.example.json --no-notify
```

The compatibility entrypoint remains available:

```bash
python crypto_alerts_updated.py run --config config.example.json --no-notify
```

To enable the bounded optional model review:

```bash
export OPENAI_API_KEY="..."
python -m crypto_alerts run --config config.example.json --no-notify
```

## Configuration

`config.example.json` is strict schema version 2. Its important scale controls are:

```json
{
  "universe": {
    "mode": "exchange_union",
    "quote_asset": "USDT",
    "exchanges": ["okx", "binance"],
    "max_assets": 2000,
    "minimum_coverage_ratio": 0.9,
    "max_workers": 8,
    "exclude_stablecoins": true,
    "exclude_leveraged_tokens": true
  },
  "analysis": {
    "engine": "fuzzy_expert",
    "openai_enabled": true,
    "openai_model": "gpt-5.6",
    "openai_timeout_seconds": 20,
    "openai_max_assets": 12
  }
}
```

The `assets` list in the configuration is the mandatory core, not the complete runtime
universe.

## Artifacts and delivery

Every successful run writes:

- `artifacts/digest.json`: schema v3, including the complete recommendation set,
  universe hash, coverage, exchange counts, failures, alerts, and audit fields;
- `artifacts/digest.md`: a bounded human summary of the strongest signals and alerts;
- `artifacts/suppressed-run.json`: a receipt when a same-day run is suppressed.

The full JSON remains authoritative. Markdown is intentionally capped so hundreds of
tokens do not exceed Telegram or e-mail transport limits.

Telegram uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. SMTP uses `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_TO`. All values
must be process environment variables or GitHub Actions secrets. No secret is written
to reports or logs.

The scheduled workflow runs at 10:00 UTC (07:00 in São Paulo), supports manual runs,
restores deduplication state, and uploads artifacts even when delivery is unavailable.
The state transaction is process-locked, so overlapping runs cannot both commit the
same daily digest.

## Portfolio policy

`check-portfolio` evaluates a proposed spot allocation without placing trades:

```bash
python -m crypto_alerts check-portfolio \
  --config config.example.json \
  --portfolio portfolio.example.json
```

| Guardrail | Default maximum |
|---|---:|
| Active holdings | 5 |
| Weight per asset | 40% |
| Capital risk per trade | 1% |
| Weekly loss circuit breaker | Block at -6% or worse |
| Margin, derivatives, leverage | Prohibited |

## Limitations

The scoring rules have not been shown here to predict returns and have no verified
accuracy or profitability. Public exchange data can be delayed, incomplete, manipulated,
or inconsistent. Cross-exchange consolidation currently assumes identical canonical base
tickers refer to the same token; ticker reuse or redenomination remains a risk that users
must verify. RSS classification cannot validate every underlying claim. A model second
opinion can be wrong and is deliberately non-authoritative. `store: false` does not by
itself guarantee zero provider retention, so the model receives no holdings, balances,
PII, exchange credentials, notification credentials, source text, or URLs.

Review the evidence before acting and use only capital you can afford to lose. Licensed
under the MIT License.
