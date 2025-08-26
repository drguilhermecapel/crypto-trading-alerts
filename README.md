# Crypto Trading Alerts

Sistema avançado de alertas de trading de criptomoedas com análise técnica baseada em probabilidades e fórmulas matemáticas para múltiplas exchanges.

## 🚀 Características

- **Análise Técnica Avançada**: Utiliza múltiplos indicadores técnicos (RSI, MACD, Bollinger Bands, ATR, Stochastic, Awesome Oscillator)
- **Detecção de Divergências**: Identifica divergências entre preço e indicadores
- **Múltiplas Exchanges**: Suporte para OKX, Binance, Coinbase, Kraken, Bybit
- **Sistema de Pontuação**: Algoritmo proprietário que combina múltiplos sinais
- **Gestão de Risco**: Stop loss e take profit baseados em ATR
- **Alertas Inteligentes**: Notificações por e-mail e Telegram
- **Backtesting**: Módulo para validação de estratégias
- **Modelos Preditivos**: Integração opcional com ARIMA e GARCH

## 📊 Indicadores Técnicos

### Indicadores de Tendência
- **EMA (Exponential Moving Average)**: 20, 50, 200 períodos
- **SMA (Simple Moving Average)**: Médias móveis simples
- **Donchian Channel**: Canal de breakout

### Indicadores de Momentum
- **RSI (Relative Strength Index)**: Força relativa
- **MACD**: Convergência e divergência de médias móveis
- **ROC (Rate of Change)**: Taxa de mudança
- **Stochastic Oscillator**: Oscilador estocástico
- **Awesome Oscillator**: Oscilador de momentum

### Indicadores de Volatilidade
- **Bollinger Bands**: Bandas de volatilidade
- **ATR (Average True Range)**: Volatilidade média

## 🔍 Sistema de Pontuação

O sistema utiliza um algoritmo proprietário que combina:

```python
score = (trend_score * 1.5) + (mom_score * 1.2) + (vol_score * 0.8) + 
        (div_score * 1.5) + (brk_score * 2.0) + (arga_score * 0.5)
```

### Classificação dos Sinais
- **Strong Buy** (Score ≥ 3.0): Sinal de compra forte
- **Buy** (Score ≥ 1.5): Sinal de compra
- **Hold** (-1.5 < Score < 1.5): Manter posição
- **Sell** (Score ≤ -1.5): Sinal de venda
- **Strong Sell** (Score ≤ -3.0): Sinal de venda forte

## 🛠️ Instalação

1. Clone o repositório:
```bash
git clone https://github.com/drguilhermecapel/crypto-trading-alerts.git
cd crypto-trading-alerts
```

2. Instale as dependências:
```bash
pip install -r requirements.txt
```

3. Configure as variáveis de ambiente:
```bash
# Credenciais da Exchange (exemplo para OKX)
export OKX_API_KEY="sua_api_key"
export OKX_API_SECRET="sua_api_secret"
export OKX_API_PASSWORD="sua_api_password"

# Alertas por E-mail (opcional)
export EMAIL_ALERTS_ENABLED="true"
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT="587"
export FROM_EMAIL="seu_email@gmail.com"
export TO_EMAIL="destino@gmail.com"
export EMAIL_PASSWORD="sua_senha_app"

# Alertas por Telegram (opcional)
export TELEGRAM_ALERTS_ENABLED="true"
export TELEGRAM_BOT_TOKEN="seu_bot_token"
export TELEGRAM_CHAT_ID="seu_chat_id"
```

## 🚀 Uso

### Varredura Básica
```bash
python crypto_alerts_updated.py --scan_top --top_n 30 --timeframe 1h
```

### Análise de Símbolos Específicos
```bash
python crypto_alerts_updated.py --symbols "BTC/USDT,ETH/USDT" --timeframe 1h
```

### Com Envio de Ordens (CUIDADO!)
```bash
python crypto_alerts_updated.py --symbols "BTC/USDT" --timeframe 1h --capital 1000 --place_orders --risk_per_trade 0.01
```

### Parâmetros Principais

- `--exchange`: Exchange a usar (okx, binance, etc.)
- `--symbols`: Lista de símbolos separados por vírgula
- `--scan_top`: Varrer os top símbolos por volume
- `--top_n`: Número de top símbolos (padrão: 20)
- `--timeframe`: Timeframe (1m, 5m, 15m, 1h, 4h, 1d)
- `--capital`: Capital disponível em USD
- `--risk_per_trade`: Risco por trade (0.01 = 1%)
- `--place_orders`: Enviar ordens reais (USE COM CUIDADO!)

## 📈 Backtesting

O sistema inclui um módulo básico de backtesting:

```python
from crypto_alerts_updated import run_backtest, composite_signal

# Executar backtest
results = run_backtest(df, composite_signal, initial_capital=1000.0)
print(f"Retorno total: {results['total_return']:.2%}")
```

## 🔐 Segurança

- **Nunca** compartilhe suas chaves de API
- Use variáveis de ambiente para credenciais
- Teste sempre em modo simulação antes de usar ordens reais
- Configure limites de risco apropriados

## 📱 Alertas

### E-mail
Configure SMTP para receber alertas por e-mail com detalhes completos dos sinais.

### Telegram
Configure um bot do Telegram para alertas instantâneos no seu celular.

## 🔧 Configuração Avançada

Edite o arquivo `config.yaml` para personalizar:
- Parâmetros dos indicadores
- Pesos do sistema de pontuação
- Configurações de risco
- Timeframes preferidos

## 📊 Exchanges Suportadas

- **OKX**: Suporte completo com regras específicas
- **Binance**: Suporte via CCXT
- **Coinbase**: Suporte via CCXT
- **Kraken**: Suporte via CCXT
- **Bybit**: Suporte via CCXT

## 🤖 Modelos Preditivos (Opcional)

Se instaladas as dependências opcionais:
```bash
pip install statsmodels arch
```

O sistema utilizará:
- **ARIMA**: Para previsão de retornos
- **GARCH**: Para previsão de volatilidade

## ⚠️ Disclaimer

Este software é fornecido apenas para fins educacionais e de pesquisa. O trading de criptomoedas envolve riscos significativos e você pode perder todo o seu capital. Sempre:

- Faça sua própria pesquisa
- Teste em ambiente de simulação
- Use apenas capital que pode perder
- Considere consultar um consultor financeiro

## 📄 Licença

Este projeto está licenciado sob a licença MIT. Veja o arquivo LICENSE para detalhes.

## 🤝 Contribuições

Contribuições são bem-vindas! Por favor:

1. Faça um fork do projeto
2. Crie uma branch para sua feature
3. Commit suas mudanças
4. Push para a branch
5. Abra um Pull Request

## 📞 Suporte

Para suporte e dúvidas:
- Abra uma issue no GitHub
- Entre em contato via e-mail

---

**⚡ Desenvolvido com Python e amor pela análise técnica ⚡**
