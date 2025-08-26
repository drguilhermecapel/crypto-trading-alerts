# Documentação Técnica - Crypto Trading Alerts

## Índice

1. [Arquitetura do Sistema](#arquitetura-do-sistema)
2. [Indicadores Técnicos](#indicadores-técnicos)
3. [Sistema de Pontuação](#sistema-de-pontuação)
4. [Gestão de Risco](#gestão-de-risco)
5. [Integração com Exchanges](#integração-com-exchanges)
6. [Sistema de Alertas](#sistema-de-alertas)
7. [Backtesting](#backtesting)
8. [Configuração Avançada](#configuração-avançada)
9. [Otimizações de Performance](#otimizações-de-performance)
10. [Troubleshooting](#troubleshooting)

## Arquitetura do Sistema

### Visão Geral

O sistema é composto por módulos independentes que trabalham em conjunto:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Data Source   │    │   Indicators    │    │   Signals       │
│   (Exchanges)   │───▶│   (Technical)   │───▶│   (Composite)   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Risk Mgmt     │    │   Backtesting   │    │   Alerts        │
│   (Position)    │    │   (Validation)  │    │   (Notification)│
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### Componentes Principais

#### 1. Módulo de Dados (`fetch_ohlcv_generic`)
- Conecta com múltiplas exchanges via CCXT
- Normaliza dados OHLCV
- Cache inteligente para otimização

#### 2. Módulo de Indicadores Técnicos
- RSI, MACD, Bollinger Bands, ATR, Stochastic
- Detecção de divergências
- Cálculos otimizados com Numba

#### 3. Sistema de Sinais (`composite_signal`)
- Combina múltiplos indicadores
- Algoritmo de pontuação proprietário
- Classificação automática de sinais

#### 4. Gestão de Risco (`build_position_plan_generic`)
- Stop loss baseado em ATR
- Take profit calculado
- Conformidade com regras da exchange

## Indicadores Técnicos

### RSI (Relative Strength Index)

**Fórmula:**
```
RSI = 100 - (100 / (1 + RS))
RS = Média de ganhos / Média de perdas
```

**Interpretação:**
- RSI > 70: Sobrecomprado
- RSI < 30: Sobrevendido
- Divergências: Sinal de reversão

**Implementação:**
```python
def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))
```

### MACD (Moving Average Convergence Divergence)

**Fórmulas:**
```
MACD Line = EMA(12) - EMA(26)
Signal Line = EMA(MACD Line, 9)
Histogram = MACD Line - Signal Line
```

**Sinais:**
- Cruzamento MACD > Signal: Bullish
- Cruzamento MACD < Signal: Bearish
- Divergências: Mudança de tendência

### Bollinger Bands

**Fórmulas:**
```
Middle Band = SMA(20)
Upper Band = Middle Band + (2 * StdDev)
Lower Band = Middle Band - (2 * StdDev)
```

**Sinais:**
- Preço toca banda superior: Possível reversão bearish
- Preço toca banda inferior: Possível reversão bullish
- Squeeze: Baixa volatilidade, possível breakout

### ATR (Average True Range)

**Fórmula:**
```
TR = max(High - Low, |High - Close_prev|, |Low - Close_prev|)
ATR = EMA(TR, period)
```

**Uso:**
- Medida de volatilidade
- Cálculo de stop loss
- Dimensionamento de posição

## Sistema de Pontuação

### Algoritmo Proprietário

O sistema combina múltiplos fatores com pesos específicos:

```python
score = (trend_score * 1.5) + (momentum_score * 1.2) + 
        (volatility_score * 0.8) + (divergence_score * 1.5) + 
        (breakout_score * 2.0) + (arima_garch_score * 0.5)
```

### Componentes do Score

#### 1. Trend Score (-1 a +1)
- EMA 20 vs EMA 50 vs EMA 200
- Inclinação das médias móveis
- Posição do preço relativa às EMAs

#### 2. Momentum Score (-1 a +1)
- RSI: Sobrecomprado/Sobrevendido
- MACD: Cruzamentos e divergências
- ROC: Taxa de mudança
- Stochastic: Momentum de curto prazo

#### 3. Volatility Score (-1 a +1)
- Bollinger Bands: Posição relativa
- ATR: Expansão/Contração
- Volume: Confirmação de movimentos

#### 4. Divergence Score (-2 a +2)
- RSI vs Preço
- MACD vs Preço
- Volume vs Preço

#### 5. Breakout Score (-2 a +2)
- Donchian Channel: Breakouts
- Bollinger Bands: Squeeze e expansão
- Volume: Confirmação

### Classificação dos Sinais

```python
if score >= 3.0:
    return "Strong Buy"
elif score >= 1.5:
    return "Buy"
elif score <= -3.0:
    return "Strong Sell"
elif score <= -1.5:
    return "Sell"
else:
    return "Hold"
```

## Gestão de Risco

### Stop Loss Baseado em ATR

```python
def calculate_stop_loss(entry_price: float, atr: float, 
                       direction: str, multiplier: float = 2.0) -> float:
    if direction == "long":
        return entry_price - (atr * multiplier)
    else:
        return entry_price + (atr * multiplier)
```

### Take Profit

```python
def calculate_take_profit(entry_price: float, stop_loss: float, 
                         ratio: float = 2.0) -> float:
    risk = abs(entry_price - stop_loss)
    if entry_price > stop_loss:  # Long position
        return entry_price + (risk * ratio)
    else:  # Short position
        return entry_price - (risk * ratio)
```

### Dimensionamento de Posição

```python
def calculate_position_size(capital: float, risk_per_trade: float, 
                          entry: float, stop_loss: float) -> float:
    risk_amount = capital * risk_per_trade
    risk_per_unit = abs(entry - stop_loss)
    return risk_amount / risk_per_unit
```

## Integração com Exchanges

### Exchanges Suportadas

1. **OKX** - Suporte completo
2. **Binance** - Via CCXT
3. **Coinbase** - Via CCXT
4. **Kraken** - Via CCXT
5. **Bybit** - Via CCXT

### Configuração de Credenciais

```bash
# Variáveis de ambiente
export OKX_API_KEY="sua_api_key"
export OKX_API_SECRET="sua_api_secret"
export OKX_API_PASSWORD="sua_api_password"

export BINANCE_API_KEY="sua_api_key"
export BINANCE_API_SECRET="sua_api_secret"
```

### Regras Específicas por Exchange

#### OKX
```python
def okx_market_rules(exchange, symbol):
    market = exchange.market(symbol)
    return {
        'price_precision': market['precision']['price'],
        'amount_precision': market['precision']['amount'],
        'min_amount': market['limits']['amount']['min'],
        'min_cost': market['limits']['cost']['min'],
        'maker_fee': market['maker'],
        'taker_fee': market['taker']
    }
```

## Sistema de Alertas

### E-mail

```python
def send_email_alert(signal, smtp_config):
    msg = MIMEMultipart()
    msg['From'] = smtp_config['from_email']
    msg['To'] = smtp_config['to_email']
    msg['Subject'] = f"Alerta: {signal.symbol} - {signal.label}"
    
    body = f"""
    Símbolo: {signal.symbol}
    Sinal: {signal.label}
    Score: {signal.score:.2f}
    Entrada: {signal.entry_hint}
    Stop Loss: {signal.stop_loss}
    Take Profit: {signal.take_profit}
    """
    
    msg.attach(MIMEText(body, 'plain'))
    # ... envio do e-mail
```

### Telegram

```python
def send_telegram_alert(signal, telegram_config):
    bot_token = telegram_config['bot_token']
    chat_id = telegram_config['chat_id']
    
    message = f"""
    🚨 *Alerta de Trading* 🚨
    📊 *Símbolo:* {signal.symbol}
    📈 *Sinal:* {signal.label}
    ⭐ *Score:* {signal.score:.2f}
    """
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # ... envio via API
```

## Backtesting

### Estrutura Básica

```python
def run_backtest(df, signal_func, initial_capital=1000.0):
    capital = initial_capital
    position = None
    trades = []
    
    for i in range(len(df)):
        current_data = df.iloc[:i+1]
        signal = signal_func(current_data, "TEST", "1h")
        
        if signal and signal.label in ["Strong Buy", "Buy"]:
            if position is None:  # Abrir posição
                position = {
                    'entry_price': df.iloc[i]['close'],
                    'entry_time': df.index[i],
                    'stop_loss': signal.stop_loss,
                    'take_profit': signal.take_profit
                }
        
        elif position is not None:  # Verificar saída
            current_price = df.iloc[i]['close']
            
            # Verificar stop loss ou take profit
            if (current_price <= position['stop_loss'] or 
                current_price >= position['take_profit']):
                
                # Fechar posição
                pnl = current_price - position['entry_price']
                capital += pnl
                
                trades.append({
                    'entry_time': position['entry_time'],
                    'exit_time': df.index[i],
                    'entry_price': position['entry_price'],
                    'exit_price': current_price,
                    'pnl': pnl
                })
                
                position = None
    
    return {
        'initial_capital': initial_capital,
        'final_equity': capital,
        'total_return': (capital - initial_capital) / initial_capital,
        'trades': trades
    }
```

## Configuração Avançada

### Arquivo config.yaml

```yaml
# Parâmetros dos indicadores
indicators:
  rsi:
    period: 14
    overbought: 70
    oversold: 30
  
  macd:
    fast_period: 12
    slow_period: 26
    signal_period: 9
  
  bollinger:
    period: 20
    std_dev: 2
  
  atr:
    period: 14

# Sistema de pontuação
scoring:
  weights:
    trend: 1.5
    momentum: 1.2
    volatility: 0.8
    divergence: 1.5
    breakout: 2.0
    arima_garch: 0.5

# Gestão de risco
risk_management:
  default_stop_multiplier: 2.0
  default_tp_ratio: 2.0
  max_risk_per_trade: 0.02

# Alertas
alerts:
  email:
    enabled: false
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
  
  telegram:
    enabled: false
```

## Otimizações de Performance

### Numba JIT Compilation

```python
from numba import jit

@jit(nopython=True)
def fast_rsi(prices, period=14):
    # Implementação otimizada
    pass
```

### Cache de Indicadores

```python
class IndicatorCache:
    def __init__(self, max_size=1000):
        self.cache = {}
        self.max_size = max_size
    
    def get_key(self, symbol, timeframe, indicator, params):
        return f"{symbol}_{timeframe}_{indicator}_{hash(params)}"
```

### Processamento Paralelo

```python
import concurrent.futures

def parallel_symbol_analysis(symbols, exchange, timeframe, max_workers=4):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(analyze_symbol, symbol) for symbol in symbols]
        results = [future.result() for future in futures]
    return results
```

## Troubleshooting

### Problemas Comuns

#### 1. Erro de Conexão com Exchange
```
Erro: ccxt.NetworkError
Solução: Verificar conectividade e credenciais
```

#### 2. Dados OHLCV Vazios
```
Erro: DataFrame vazio
Solução: Verificar se o símbolo existe na exchange
```

#### 3. Erro de Precisão
```
Erro: Ordem rejeitada por precisão
Solução: Verificar regras da exchange
```

### Logs e Debugging

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_alerts.log'),
        logging.StreamHandler()
    ]
)
```

### Monitoramento

```python
def monitor_system_health():
    # Verificar conectividade
    # Verificar uso de memória
    # Verificar performance
    pass
```

## Considerações de Segurança

1. **Nunca** hardcode credenciais no código
2. Use variáveis de ambiente para configurações sensíveis
3. Implemente rate limiting para APIs
4. Monitore logs para atividades suspeitas
5. Use HTTPS para todas as comunicações

## Roadmap Futuro

1. **Machine Learning**: Integração com modelos de ML
2. **Mais Exchanges**: Suporte para exchanges adicionais
3. **Interface Web**: Dashboard para monitoramento
4. **Mobile App**: Aplicativo para alertas móveis
5. **Social Trading**: Compartilhamento de sinais

---

**Última atualização:** 26 de Agosto de 2025
**Versão:** 1.0.0

