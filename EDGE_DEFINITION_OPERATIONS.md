# EDGE DEFINITION & OPERATIONS POLICY
## Sistema de Trading BTC Perpetuo - Gobernanza Operativa Formal

---

## 1️⃣ EDGE STATEMENT (TESIS FORMAL DEL EDGE)

### Definición obligatoria

**Esta estrategia gana porque:**
- Captura expansión direccional temprana tras eventos de desplazamiento de liquidez
- Opera en sesiones con participación real (volumen orgánico)
- El mercado paga continuidad más que reversión en estos contextos

### Implicaciones lógicas (supuestos no negociables)

El edge **NO EXISTE** si:
- ❌ El mercado está en régimen lateral
- ❌ La expansión intradía es baja
- ❌ No hay participantes reales (volumen artificial o sesiones muertas)

**Conclusión crítica:** Si alguno de estos supuestos falla, operar es **estadísticamente incorrecto**, aunque el setup aparezca en el gráfico.

---

## 2️⃣ CONTEXTOS DE NO-EDGE (INOPERABLES)

### Regla clave
**Filtrar no es suficiente. La estrategia debe declararse inválida.**

### Condiciones de invalidez (cualquiera dispara STOP OPERACIONAL)

| Condición | Métrica | Acción |
|-----------|---------|--------|
| Baja volatilidad intradía | ATR < percentil 30 (últimos 60 días) | ❌ NO OPERAR |
| Rango comprimido | Rango sesión < 0.7 × media histórica | ❌ NO OPERAR |
| Ambiente post-macro sin follow | Día posterior a shock sin continuidad | ❌ NO OPERAR |
| Pérdida de impulso | 2 sesiones sin expansión > 1R | ❌ NO OPERAR |
| Volumen degrado | Caída volumen London → NY | ❌ NO OPERAR |

**Estos no son filtros suaves. Son condiciones de INVALIDACIÓN DE EDGE.**

---

## 3️⃣ DEFINICIÓN DE TRADE VÁLIDO (UNIDAD DE ÉXITO)

### Un trade es correcto si cumple 4 criterios (independientes de PnL):

```
✓ Contexto EDGE válido       (supuestos cumplen)
✓ Setup válido               (patrón identificado correctamente)
✓ Timing correcto            (entrada en zona de probabilidad)
✓ Gestión correcta           (salida sigue lógica definida)
```

### Principio fundamental
**El resultado económico NO define la calidad del trade.**

Un trade perdedor puede ser 100% exitoso operativamente si cumple los 4 criterios.

### Métricas operativas de calidad (sí / no)

- Setup es válido según reglas de entrada
- Timing respeta zona de entrada óptima
- Gestión de riesgo es conforme (SL, TP1, extensión)
- Contexto en el momento de entrada es de EDGE

---

## 4️⃣ MODELO DE VARIANZA ACEPTADA

### Supuestos explícitos (reglas duras)

- Rachas de **6–10 pérdidas consecutivas** son normales
- Drawdowns del **12–18%** son esperables
- Meses planos o ligeramente negativos son parte del funcionamiento normal

### Regla de evaluación estadística

**Nada se evalúa con N < 30 trades**

- No hay conclusiones válidas antes de 30 operaciones
- Evaluación en ventanas: 30, 50, 100 trades
- La varianza natural absorbe desviaciones hasta N=30

---

## 5️⃣ MODELO DE DEGRADACIÓN DEL EDGE

### Señales tempranas de muerte (LEADING INDICATORS)

**El edge empieza a morir si se observan 2 o más:**

| Señal | Definición | Acción |
|-------|-----------|--------|
| Caída de MFE/MAE | Ratio MFE:MAE < 1.5 en ventana de 20 trades | 🔴 ALERTA |
| Más BE stops que winners | Break-even stops > 40% del total | 🔴 ALERTA |
| Tiempo sin extensión | Winners alcanzan TP1 pero no extienden | 🔴 ALERTA |
| Aumento de duración | Tiempo promedio en trade > 12h sin retorno adicional | 🔴 ALERTA |

**Estas señales preceden a pérdidas visibles (lag de 10-15 trades)**

### Respuesta operativa a degradación

Si se detectan 2+ señales:
1. Reducir tamaño a 50% del normal
2. Aumentar exigencia de contexto (filtros más estrictos)
3. Entrar en "Modo Observación" (14 días)
4. Evaluar en day 15

---

## 6️⃣ POLÍTICA DE ACTIVACIÓN

### La estrategia SOLO PUEDE OPERAR si:

```
✓ No hay degradación activa (< 2 señales tempranas)
✓ Contexto de edge válido (0 condiciones de invalidez)
✓ No se superó drawdown operativo (< 18%)
✓ NO está en periodo de observación post-degradación
```

**Si CUALQUIERA falla → Sistema permanece inactivo**

### Activación automática

El sistema se activa cuando:
- Degradación se resuelve (< 2 señales en nueva ventana)
- Contexto vuelve a ser válido
- Drawdown vuelve a < 15%
- Salió de periodo de observación

---

## 7️⃣ POLÍTICA DE NO-INTERVENCIÓN

### Reglas inquebrantables (el trader NO decide)

```
❌ No se modifica NADA antes de 30 trades
❌ No se ajustan parámetros en drawdown
❌ No se evalúa por PnL diario
❌ No se pausa por emociones
❌ No se "justifica" un trade fracasado
```

### El trader observa únicamente

- Count de trades ejecutados
- Ventanas de 30+ trades completadas
- Señales de degradación detectadas
- Estado de contexto (válido / inválido)

---

## 8️⃣ POLÍTICA DE MUERTE DE LA ESTRATEGIA

### La estrategia se APAGA Y ARCHIVA si:

```
1️⃣  Expectancy < 0 en 3 ventanas CONSECUTIVAS de 50 trades
     (150 trades sin recuperación)

2️⃣  Pérdida de edge en TODOS los regímenes
     (Ni en expansión, ni en rango, ni en sesiones específicas)

3️⃣  Degradación ESTRUCTURAL sin recuperación
     (Señales tempranas sostenidas por 30+ días)
```

**Si se cumple cualquiera → Estrategia archivada permanentemente**

### Por qué archivar es correcto

- Las estrategias tienen ciclos de vida
- Matar una estrategia = profesionalismo, no fracaso
- El mercado evoluciona; el edge desaparece
- Operar muerto = capital desperdiciado

---

## 9️⃣ ROL DEL TRADER (DEFINICIÓN FINAL)

### El trader NO es (roles prohibidos):

```
❌ Optimizador          (no ajusta parámetros)
❌ Salvavidas           (no rescata trades)
❌ Intérprete de mercado (no "predice")
❌ Decisor              (el sistema decide)
```

### El trader ES (roles únicos permitidos):

```
✅ Ejecutor del sistema       (entra, sale, respeta reglas)
✅ Guardián de la muestra     (protege N ≥ 30)
✅ Observador estadístico     (registra, mide, no juzga)
✅ Guardián del edge          (valida contexto antes de operar)
```

### Checklist pre-operación (sí / no)

- ¿Hay 2+ señales de degradación? → NO OPERAR
- ¿Hay 1+ condiciones de invalidez? → NO OPERAR
- ¿Drawdown > 18%? → NO OPERAR
- ¿Estamos en periodo de observación post-alerta? → NO OPERAR
- Si todos NO → PROCEDER

---

## 🔟 MÉTRICAS DE SEGUIMIENTO (DASHBOARD OBLIGATORIO)

### Métricas de muestra

| Métrica | Frecuencia | Threshold |
|---------|-----------|-----------|
| Win rate | Ventana 30 | > 45% |
| Expectancy | Ventana 50 | > 0.2R |
| Profit factor | Ventana 50 | > 1.3 |
| Max Consecutive Losses | Rolling | < 10 |
| Drawdown actual | Diario | < 18% |

### Métricas de degradación (señales tempranas)

| Métrica | Cálculo | Umbral alerta |
|---------|---------|---------------|
| MFE/MAE ratio | ∑MFE / ∑MAE | < 1.5 |
| BE stop % | Trades en BE / Total | > 40% |
| Avg trade duration | ∑Duración / N | > 12h |
| Winners no-extension % | Winners sin TP2+ / Total winners | > 60% |

### Métricas de contexto (validez de edge)

| Métrica | Cálculo | Validez |
|---------|---------|---------|
| ATR intradía | ATR(60) | > P30 histórico |
| Rango sesión | High - Low | > 0.7 × media(60) |
| Volumen momentum | Vol(London-NY) | > trend histórico |
| Expansión consecutiva | Sessions > 1R | > 2 sesiones |

---

## 1️⃣1️⃣ IMPLEMENTACIÓN TÉCNICA

### Clase EdgeValidator

```python
class EdgeValidator:
    
    def validate_context(self) -> bool:
        """Retorna True si contexto es válido (edge existe)"""
        # Verifica 5 condiciones de no-edge
        return all([
            self.atr_above_threshold(),
            self.range_above_threshold(),
            self.not_macro_event_day(),
            self.expansion_present(),
            self.volume_healthy()
        ])
    
    def detect_degradation(self) -> int:
        """Retorna N señales tempranas (0-4)"""
        signals = [
            self.mfe_mae_ratio() < 1.5,
            self.be_stops_ratio() > 0.4,
            self.winners_no_extend_ratio() > 0.6,
            self.avg_trade_duration() > 12
        ]
        return sum(signals)
    
    def can_operate(self) -> bool:
        """Retorna True si estrategia puede operar"""
        return all([
            self.detect_degradation() < 2,
            self.validate_context(),
            self.drawdown_ok(),
            not self.in_observation_period()
        ])
    
    def should_kill_strategy(self) -> bool:
        """Retorna True si expectancy se rompió permanentemente"""
        return self.expectancy_negative_3_windows()
```

### Estado del sistema

```python
class StrategyState(Enum):
    ACTIVE = "operating"
    DEGRADED = "reduced_size"
    OBSERVATION = "post_alert_monitoring"
    DEAD = "archived"
```

---

## 1️⃣2️⃣ REPORTE PERIÓDICO (SEMANAL)

### Formato obligatorio

```
WEEK REPORT - WW/YYYY
======================

1. SAMPLE SIZE
   Trades this week: N
   Total trades (rolling): M
   
2. CONTEXT VALIDITY
   - ATR valid: ✓/✗
   - Range valid: ✓/✗
   - Volume healthy: ✓/✗
   Status: [VALID / INVALID]
   
3. DEGRADATION SIGNALS
   - MFE/MAE: X.X (threshold: 1.5)
   - BE stops: X% (threshold: 40%)
   - Non-extending winners: X% (threshold: 60%)
   - Avg duration: Xh (threshold: 12h)
   Active signals: N/4
   Status: [HEALTHY / DEGRADING / DEGRADED]
   
4. OPERATING STATE
   Current state: [ACTIVE / DEGRADED / OBSERVATION / DEAD]
   
5. KEY METRICS
   Win rate (30): X%
   Expectancy (50): X.XR
   Profit factor: X.X
   Max consecutive losses: X
   Current drawdown: X%
```

---

## 1️⃣3️⃣ PROTOCOLO DE DECISIÓN (FLOWCHART)

```
INICIO DEL DÍA
    ↓
¿Hay 2+ señales de degradación?
    ├─ YES → NO OPERAR (Modo Observación o Reduced)
    └─ NO ↓
¿Contexto de edge es válido?
    ├─ NO → NO OPERAR (Esperamos sesión mejor)
    └─ YES ↓
¿Drawdown < 18%?
    ├─ NO → NO OPERAR (Pausa obligatoria)
    └─ YES ↓
¿Setup está presente?
    ├─ NO → ESPERAR siguiente oportunidad
    └─ YES ↓
EJECUTAR OPERACIÓN CONFORME A REGLAS
    ↓
REGISTRAR: Setup, Timing, Gestión, Contexto
    ↓
FIN
```

---

## 1️⃣4️⃣ REVISIÓN DE MUERTE (CHECKLIST)

Si expectancy se torna negativa:

```
1. ¿Se observan 2+ señales de degradación? 
   → Entrar en Modo Observación (14 días)
   → Reducir tamaño 50%
   
2. Después de 14 días:
   ¿Recuperó expectancy o desapareció degradación?
   → YES: Volver a ACTIVE
   → NO: Ir a paso 3
   
3. ¿3 ventanas consecutivas de 50 trades con expectancy < 0?
   → YES: ARCHIVAR ESTRATEGIA (muerte oficial)
   → NO: Mantenerse en observación extendida
```

---

## VERSIÓN Y EFECTIVIDAD

- **Versión:** 1.0
- **Fecha de implementación:** 2026-01-03
- **Próxima revisión:** 2026-02-28 (o post-muerte)

---

**Este documento es la constitución operativa. No se discute, se ejecuta.**
