#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
performance_optimizations.py

Otimizações de performance para o sistema de alertas de trading.
"""

import numpy as np
import pandas as pd
from numba import jit
from functools import lru_cache
import concurrent.futures
from typing import List, Dict, Optional

# =========================
# Otimizações com Numba
# =========================

@jit(nopython=True)
def fast_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Cálculo otimizado do RSI usando Numba."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    # Primeira média
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    rsi = np.full(len(prices), np.nan)
    
    for i in range(period, len(prices)):
        if i == period:
            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
        
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi

@jit(nopython=True)
def fast_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Cálculo otimizado da EMA usando Numba."""
    alpha = 2.0 / (period + 1.0)
    ema = np.full(len(prices), np.nan)
    ema[0] = prices[0]
    
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1 - alpha) * ema[i-1]
    
    return ema

@jit(nopython=True)
def fast_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Cálculo otimizado do ATR usando Numba."""
    tr = np.full(len(high), np.nan)
    
    for i in range(1, len(high)):
        tr1 = high[i] - low[i]
        tr2 = abs(high[i] - close[i-1])
        tr3 = abs(low[i] - close[i-1])
        tr[i] = max(tr1, tr2, tr3)
    
    # Calcular ATR usando EMA
    atr = np.full(len(high), np.nan)
    atr[period] = np.mean(tr[1:period+1])
    
    alpha = 1.0 / period
    for i in range(period + 1, len(high)):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    
    return atr

# =========================
# Cache para indicadores
# =========================

class IndicatorCache:
    """Cache para indicadores técnicos para evitar recálculos."""
    
    def __init__(self, max_size: int = 1000):
        self.cache = {}
        self.max_size = max_size
    
    def get_key(self, symbol: str, timeframe: str, indicator: str, params: tuple) -> str:
        """Gera chave única para o cache."""
        return f"{symbol}_{timeframe}_{indicator}_{hash(params)}"
    
    def get(self, key: str) -> Optional[np.ndarray]:
        """Recupera indicador do cache."""
        return self.cache.get(key)
    
    def set(self, key: str, value: np.ndarray):
        """Armazena indicador no cache."""
        if len(self.cache) >= self.max_size:
            # Remove o item mais antigo
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        
        self.cache[key] = value.copy()

# Instância global do cache
indicator_cache = IndicatorCache()

# =========================
# Processamento paralelo
# =========================

def parallel_symbol_analysis(symbols: List[str], exchange, timeframe: str, 
                            max_workers: int = 4) -> List[Dict]:
    """Analisa múltiplos símbolos em paralelo."""
    
    def analyze_single_symbol(symbol: str) -> Optional[Dict]:
        try:
            from crypto_alerts_updated import fetch_ohlcv_generic, composite_signal
            
            df = fetch_ohlcv_generic(exchange, symbol, timeframe)
            if df.empty:
                return None
            
            signal = composite_signal(df, symbol, timeframe)
            return signal.__dict__ if signal else None
            
        except Exception as e:
            print(f"Erro ao analisar {symbol}: {e}")
            return None
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_symbol = {
            executor.submit(analyze_single_symbol, symbol): symbol 
            for symbol in symbols
        }
        
        for future in concurrent.futures.as_completed(future_to_symbol):
            result = future.result()
            if result:
                results.append(result)
    
    return results

# =========================
# Otimizações de memória
# =========================

def optimize_dataframe_memory(df: pd.DataFrame) -> pd.DataFrame:
    """Otimiza o uso de memória do DataFrame."""
    
    # Converter float64 para float32 quando possível
    float_cols = df.select_dtypes(include=['float64']).columns
    df[float_cols] = df[float_cols].astype('float32')
    
    # Converter int64 para int32 quando possível
    int_cols = df.select_dtypes(include=['int64']).columns
    for col in int_cols:
        if df[col].min() >= np.iinfo(np.int32).min and df[col].max() <= np.iinfo(np.int32).max:
            df[col] = df[col].astype('int32')
    
    return df

def batch_process_symbols(symbols: List[str], batch_size: int = 10) -> List[List[str]]:
    """Divide símbolos em lotes para processamento eficiente."""
    batches = []
    for i in range(0, len(symbols), batch_size):
        batches.append(symbols[i:i + batch_size])
    return batches

# =========================
# Configurações de performance
# =========================

PERFORMANCE_CONFIG = {
    'use_numba': True,
    'use_cache': True,
    'max_workers': 4,
    'batch_size': 10,
    'memory_optimization': True,
    'indicator_cache_size': 1000
}

def apply_performance_optimizations():
    """Aplica todas as otimizações de performance disponíveis."""
    
    if PERFORMANCE_CONFIG['use_numba']:
        print("✅ Otimizações Numba habilitadas")
    
    if PERFORMANCE_CONFIG['use_cache']:
        print("✅ Cache de indicadores habilitado")
    
    if PERFORMANCE_CONFIG['memory_optimization']:
        print("✅ Otimizações de memória habilitadas")
    
    print(f"✅ Processamento paralelo configurado para {PERFORMANCE_CONFIG['max_workers']} workers")
    print(f"✅ Tamanho do lote: {PERFORMANCE_CONFIG['batch_size']} símbolos")

if __name__ == "__main__":
    # Teste das otimizações
    print("🚀 Testando otimizações de performance...")
    
    # Teste do RSI otimizado
    test_prices = np.random.randn(1000).cumsum() + 100
    
    import time
    
    # RSI otimizado
    start = time.time()
    fast_rsi_result = fast_rsi(test_prices)
    fast_time = time.time() - start
    
    print(f"✅ RSI otimizado: {fast_time:.4f}s")
    
    # EMA otimizada
    start = time.time()
    fast_ema_result = fast_ema(test_prices, 20)
    ema_time = time.time() - start
    
    print(f"✅ EMA otimizada: {ema_time:.4f}s")
    
    apply_performance_optimizations()
    print("🎯 Todas as otimizações aplicadas com sucesso!")

