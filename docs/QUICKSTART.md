# 🚀 Quick Start Guide

## Paso 1: Instalación

```bash
# Clonar o navegar al proyecto
cd /home/carlo/projects/trading/daytrader2

# Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# o
venv\Scripts\activate  # Windows

# Instalar dependencias
pip install -r requirements.txt
```

## Paso 2: Configurar Kafka (Opcional - para datos reales)

```bash
# Iniciar servicios con Docker
docker-compose up -d

# Verificar que Kafka esté corriendo
docker-compose ps
```

## Paso 3: Configurar Parámetros

Editar `engine/config.py` según tus preferencias:

```python
# Capital inicial
RiskManagement.INITIAL_CAPITAL = 10000.0

# Riesgo por operación
RiskManagement.RISK_LONDON_NY_PCT = 1.0  # 1%

# Límites de seguridad
KillConditions.MAX_DAILY_DRAWDOWN_PCT = 3.0  # 3%
KillConditions.MAX_CONSECUTIVE_LOSSES = 3
```

## Paso 4: Modo de Ejecución

### Modo Simulación (Sin Exchange Real)
```bash
python main.py
```
El sistema correrá en modo simulación, generando señales pero sin ejecutar órdenes reales.

### Modo Paper Trading (Testnet)
Editar `main.py`:
```python
import ccxt

# En TradingSystemOrchestrator.__init__():
self.exchange_connector = ccxt.binance({
    'options': {'defaultType': 'future'},
    'urls': {'api': 'https://testnet.binancefuture.com'}
})
```

### Modo Live (⚠️ Dinero Real)
```bash
# Configurar variables de entorno
export BINANCE_API_KEY="tu_api_key"
export BINANCE_SECRET="tu_secret"

# Editar main.py para usar credenciales reales
```

## Paso 5: Monitoreo

### Logs en Terminal
El sistema mostrará logs en tiempo real:
```
2026-01-02 10:30:15 - SignalEngine      - INFO     - HTF Filter PASSED - Bias: LONG
2026-01-02 10:30:16 - SignalEngine      - INFO     - MTF Structure Filter PASSED
2026-01-02 10:30:17 - SignalEngine      - INFO     - LTF Trigger Filter PASSED
2026-01-02 10:30:17 - SignalEngine      - INFO     - 🎯 SETUP COMPLETE - LONG
```

### Métricas Prometheus
Abrir en navegador: http://localhost:8000

### Estado del Sistema
Cada minuto verás un reporte:
```
============================================================
📊 SYSTEM STATUS
State: IDLE
Current Bias: LONG
Has Position: False
Daily Trades: 2
Daily DD: $-50.00
Performance: PF=1.75 | Exp=$12.50 | WR=60.0%
============================================================
```

## Paso 6: Entender el Flujo

1. **Warm-up**: Sistema necesita datos históricos (200 velas 1H, 50 velas 5M)
2. **Análisis**: Cada segundo evalúa los filtros HTF → MTF → LTF
3. **Setup Completo**: Cuando todos los filtros pasan → Coloca orden
4. **Gestión**: Administra la posición con TP1, TP2, trailing stop
5. **Cooldown**: 15 minutos de descanso después de cada operación

## Componentes Principales

```
engine/
├── config.py          ← Todos los parámetros configurables
├── signal_engine.py   ← Motor principal de la estrategia
├── filters.py         ← Filtros HTF, MTF, LTF, Kill Conditions
├── risk_engine.py     ← Gestión de riesgo y posiciones
├── order_manager.py   ← Ejecución de órdenes
├── state_machine.py   ← Estados del bot
└── indicators.py      ← Indicadores técnicos optimizados
```

## Parámetros Clave

### Timeframes
- **HTF (1H)**: Determina sesgo LONG/SHORT
- **MTF (5M)**: Identifica estructura y zona de interés
- **LTF (1M)**: Gatillo de entrada basado en flujo

### Filtros HTF (1H)
- EMA 50 > 200 = LONG | EMA 50 < 200 = SHORT
- ADX > 20 con pendiente positiva
- ATR por encima de su promedio

### Filtros MTF (5M)
- Break of Structure confirmado
- Retroceso Fibonacci 38.2% - 61.8%
- Precio entre EMA 20 y EMA 50
- Cerca de VWAP
- Sin sobreextensión (< 2× ATR del VWAP)

### Filtros LTF (1M)
- Cierre de vela a favor de la tendencia
- Volumen Relativo > 1.5×
- Order Book Imbalance ≥ 60/40
- Sin spike de spread
- Velocidad de precio < 3× ATR

### Gestión de Salidas
- **TP1** (+1R): Cierra 50%, mueve SL a BE
- **TP2**: High/Low de sesión
- **Trailing Stop**: 1× ATR(1m)
- **Exit Climático**: Volumen ≥ 2.5× promedio
- **Exit por Tiempo**: Máx 45 min sin TP1

### Kill Conditions
El bot se detiene automáticamente si:
- Drawdown diario ≥ 3%
- 3 pérdidas consecutivas
- WebSocket desconectado > 10s
- Funding rate > ±0.08%
- Evento macro detectado
- Vela ≥ 3× ATR (Cisne Negro)

## Troubleshooting

### El bot no genera señales
- ✅ Verificar que hay datos suficientes (warm-up)
- ✅ Revisar logs de filtros HTF/MTF/LTF
- ✅ Confirmar que no está en cooldown
- ✅ Verificar que el estado es IDLE

### Errores de conexión
- ✅ Kafka debe estar corriendo
- ✅ Verificar conectividad a exchange
- ✅ Revisar credenciales API

### Métricas no aparecen
- ✅ Verificar puerto 8000 disponible
- ✅ Abrir http://localhost:8000

## Comandos Útiles

```bash
# Ver logs en tiempo real
python main.py

# Detener (Ctrl+C)
# El sistema hará shutdown gracefully

# Ver métricas
curl http://localhost:8000

# Verificar Kafka
docker-compose logs -f kafka
```

## Próximos Pasos

1. **Backtest**: Probar con datos históricos
2. **Paper Trading**: Validar en testnet
3. **Optimización**: Ajustar parámetros según resultados
4. **Live Trading**: Iniciar con capital mínimo

## ⚠️ Recordatorios Importantes

- ✅ **Nunca** operar con dinero que no puedes perder
- ✅ Probar primero en simulación y paper trading
- ✅ Empezar con posiciones pequeñas
- ✅ Monitorear constantemente las primeras operaciones
- ✅ Respetar los kill conditions del sistema
- ✅ Llevar registro de todas las operaciones

## 📚 Documentación Completa

- [README_STRATEGY.md](README_STRATEGY.md) - Documentación técnica completa
- [Especificaciones Técnicas Finales_ Sistema de Trading BTC Perpetual.md] - Documento de especificaciones original

---

**¡Buena suerte y opera con disciplina!** 🎯
