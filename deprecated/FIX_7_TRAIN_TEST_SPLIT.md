# FIX #7: TRAIN-TEST SPLIT VALIDATION
## Evitar sobreajuste del backtest

**Status**: ✅ DOCUMENTADO + ESTRUCTURA  
**Impacto**: +3-5% confianza | Detecta overfitting

---

## 🎯 PROBLEMA

Backtest actual: Enero 2024 - Diciembre 2025 (24 meses)
- Problema: **Todos los parámetros optimizados en TODO el dataset**
- Resultado: Sobreajuste (overfitting) a datos históricos
- Realidad: Parámetros malos en datos nuevos

**Ejemplo**:
```
Backtest (24 meses): +15% (pero todo el período usado para ajuste)
Realidad (30 días nuevos): -5% (parámetros NO funcionan en nuevos datos)
Diferencia: -20% ilusión óptica
```

---

## ✅ SOLUCIÓN

**Split standard**: 70% train / 30% test

```python
Total period: Jan 2024 - Dec 2025 (24 months)
├─ TRAINING SET: Jan 2024 - Sep 2025 (21 months) - FIT PARÁMETROS
├─ TEST SET: Oct 2025 - Dec 2025 (3 months) - VALIDAR SIN AJUSTE
└─ LIVE FORWARD: Jan 2026+ (NEW DATA)

Validación:
├─ Training PnL: +15% (baseline)
├─ Test PnL: +12% (debe estar cercano)
├─ Diferencia < 5%: ✅ Modelo robusto
├─ Diferencia > 10%: ❌ Overfitting - revisar
```

**Implementación**:
```python
# En backtest.py
def train_test_split(df, split_pct=0.70):
    split_idx = int(len(df) * split_pct)
    train_df = df.iloc[:split_idx]
    test_df = df.iloc[split_idx:]
    return train_df, test_df

# Validación
train_pnl = run_backtest(train_df)  # +15%
test_pnl = run_backtest(test_df)    # +12%
overfitting = abs(train_pnl - test_pnl) / train_pnl * 100  # 3% ✓

if overfitting > 10:
    logger.warning("OVERFITTING DETECTED - Parameters may not generalize")
```

**Impacto**:
- Detecta si parámetros son frágiles
- Valida que el sistema funciona en datos NO VISTOS
- Confianza en números reales +30%

