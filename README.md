# Crypto Trading Alerts

A read-only, once-daily monitor for material developments affecting BTC, ETH, SOL,
XRP, ADA, SEI, APT, and AVAX. It combines confirmed OKX spot candles with recent
catalyst headlines and produces an evidence-bearing digest suitable for Telegram,
e-mail, GitHub Actions artifacts, and local review.

This is an alerting and decision-support tool. It does not place orders, accept
exchange credentials, use leverage, or make autonomous investment decisions.

## What counts as material

A market move is emitted only when both conditions are met:

- absolute 24-hour return is at least 5.0%; and
- 24-hour quote volume is at least 1.5 times the median of the previous seven
  non-overlapping 24-hour periods.

The boundary is inclusive: +5.00% or -5.00% with 1.50x volume qualifies. A 4.99%
move or 1.49x volume does not.

Recent RSS/Atom items are classified into six catalyst groups:

1. on-chain or ecosystem acceleration;
2. exchange or liquidity events;
3. network upgrades;
4. outages, exploits, or security incidents;
5. ETF or institutional catalysts; and
6. regulatory or legal catalysts.

Low-quality sources cannot independently create a news alert. Each emitted event
contains the catalyst, evidence URL, source quality, probable market impact, main
risk, and a technical-versus-fundamental classification. These fields are
deterministic assessments, not forecasts or guarantees.

## Source policy

| Source | Use | Quality |
|---|---|---|
| OKX public spot candles | Price and quote-volume evidence | High / primary market data |
| Government and monitored-network official domains | Regulatory, legal, network evidence | High |
| Established financial/crypto editorial domains | Catalyst discovery | Medium |
| Unknown domains | Context only; never sufficient alone | Low |

The default feeds are the official [SEC RSS feed](https://www.sec.gov/about/rss-feeds)
and [CoinDesk RSS](https://www.coindesk.com/coindesk-news/2021/09/17/coindesk-rss).
Market data uses the official [OKX public market API](https://www.okx.com/docs-v5/en/).

## Quick start

Python 3.11 or newer is required. Install the pinned hardened XML parser and timezone
data dependencies before running the monitor.

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

Each run writes a Markdown digest and machine-readable JSON under `artifacts/`.
When no material event exists, the report says so and no notification is sent by
default.

## Telegram and e-mail

Set notification credentials only as local environment variables or GitHub Actions
secrets. Telegram requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. SMTP requires
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_TO`.
No secret is written to reports or logs.

The scheduled workflow falls back to artifact-only mode if Telegram secrets are not
configured. Add the two Telegram secrets in the repository settings to receive the
digest on the phone.

## Daily schedule and deduplication

`.github/workflows/daily-alerts.yml` runs at 10:00 UTC (07:00 in São Paulo) and can
also be started manually. State is restored through a GitHub Actions cache. Event IDs
are suppressed for seven days and at most one digest is delivered per São Paulo
calendar day unless a manual force flag is used.

If required market data are incomplete, stale, malformed, or unavailable, the run
fails visibly and does not advance the state. Optional feed failures appear as
warnings; fewer than the configured minimum successful feeds is a hard failure.

## Advisory risk policy

The optional `check-portfolio` command evaluates a proposed spot allocation without
placing trades. Its fail-closed defaults are:

| Guardrail | Limit |
|---|---:|
| Active crypto holdings | 5 |
| Weight per asset | 40% |
| Capital risk per trade | 1% |
| Weekly loss circuit breaker | Block at -6% or worse |
| Margin, derivatives, leverage | Prohibited |

```bash
python -m crypto_alerts check-portfolio --config config.example.json --portfolio portfolio.example.json
```

## Example event

```json
{
  "asset": "SOL",
  "category": "price_volume",
  "catalyst": "SOL moved +5.4% over 24 hours with 1.7x quote volume",
  "evidence_urls": ["https://www.okx.com/api/v5/market/candles?instId=SOL-USDT&bar=1H&limit=193"],
  "source_quality": "HIGH",
  "probable_market_impact": "Short-term bullish pressure is plausible while volume remains elevated.",
  "main_risk": "A high-volume move can reverse; the detector does not identify a fundamental cause.",
  "technical_vs_fundamental": "technical"
}
```

## Limitations

Headline classification is rule-based and cannot verify every underlying claim.
OKX volume represents one venue, not the entire market. RSS feeds can be delayed or
unavailable. No performance, predictive-accuracy, or profitability claim is made.
External delivery is at-least-once: after a rare partial transport failure, retrying
may duplicate a chunk already accepted by Telegram or one channel. Digest artifacts
remain the authoritative complete record.
Review the evidence before acting and use only capital you can afford to lose.

This software is educational decision support, not financial advice. Licensed under
the MIT License.
