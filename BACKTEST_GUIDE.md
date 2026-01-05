# Guía de Backtesting - Sistema de Trading BTC Perpetuo

## Descripción General

El módulo de backtesting permite simular tu estrategia de trading con datos históricos, sin ejecutar órdenes reales. Esto te permite:

- ✅ **Validar la estrategia** antes de deployar en producción
- 📊 **Medir rendimiento** con métricas profesionales
- 🎯 **Optimizar parámetros** iterativamente
- 💰 **Estimar drawdown máximo** y riesgos
- 📈 **Comprender comportamiento** en diferentes mercados

## Arquitectura del Backtester

```
Backtester
├── Market Data Buffer (sincronizado multi-timeframe)
├── State Machine (mismo que producción)
├── Filters (Kill conditions, Black Swan, HTF, MTF, LTF)
├── Risk Engine (same as production)
└── Trade Simulator (sin ejecución real)
```

**Ventaja**: Usa los MISMOS componentes que el sistema real, asegurando que el backtest es confiable.

## Componentes Principales

### 1. `Backtester` (engine/backtest.py)

Clase principal que simula el trading:

```python
from engine.backtest import Backtester

bt = Backtester(
    symbol='BTC/USD',
    initial_balance=10000.0,      # Capital inicial
    position_size_pct=1.0,         # % del capital por trade
    entry_timeout_minutes=3        # Minutos antes de cancelar entrada
)
```

### 2. `BacktestTrade`

Representa cada trade ejecutado:

```python
trade.entry_time           # datetime de entrada
trade.entry_price          # Precio de entrada
trade.direction            # 'LONG' o 'SHORT'
trade.exit_time            # datetime de salida
trade.exit_price           # Precio de salida
trade.exit_reason          # 'TP', 'SL', 'EXIT_SIGNAL', 'TIMEOUT'
trade.pnl                  # Ganancia/pérdida en USD
trade.pnl_pct              # Ganancia/pérdida en %
trade.duration_minutes     # Duración del trade
```

### 3. `BacktestResults`

Estadísticas del backtest:

```python
results.total_trades              # Número de trades
results.winning_trades            # Trades ganadores
results.losing_trades             # Trades perdedores
results.win_rate                  # % de trades ganadores
results.total_pnl                 # P&L total en USD
results.avg_win / avg_loss        # Promedio de ganancias/pérdidas
results.profit_factor             # Total ganancias / Total pérdidas
results.sharpe_ratio              # Ratio de Sharpe anualizado
results.max_drawdown              # Máximo drawdown en USD
results.max_drawdown_pct          # Máximo drawdown en %
results.max_consecutive_wins      # Rachas ganadoras máximas
results.max_consecutive_losses    # Rachas perdedoras máximas
```

## Uso Básico

### Opción 1: Datos desde CCXT (Binance, Kraken, etc.)

```python
from backtest_runner import BacktestRunner

# Crear runner
runner = BacktestRunner(
    symbol='BTC/USD',
    initial_balance=10000.0,
    position_size_pct=1.0
)

# Descargar últimos 30 días con velas de 1 minuto
runner.load_from_ccxt(days=30, timeframe='1m')

# Ejecutar backtest
results = runner.run_backtest()

# Ver resultados
runner.print_results(results)
```

### Opción 2: Datos desde CSV

```python
# Archivo CSV debe tener columnas: timestamp, open, high, low, close, volume

runner = BacktestRunner(symbol='BTC/USD')
runner.load_from_csv('historical_data.csv')
results = runner.run_backtest()
runner.print_results(results)
```

### Opción 3: Datos Sintéticos (Testing rápido)

```python
from example_backtest import generate_synthetic_data
from engine.backtest import Backtester

# Generar datos sintéticos
df = generate_synthetic_data(
    start_price=40000.0,
    num_candles=1000,
    volatility=0.02,
    trend=0.0003
)

# Backtesting
bt = Backtester(symbol='BTC/USD')
bt.load_historical_candles(df, timeframe='1m')
results = bt.run()
print(bt.get_report())
```

## Línea de Comandos

### Backtest desde CCXT

```bash
# Últimos 30 días, velas de 1 minuto
python backtest_runner.py --symbol BTC/USD --days 30 --timeframe 1m

# Últimos 7 días con capital inicial de $50,000
python backtest_runner.py --symbol BTC/USD --days 7 --capital 50000

# Rango de fechas específico
python backtest_runner.py --symbol BTC/USD --days 30 \
    --start-date 2026-01-01 --end-date 2026-01-31
```

### Backtest desde CSV

```bash
python backtest_runner.py --file datos.csv

# Con custom capital y position size
python backtest_runner.py --file datos.csv --capital 25000 --position-size 2.0
```

### Ejecutar ejemplos incluidos

```bash
python example_backtest.py
```

Esto ejecuta 3 ejemplos:
1. **Uptrend**: Mercado alcista (trend positivo)
2. **Volatile**: Mercado muy volátil (sin trend)
3. **Downtrend**: Mercado bajista (trend negativo)

## Ejemplo Completo de Código

```python
#!/usr/bin/env python3
"""Backtest completo con análisis"""

import pandas as pd
from engine.backtest import Backtester

# 1. Cargar datos
df = pd.read_csv('btc_data.csv')
print(f"Datos: {len(df)} candles desde {df['timestamp'].min()}")

# 2. Crear backtester
bt = Backtester(
    symbol='BTC/USD',
    initial_balance=10000.0,
    position_size_pct=1.0
)

# 3. Cargar candles
bt.load_historical_candles(df, timeframe='1m')

# 4. Ejecutar backtest
results = bt.run()

# 5. Analizar resultados
print(f"\n📊 RESULTADOS:")
print(f"  Total trades: {results.total_trades}")
print(f"  Win rate: {results.win_rate:.2f}%")
print(f"  Total P&L: ${results.total_pnl:.2f}")
print(f"  Sharpe ratio: {results.sharpe_ratio:.2f}")
print(f"  Max drawdown: {results.max_drawdown_pct:.2f}%")
print(f"  Profit factor: {results.profit_factor:.2f}")

# 6. Reporte detallado
print(bt.get_report())

# 7. Exportar resultados
results_dict = results.to_dict()
results_df = pd.DataFrame([results_dict])
results_df.to_csv('backtest_results.csv', index=False)
```

## Interpretación de Métricas

### Win Rate
- % de trades que ganaron dinero
- Objetivo: > 50% es positivo, > 60% es muy bueno

### Profit Factor
- Total ganancias / Total pérdidas
- Objetivo: > 1.0 es positivo, > 2.0 es excelente

### Sharpe Ratio
- Rendimiento ajustado por volatilidad
- Objetivo: > 1.0 es aceptable, > 2.0 es muy bueno

### Max Drawdown
- Máxima pérdida del pico al valle
- Indica el riesgo de la estrategia
- Objetivo: < 20% para estrategias conservadoras

### Profit Factor
- Total ganancias / Total pérdidas
- > 1.0 indica strategy rentable

## Validación de Datos

El backtester valida automáticamente:

1. ✅ Datos están ordenados cronológicamente
2. ✅ Columnas requeridas: timestamp, open, high, low, close, volume
3. ✅ High >= Low en cada candle
4. ✅ No hay valores negativos de precio
5. ✅ No hay valores NaN

## Ficheros de Salida

Cada backtest genera:

1. **backtest_results_YYYYMMDD_HHMMSS.txt** - Reporte completo
2. Opcionalmente: **backtest_results.csv** - Estadísticas en formato CSV

## Optimización de Parámetros

Para encontrar los mejores parámetros, ejecuta múltiples backtests:

```python
import pandas as pd
from engine.backtest import Backtester

results_list = []

for position_size in [0.5, 1.0, 2.0, 3.0]:
    for days in [7, 14, 30]:
        # Crear backtester con estos parámetros
        bt = Backtester(
            symbol='BTC/USD',
            initial_balance=10000.0,
            position_size_pct=position_size
        )
        
        # Cargar datos
        # ... code ...
        
        # Ejecutar
        results = bt.run()
        
        results_list.append({
            'position_size': position_size,
            'days': days,
            'sharpe': results.sharpe_ratio,
            'win_rate': results.win_rate,
            'max_dd': results.max_drawdown_pct,
            'total_pnl': results.total_pnl
        })

# Analizar resultados
df_results = pd.DataFrame(results_list)
print(df_results.sort_values('sharpe', ascending=False))
```

## Limitaciones y Consideraciones

⚠️ **El backtest no es perfecto. Considera:**

1. **Slippage**: El backtest usa precios exactos. En realidad hay slippage
2. **Liquidez**: No simula falta de liquidez en ordenes grandes
3. **Comisiones**: No incluye comisiones (agrega 0.1% si quieres ser realista)
4. **Gaps**: No maneja gaps de precio (ej: apertura asiática)
5. **Sentimiento**: No captura cambios de sentimiento del mercado
6. **Execution**: Asume ejecución instantánea (en realidad toma ms-segundos)

## Mejoras Futuras

- [ ] Agregar soporte para multi-symbol backtests
- [ ] Incluir comisiones realistas por exchange
- [ ] Simulación de slippage
- [ ] Análisis de Monte Carlo
- [ ] Exportar reportes en PDF
- [ ] Visualización interactiva de equity curve
- [ ] Análisis de correlation entre trades

## Troubleshooting

### Error: "No 1m candles loaded"
```
Solución: Asegúrate de llamar a load_historical_candles() antes de run()
```

### Error: "ccxt not installed"
```bash
pip install ccxt
```

### Pocos trades en el backtest
```
Posible causa: La lógica de entrada es muy restrictiva
Solución: Revisar la función _check_entry_signal() y ajustar condiciones
```

### Resultados no realistas
```
Posible causa: 
- Timeframe incorrecto (ej: 1h en lugar de 1m)
- Falta de datos históricos suficientes
- Position size muy grande
```

## Referencias

- [engine/backtest.py](engine/backtest.py) - Código del backtester
- [backtest_runner.py](backtest_runner.py) - CLI para ejecutar backtests
- [example_backtest.py](example_backtest.py) - Ejemplos prácticos
