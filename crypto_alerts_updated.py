#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_alerts.py  —  OKX-Ready

Sistema de varredura, pontuação e alertas para criptomoedas com regras específicas da OKX:
- Conexão via ccxt com OKX (spot por padrão), leitura de OHLCV
- Indicadores clássicos (EMA/SMA/RSI/MACD/Bollinger/ATR/ROC/Donchian)
- Padrões: divergência (preço vs RSI/MACD), breakout com confirmação de volume
- Score composto => Strong Buy/Buy/Hold/Sell/Strong Sell
- Gestão de risco: stop inicial por ATR, trailing por ATR, position sizing por risco fixo
- **Regras da OKX**: precisão de preço/quantidade, tamanho mínimo, custo mínimo (min notional),
  taxas maker/taker, e (opcional) **envio de ordens** com checagens de conformidade
- (Opcional) Modelos ARIMA (retorno esperado) e GARCH (vol esperada) se statsmodels/arch estiverem instalados
- (Opcional) Carregamento de parâmetros via config.yaml

Uso rápido:
  pip install -r requirements.txt
  python crypto_alerts.py --scan_top --top_n 30 --timeframe 1h

Para enviar ordens reais (cuidado!):
  export OKX_API_KEY=... OKX_API_SECRET=... OKX_API_PASSWORD=...
  python crypto_alerts.py --symbols "BTC/USDT,ETH/USDT" --timeframe 1h --capital 2200 --place_orders \
                          --order_type limit --risk_per_trade 0.01
"""
import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd

# Dependências opcionais
_HAVE_SM = False
_HAVE_ARCH = False
try:
    from statsmodels.tsa.arima.model import ARIMA
    _HAVE_SM = True
except Exception:
    pass

try:
    from arch import arch_model
    _HAVE_ARCH = True
except Exception:
    pass

# ccxt para dados/ordens
try:
    import ccxt
except Exception:
    ccxt = None

try:
    import yaml
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False


# =========================
# Indicadores
# =========================

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    gain = up.ewm(alpha=1/n, adjust=False).mean()
    loss = down.ewm(alpha=1/n, adjust=False).mean()
    rs = gain / (loss.replace(0.0, np.nan))
    rsi_v = 100 - (100 / (1 + rs))
    return rsi_v.fillna(50)

def macd(close: pd.Series, fast=12, slow=26, signal=9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std()
    upper = mid + k * std
    lower = mid - k * std
    return lower, mid, upper

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"]; low = df["low"]; close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev).abs(),
        (low - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def roc(close: pd.Series, n: int = 12) -> pd.Series:
    return (close - close.shift(n)) / close.shift(n) * 100.0

def donchian_channel(df: pd.DataFrame, n: int = 20) -> Tuple[pd.Series, pd.Series]:
    upper = df["high"].rolling(n, min_periods=n).max()
    lower = df["low"].rolling(n, min_periods=n).min()
    return lower, upper

# Novo indicador: Stochastic Oscillator
def stochastic_oscillator(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
    highest_high = df['high'].rolling(k_period).max()
    lowest_low = df['low'].rolling(k_period).min()
    # %K
    k_line = 100 * ((df['close'] - lowest_low) / (highest_high - lowest_low))
    # %D (SMA de %K)
    d_line = sma(k_line, d_period)
    return k_line, d_line

# Novo indicador: Awesome Oscillator (AO)
def awesome_oscillator(df: pd.DataFrame, fast_period: int = 5, slow_period: int = 34) -> pd.Series:
    median_price = (df['high'] + df['low']) / 2
    ao = sma(median_price, fast_period) - sma(median_price, slow_period)
    return ao


# =========================
# Divergências (preço vs RSI/MACD/Stochastic)
# =========================

def _pivots(s: pd.Series, window: int = 5) -> Tuple[pd.Series, pd.Series]:
    rollmax = s.rolling(window, min_periods=window).max()
    rollmin = s.rolling(window, min_periods=window).min()
    tops = s.where(s == rollmax)
    bottoms = s.where(s == rollmin)
    return tops, bottoms

def detect_divergence(price: pd.Series, indicator: pd.Series, window=10) -> pd.Series:
    price_tops, price_bottoms = _pivots(price, window)
    ind_tops, ind_bottoms = _pivots(indicator, window)
    signal = pd.Series(0, index=price.index, dtype='int')

    # Divergência de baixa (preço faz topo mais alto, indicador faz topo mais baixo)
    top_idx = price_tops.dropna().index
    if len(top_idx) >= 2:
        # Pegar os dois últimos topos de preço
        p_t2_idx, p_t1_idx = top_idx[-1], top_idx[-2]
        p_t2, p_t1 = price.loc[p_t2_idx], price.loc[p_t1_idx]

        # Encontrar os valores correspondentes do indicador nos mesmos índices de tempo
        i_t2, i_t1 = indicator.loc[p_t2_idx], indicator.loc[p_t1_idx]

        if (p_t2 > p_t1) and (i_t2 < i_t1):
            signal.loc[p_t2_idx] = -2 # Divergência de baixa forte

    # Divergência de alta (preço faz fundo mais baixo, indicador faz fundo mais alto)
    bot_idx = price_bottoms.dropna().index
    if len(bot_idx) >= 2:
        # Pegar os dois últimos fundos de preço
        p_b2_idx, p_b1_idx = bot_idx[-1], bot_idx[-2]
        p_b2, p_b1 = price.loc[p_b2_idx], price.loc[p_b1_idx]

        # Encontrar os valores correspondentes do indicador nos mesmos índices de tempo
        i_b2, i_b1 = indicator.loc[p_b2_idx], indicator.loc[p_b1_idx]

        if (p_b2 < p_b1) and (i_b2 > i_b1):
            signal.loc[p_b2_idx] = 2 # Divergência de alta forte

    return signal


# =========================
# ARIMA + GARCH (opcionais)
# =========================

def arima_expected_return(close: pd.Series, order=(1,0,1), horizon: int = 1) -> Optional[float]:
    if not _HAVE_SM or close.dropna().shape[0] < 50:
        return None
    try:
        returns = np.log(close).diff().dropna()
        model = ARIMA(returns, order=order)
        res = model.fit(method_kwargs={"warn_convergence": False}, disp=0)
        forecast = res.forecast(steps=horizon)
        return float(forecast.iloc[-1])
    except Exception:
        return None

def garch_expected_vol(close: pd.Series, p=1, q=1, horizon: int = 1) -> Optional[float]:
    if not _HAVE_ARCH or close.dropna().shape[0] < 300:
        return None
    try:
        returns = 100 * np.log(close).diff().dropna()
        am = arch_model(returns, vol='GARCH', p=p, q=q, dist='normal', mean='Zero')
        res = am.fit(disp="off")
        fcast = res.forecast(horizon=horizon)
        var = fcast.variance.values[-1, -1]
        return math.sqrt(var) / 100.0
    except Exception:
        return None


# =========================
# OKX: conexão, regras, fees, precisão, ordens
# =========================

def get_exchange_client(exchange_id: str) -> 'ccxt.Exchange':
    if ccxt is None:
        raise RuntimeError("ccxt não está instalado. Instale com: pip install ccxt")
    if exchange_id not in ccxt.exchanges:
        raise ValueError(f"Exchange {exchange_id} não suportada pelo ccxt.")

    exchange_class = getattr(ccxt, exchange_id)
    params = {
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},  # spot por padrão
    }

    # Carregar credenciais específicas para a exchange
    api_key = os.getenv(f"{exchange_id.upper()}_API_KEY")
    api_secret = os.getenv(f"{exchange_id.upper()}_API_SECRET")
    api_password = os.getenv(f"{exchange_id.upper()}_API_PASSWORD")

    if api_key and api_secret:
        params.update({
            "apiKey": api_key,
            "secret": api_secret,
        })
        if api_password: # OKX usa password, outras não
            params["password"] = api_password

    ex = exchange_class(params)
    ex.load_markets()
    return ex

def fetch_ohlcv_generic(ex, symbol: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/Sao_Paulo")
    df.set_index("ts", inplace=True)
    return df

def get_top_symbols_generic(ex, quote="USDT", top_n=20) -> List[str]:
    tickers = ex.fetch_tickers()
    syms = []
    for sym, tk in tickers.items():
        try:
            m = ex.market(sym)
        except Exception:
            continue
        if not m.get("spot", False):
            continue
        if quote and f"/{quote}" not in sym:
            continue
        # volume em moeda de cotação
        vol_quote = tk.get("quoteVolume")
        if vol_quote is None:
            vol_quote = (tk.get("baseVolume", 0) or 0) * (tk.get("last", 0) or 0)
        syms.append((sym, float(vol_quote or 0)))
    syms = [x for x in syms if x[1] > 0]
    syms.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in syms[:top_n]]

def get_market_rules_generic(ex, symbol: str) -> Dict:
    m = ex.market(symbol)
    limits = m.get("limits", {})
    precision = m.get("precision", {})
    maker = m.get("maker", None)
    taker = m.get("taker", None)

    # Tentativa de buscar taxas específicas por símbolo
    fee_maker = maker
    fee_taker = taker
    try:
        fees = ex.fetch_trading_fee(symbol)
        fee_maker = fees.get("maker", fee_maker)
        fee_taker = fees.get("taker", fee_taker)
    except Exception:
        pass

    return {
        "price_precision": precision.get("price"),
        "amount_precision": precision.get("amount"),
        "min_amount": (limits.get("amount", {}) or {}).get("min", None),
        "max_amount": (limits.get("amount", {}) or {}).get("max", None),
        "min_cost": (limits.get("cost", {}) or {}).get("min", None),  # notional mínimo
        "min_price": (limits.get("price", {}) or {}).get("min", None),
        "max_price": (limits.get("price", {}) or {}).get("max", None),
        "fee_maker": fee_maker,
        "fee_taker": fee_taker,
    }

def _round_step(value: float, precision: Optional[int]) -> float:
    if precision is None:
        return float(value)
    return float(round(value, int(precision)))

def conform_order_generic(ex, symbol: str, price: float, amount: float, rules: Dict) -> Tuple[float, float, List[str]]:
    """Aplica precisão e mínimos da exchange. Retorna (price, amount, warnings)."""
    warns = []
    p = _round_step(price, rules.get("price_precision"))
    a = _round_step(amount, rules.get("amount_precision"))

    min_amount = rules.get("min_amount")
    if min_amount and a < min_amount:
        a = min_amount
        warns.append(f"Ajustado amount para min_amount={min_amount}")

    min_cost = rules.get("min_cost")
    if min_cost and (p * a) < min_cost:
        # aumenta quantidade para atingir notional mínimo
        a = _round_step(max(a, min_cost / p), rules.get("amount_precision"))
        warns.append(f"Ajustado amount para cumprir min_cost≈{min_cost:.8f}")

    return p, a, warns

def estimate_fees_generic(ex, symbol: str, price: float, amount: float, rules: Dict, order_type: str = "limit") -> float:
    notional = price * amount
    rate = rules.get("fee_maker") if order_type == "limit" else rules.get("taker")
    if rate is None:
        # fallback típico OKX retail (podem variar por tier)
        rate = 0.0008 if order_type == "limit" else 0.001
    return notional * float(rate)


# =========================
# Sinais e plano
# =========================

@dataclass
class SignalResult:
    symbol: str
    timeframe: str
    score: float
    label: str
    entry_hint: str
    stop_loss: Optional[float]
    trail_atr_mult: float
    take_profit: Optional[float]
    details: Dict[str, float]

def composite_signal(df: pd.DataFrame, symbol: str, timeframe: str,
                     risk_per_trade: float = 0.01,
                     atr_mult_sl: float = 2.0,
                     atr_mult_trail: float = 3.0) -> Optional[SignalResult]:
    if df.shape[0] < 60:
        return None

    close = df['close']; high = df['high']; low = df['low']; vol = df['volume']

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    rsi14 = rsi(close, 14)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_lower, bb_mid, bb_upper = bollinger(close)
    atr14 = atr(df, 14)
    roc12 = roc(close, 12)
    dlow, dup = donchian_channel(df, 20)
    stoch_k, stoch_d = stochastic_oscillator(df)
    ao = awesome_oscillator(df)

    div_rsi = detect_divergence(close, rsi14, window=10).iloc[-1]
    div_macd = detect_divergence(close, macd_line, window=10).iloc[-1]
    div_stoch = detect_divergence(close, stoch_k, window=10).iloc[-1]

    vol_sma20 = sma(vol, 20)
    breakout_up = (close.iloc[-1] > dup.iloc[-2]) and (vol.iloc[-1] > 1.5 * vol_sma20.iloc[-1])
    breakout_down = (close.iloc[-1] < dlow.iloc[-2]) and (vol.iloc[-1] > 1.5 * vol_sma20.iloc[-1])

    trend_score = 0
    if ema20.iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1]:
        trend_score += 2
    elif ema20.iloc[-1] > ema50.iloc[-1]:
        trend_score += 1
    elif ema20.iloc[-1] < ema50.iloc[-1] < ema200.iloc[-1]:
        trend_score -= 2
    elif ema20.iloc[-1] < ema50.iloc[-1]:
        trend_score -= 1

    mom_score = 0
    if rsi14.iloc[-1] > 55 and macd_line.iloc[-1] > macd_sig.iloc[-1] and macd_hist.iloc[-1] > macd_hist.iloc[-2]:
        mom_score += 1
    if roc12.iloc[-1] > 0:
        mom_score += 0.5
    if stoch_k.iloc[-1] > stoch_d.iloc[-1] and stoch_k.iloc[-1] < 80: # Estocástico cruzando para cima e não sobrecomprado
        mom_score += 0.5
    if ao.iloc[-1] > ao.iloc[-2] and ao.iloc[-1] > 0: # AO verde e crescendo
        mom_score += 0.5

    if rsi14.iloc[-1] < 45 and macd_line.iloc[-1] < macd_sig.iloc[-1] and macd_hist.iloc[-1] < macd_hist.iloc[-2]:
        mom_score -= 1
    if roc12.iloc[-1] < 0:
        mom_score -= 0.5
    if stoch_k.iloc[-1] < stoch_d.iloc[-1] and stoch_k.iloc[-1] > 20: # Estocástico cruzando para baixo e não sobrevendido
        mom_score -= 0.5
    if ao.iloc[-1] < ao.iloc[-2] and ao.iloc[-1] < 0: # AO vermelho e caindo
        mom_score -= 0.5

    atrp = (atr14.iloc[-1] / close.iloc[-1]) * 100
    vol_score = 0
    if 1.0 <= atrp <= 6.0:
        vol_score += 0.5
    elif atrp > 10.0:
        vol_score -= 0.5

    div_score = 0
    div_score += (2 if div_rsi == 2 else 0)
    div_score -= (2 if div_rsi == -2 else 0)
    div_score += (1 if div_macd == 2 else 0)
    div_score -= (1 if div_macd == -2 else 0)
    div_score += (1 if div_stoch == 2 else 0)
    div_score -= (1 if div_stoch == -2 else 0)

    brk_score = 0
    if breakout_up: brk_score += 2
    if breakout_down: brk_score -= 2

    ar_exp = arima_expected_return(close)
    ga_vol = garch_expected_vol(close)
    arga_score = 0
    if ar_exp is not None and ga_vol is not None and ga_vol > 0:
        arga = ar_exp / ga_vol
        arga_score = max(-2, min(2, float(arga)))
    elif ar_exp is not None:
        arga_score = max(-1, min(1, float(ar_exp) * 100))

    score = (trend_score * 1.5) + (mom_score * 1.2) + (vol_score * 0.8) + (div_score * 1.5) + (brk_score * 2.0) + (arga_score * 0.5)

    if score >= 3:
        label = "Strong Buy"
    elif score >= 1.5:
        label = "Buy"
    elif score <= -3:
        label = "Strong Sell"
    elif score <= -1.5:
        label = "Sell"
    else:
        label = "Hold"

    entry_hint = ""
    if breakout_up:
        entry_hint = "Entrada por rompimento (Donchian) com volume acima da média."
    elif rsi14.iloc[-1] > 55 and close.iloc[-1] > ema20.iloc[-1]:
        entry_hint = "Entrada por continuação: pullback na EMA20 em tendência de alta."
    elif div_rsi == 2:
        entry_hint = "Entrada por divergência altista confirmada (RSI)."
    elif div_stoch == 2:
        entry_hint = "Entrada por divergência altista confirmada (Stochastic)."
    elif label in ("Sell", "Strong Sell"):
        entry_hint = "Evitar novas compras; considerar redução."

    stop_loss = round(float(close.iloc[-1] - 2.0 * atr14.iloc[-1]), 10) if label in ("Buy", "Strong Buy") else None
    take_profit = None
    if stop_loss is not None:
        R = close.iloc[-1] - stop_loss
        take_profit = round(float(close.iloc[-1] + 2 * R), 10)

    details = {
        "trend_score": float(trend_score),
        "mom_score": float(mom_score),
        "vol_score": float(vol_score),
        "div_score": float(div_score),
        "breakout_score": float(brk_score),
        "arima_garch_score": float(arga_score),
        "ATR%": float(atrp),
        "RSI": float(rsi14.iloc[-1]),
        "MACD_hist": float(macd_hist.iloc[-1]),
        "Stoch_K": float(stoch_k.iloc[-1]),
        "Stoch_D": float(stoch_d.iloc[-1]),
        "AO": float(ao.iloc[-1]),
        "close": float(close.iloc[-1]),
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "ema200": float(ema200.iloc[-1]),
    }
    return SignalResult(
        symbol=symbol,
        timeframe=timeframe,
        score=float(score),
        label=label,
        entry_hint=entry_hint,
        stop_loss=stop_loss,
        trail_atr_mult=3.0,
        take_profit=take_profit,
        details=details
    )


@dataclass
class PositionPlan:
    symbol: str
    side: str
    entry: float
    stop: float
    take_profit: Optional[float]
    size_in_units: float
    max_risk_usd: float
    est_fees: float

def build_position_plan_generic(signal: SignalResult, capital_usd: float, rules: Dict,
                            risk_per_trade: float = 0.01, order_type: str = "limit") -> Optional[PositionPlan]:
    if signal.stop_loss is None or signal.label not in ("Buy", "Strong Buy"):
        return None
    entry = signal.details["close"]
    stop = signal.stop_loss
    R = entry - stop
    if R <= 0:
        return None
    max_risk = capital_usd * risk_per_trade
    size = max_risk / R

    # Conformar com regras (precisão, min_amount, min_cost)
    entry_c, size_c, warns = conform_order_generic(None, signal.symbol, entry, size, rules)
    fees = estimate_fees_generic(None, signal.symbol, entry_c, size_c, rules, order_type=order_type)

    return PositionPlan(
        symbol=signal.symbol,
        side="long",
        entry=round(entry_c, 10),
        stop=round(stop, 10),
        take_profit=round(signal.take_profit, 10) if signal.take_profit else None,
        size_in_units=round(size_c, 10),
        max_risk_usd=round(max_risk, 10),
        est_fees=round(fees, 10)
    )


# =========================
# Funções principais
# =========================

def scan_and_alert(
    ex,
    symbols: List[str],
    timeframe: str,
    capital: float,
    risk_per_trade: float,
    place_orders: bool,
    order_type: str,
    verbose: bool = False,
):
    print(f"\n{'='*50}")
    print(f"Iniciando varredura para {len(symbols)} símbolos no timeframe {timeframe}")
    print(f"Capital: ${capital:.2f}, Risco por trade: {risk_per_trade*100:.2f}%")
    print(f"{'='*50}\n")

    results: List[SignalResult] = []
    for i, symbol in enumerate(symbols):
        print(f"[{i+1}/{len(symbols)}] Processando {symbol}...")
        try:
            df = fetch_ohlcv_generic(ex, symbol, timeframe)
            if df.empty:
                print(f"  Dados OHLCV vazios para {symbol}. Pulando.")
                continue

            signal = composite_signal(df, symbol, timeframe, risk_per_trade)
            if signal:
                results.append(signal)
                if verbose:
                    print(f"  Sinal para {symbol}: {signal.label} (Score: {signal.score:.2f})")
                    print(f"    Entrada: {signal.entry_hint}")
                    if signal.stop_loss: print(f"    Stop Loss: {signal.stop_loss:.8f}")
                    if signal.take_profit: print(f"    Take Profit: {signal.take_profit:.8f}")
                    print(f"    Detalhes: {signal.details}")

                if place_orders and signal.label in ("Strong Buy", "Buy"):
                    rules = get_market_rules_generic(ex, symbol)
                    plan = build_position_plan_generic(signal, capital, rules, risk_per_trade, order_type)
                    if plan:
                        print(f"  >>> Plano de Posição para {symbol}:")
                        print(f"      Entrada: {plan.entry:.8f}, Stop: {plan.stop:.8f}, TP: {plan.take_profit:.8f}")
                        print(f"      Tamanho: {plan.size_in_units:.8f} unidades, Risco Max: ${plan.max_risk_usd:.2f}")
                        print(f"      Taxas Estimadas: ${plan.est_fees:.8f}")
                        # Aqui você implementaria o envio da ordem real
                        # ex.create_order(plan.symbol, order_type, plan.side, plan.size_in_units, plan.entry)
                        print(f"      [SIMULADO] Ordem de {order_type} para {plan.symbol} enviada.")
                    else:
                        print(f"  Não foi possível gerar plano de posição para {symbol}.")

        except Exception as e:
            print(f"  Erro ao processar {symbol}: {e}")
        time.sleep(0.5) # Pequeno delay para evitar rate limit

    print(f"\n{'='*50}")
    print("Resumo dos Sinais:")
    print(f"{'='*50}")
    if not results:
        print("Nenhum sinal gerado.")
    else:
        for res in sorted(results, key=lambda x: x.score, reverse=True):
            print(f"- {res.symbol} ({res.timeframe}): {res.label} (Score: {res.score:.2f})")
            print(f"  Entrada: {res.entry_hint}")
            if res.stop_loss: print(f"  Stop Loss: {res.stop_loss:.8f}")
            if res.take_profit: print(f"  Take Profit: {res.take_profit:.8f}")
            print(f"  Detalhes: {res.details}")
            print("-"*20)


def main():
    parser = argparse.ArgumentParser(
        description="Sistema de alertas de criptomoedas com análise técnica e regras da OKX."
    )
    parser.add_argument("--symbols", type=str, help="Símbolos para analisar (ex: BTC/USDT,ETH/USDT)")
    parser.add_argument("--timeframe", type=str, default="1h", help="Timeframe (ex: 1h, 4h, 1d)")
    parser.add_argument("--limit", type=int, default=500, help="Número de velas OHLCV a buscar")
    parser.add_argument("--capital", type=float, default=1000.0, help="Capital total em USD para gestão de risco")
    parser.add_argument("--risk_per_trade", type=float, default=0.01, help="Percentual de risco por trade (ex: 0.01 para 1%)")
    parser.add_argument("--place_orders", action="store_true", help="Se presente, simula o envio de ordens reais (requer chaves de API)")
    parser.add_argument("--order_type", type=str, default="limit", help="Tipo de ordem (limit ou market)")
    parser.add_argument("--scan_top", action="store_true", help="Se presente, escaneia os top N símbolos por volume")
    parser.add_argument("--top_n", type=int, default=20, help="Número de símbolos a escanear se --scan_top for usado")
    parser.add_argument("--quote", type=str, default="USDT", help="Moeda de cotação para --scan_top (ex: USDT, BUSD)")
    parser.add_argument("--verbose", action="store_true", help="Exibe detalhes de cada símbolo processado")
    parser.add_argument("--config", type=str, help="Caminho para o arquivo config.yaml")

    args = parser.parse_args()

    if args.config:
        if not _HAVE_YAML:
            print("Erro: PyYAML não está instalado. Instale com: pip install pyyaml")
            sys.exit(1)
        with open(args.config, 'r') as f:
            config = yaml.safe_load(f)
            for k, v in config.items():
                setattr(args, k, v)

    if ccxt is None:
        print("Erro: ccxt não está instalado. Instale com: pip install ccxt")
        sys.exit(1)

    ex = get_okx_client()

    symbols_to_scan: List[str] = []
    if args.scan_top:
        print(f"Buscando os top {args.top_n} símbolos por volume em {args.quote}...")
        symbols_to_scan = okx_top_symbols(ex, args.quote, args.top_n)
        print(f"Encontrados {len(symbols_to_scan)} símbolos: {', '.join(symbols_to_scan)}")
    elif args.symbols:
        symbols_to_scan = [s.strip() for s in args.symbols.split(',')]
    else:
        print("Erro: Você deve especificar --symbols ou usar --scan_top.")
        parser.print_help()
        sys.exit(1)

    scan_and_alert(
        ex,
        symbols_to_scan,
        args.timeframe,
        args.capital,
        args.risk_per_trade,
        args.place_orders,
        args.order_type,
        args.verbose,
    )

if __name__ == "__main__":
    main()




# =========================
# Backtesting
# =========================

def run_backtest(df: pd.DataFrame, strategy_func, initial_capital: float = 1000.0, risk_per_trade: float = 0.01) -> Dict:
    positions = {}
    trades = []
    capital = initial_capital
    equity_curve = [initial_capital]

    for i in range(len(df)):
        current_df = df.iloc[:i+1]
        if len(current_df) < 60: # Need enough data for indicators
            equity_curve.append(capital)
            continue

        signal_result = strategy_func(current_df, "", "") # Symbol and timeframe are placeholders

        # Simple long-only strategy for demonstration
        if signal_result and signal_result.label in ["Buy", "Strong Buy"] and not positions:
            # Calculate position size based on risk_per_trade and stop_loss
            if signal_result.stop_loss and signal_result.stop_loss < current_df["close"].iloc[-1]:
                risk_amount = capital * risk_per_trade
                price_diff = current_df["close"].iloc[-1] - signal_result.stop_loss
                if price_diff > 0:
                    amount_to_buy = risk_amount / price_diff
                    # Assume we can buy fractional amounts for simplicity in backtesting
                    cost = amount_to_buy * current_df["close"].iloc[-1]
                    if cost < capital:
                        positions = {
                            "entry_price": current_df["close"].iloc[-1],
                            "amount": amount_to_buy,
                            "stop_loss": signal_result.stop_loss,
                            "take_profit": signal_result.take_profit
                        }
                        capital -= cost
                        trades.append({"type": "BUY", "price": positions["entry_price"], "amount": positions["amount"], "time": current_df.index[-1]})

        elif positions:
            # Check stop loss or take profit
            if current_df["low"].iloc[-1] <= positions["stop_loss"]:
                # Sell at stop loss
                sell_price = positions["stop_loss"]
                capital += positions["amount"] * sell_price
                trades.append({"type": "SELL", "price": sell_price, "amount": positions["amount"], "time": current_df.index[-1]})
                positions = {}
            elif positions["take_profit"] and current_df["high"].iloc[-1] >= positions["take_profit"]:
                # Sell at take profit
                sell_price = positions["take_profit"]
                capital += positions["amount"] * sell_price
                trades.append({"type": "SELL", "price": sell_price, "amount": positions["amount"], "time": current_df.index[-1]})
                positions = {}

        current_equity = capital + (positions["amount"] * current_df["close"].iloc[-1] if positions else 0)
        equity_curve.append(current_equity)

    # If position is still open at the end, close it
    if positions:
        sell_price = df["close"].iloc[-1]
        capital += positions["amount"] * sell_price
        trades.append({"type": "SELL", "price": sell_price, "amount": positions["amount"], "time": df.index[-1]})

    final_equity = capital + (positions["amount"] * df["close"].iloc[-1] if positions else 0)
    total_return = (final_equity - initial_capital) / initial_capital

    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "total_return": total_return,
        "trades": trades,
        "equity_curve": equity_curve
    }



# =========================
# Sistema de Alertas
# =========================

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

def send_email_alert(signal: SignalResult, smtp_config: Dict):
    """Envia alerta por e-mail."""
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_config['from_email']
        msg['To'] = smtp_config['to_email']
        msg['Subject'] = f"Alerta de Trading: {signal.symbol} - {signal.label}"
        
        body = f"""
        Novo sinal de trading detectado:
        
        Símbolo: {signal.symbol}
        Timeframe: {signal.timeframe}
        Sinal: {signal.label}
        Score: {signal.score:.2f}
        
        Entrada: {signal.entry_hint}
        Stop Loss: {signal.stop_loss if signal.stop_loss else 'N/A'}
        Take Profit: {signal.take_profit if signal.take_profit else 'N/A'}
        
        Detalhes técnicos:
        {signal.details}
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP(smtp_config['smtp_server'], smtp_config['smtp_port'])
        server.starttls()
        server.login(smtp_config['from_email'], smtp_config['password'])
        text = msg.as_string()
        server.sendmail(smtp_config['from_email'], smtp_config['to_email'], text)
        server.quit()
        
        print(f"Alerta por e-mail enviado para {signal.symbol}")
        return True
    except Exception as e:
        print(f"Erro ao enviar e-mail: {e}")
        return False

def send_telegram_alert(signal: SignalResult, telegram_config: Dict):
    """Envia alerta via Telegram."""
    try:
        bot_token = telegram_config['bot_token']
        chat_id = telegram_config['chat_id']
        
        message = f"""
🚨 *Alerta de Trading* 🚨

📊 *Símbolo:* {signal.symbol}
⏰ *Timeframe:* {signal.timeframe}
📈 *Sinal:* {signal.label}
⭐ *Score:* {signal.score:.2f}

💡 *Entrada:* {signal.entry_hint}
🛑 *Stop Loss:* {signal.stop_loss if signal.stop_loss else 'N/A'}
🎯 *Take Profit:* {signal.take_profit if signal.take_profit else 'N/A'}

📋 *Detalhes:*
```
{signal.details}
```
        """
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        
        response = requests.post(url, data=data)
        if response.status_code == 200:
            print(f"Alerta Telegram enviado para {signal.symbol}")
            return True
        else:
            print(f"Erro ao enviar Telegram: {response.text}")
            return False
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")
        return False

def send_alerts(signal: SignalResult, alert_config: Dict):
    """Envia alertas através dos canais configurados."""
    if signal.label in ["Strong Buy", "Buy", "Strong Sell", "Sell"]:
        if alert_config.get('email_enabled', False):
            send_email_alert(signal, alert_config['email'])
        
        if alert_config.get('telegram_enabled', False):
            send_telegram_alert(signal, alert_config['telegram'])

# =========================
# Gerenciamento de Credenciais
# =========================

def load_credentials_from_env() -> Dict:
    """Carrega credenciais de múltiplas exchanges das variáveis de ambiente."""
    exchanges = ['okx', 'binance', 'coinbase', 'kraken', 'bybit']
    credentials = {}
    
    for exchange in exchanges:
        api_key = os.getenv(f"{exchange.upper()}_API_KEY")
        api_secret = os.getenv(f"{exchange.upper()}_API_SECRET")
        api_password = os.getenv(f"{exchange.upper()}_API_PASSWORD")
        
        if api_key and api_secret:
            credentials[exchange] = {
                'api_key': api_key,
                'api_secret': api_secret,
                'api_password': api_password
            }
    
    return credentials

def load_alert_config_from_env() -> Dict:
    """Carrega configurações de alerta das variáveis de ambiente."""
    config = {
        'email_enabled': os.getenv('EMAIL_ALERTS_ENABLED', 'false').lower() == 'true',
        'telegram_enabled': os.getenv('TELEGRAM_ALERTS_ENABLED', 'false').lower() == 'true'
    }
    
    if config['email_enabled']:
        config['email'] = {
            'smtp_server': os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
            'smtp_port': int(os.getenv('SMTP_PORT', '587')),
            'from_email': os.getenv('FROM_EMAIL'),
            'to_email': os.getenv('TO_EMAIL'),
            'password': os.getenv('EMAIL_PASSWORD')
        }
    
    if config['telegram_enabled']:
        config['telegram'] = {
            'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'chat_id': os.getenv('TELEGRAM_CHAT_ID')
        }
    
    return config

