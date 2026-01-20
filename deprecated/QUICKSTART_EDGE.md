# QUICK START - SISTEMA FORMAL DE EDGE
## Comienza aquí

**Última actualización:** 2026-01-03

---

## 30 SEGUNDOS: QUÉ ES ESTO

Un sistema que:
1. Define formalmente CUÁNDO existe tu edge (contexto)
2. Detecta AUTOMÁTICAMENTE si tu edge está muriendo (4 señales)
3. AUTOMÁTICAMENTE reduce posición o para trading si es necesario
4. Te guía en cada trade con reglas claras

**Resultado:** Operas solo cuando estadísticamente es correcto.

---

## 5 MINUTOS: LEE ESTOS DOCUMENTOS

En este orden:

### 1. EDGE_DEFINITION_OPERATIONS.md
- QUÉ ES EL EDGE (definición formal)
- CUÁNDO NO EXISTE (5 condiciones)
- CUÁNDO SE MUERE (criterios de muerte)

**Tiempo:** 10 min  
**Crítico:** SÍ

---

### 2. TRADER_OPERATIONAL_CHECKLIST.md
- QUÉ HACES CADA DÍA (checklist pre-mercado)
- QUÉ HACES ANTES DE CADA TRADE (validaciones)
- QUÉ HACES DESPUÉS DE CADA TRADE (recording)

**Tiempo:** 5 min  
**Crítico:** SÍ (úsalo todos los días)

---

### 3. TRADER_TRAINING_MANUAL.md
- POR QUÉ FUNCIONA EL SISTEMA (filosofía)
- CUÁL ES TU ROL (ejecutor vs decisor)
- QUÉ SUCEDE SI TODO COLAPSA (ciclo de muerte)

**Tiempo:** 20 min (después, puedes releer)  
**Crítico:** SÍ (pero puedes estudiar en paralelo)

---

## ANTES DE OPERAR

### Paso 1: Entender los 4 Contextos de NO-EDGE

Si CUALQUIERA es cierto → NO OPERES ese día:

1. **ATR baja:** ATR < P30 (60 días)
2. **Rango comprimido:** Rango < 0.7 × promedio
3. **Post-macro shock:** Día anterior a evento macro sin follow
4. **Expansión desaparece:** 2+ sesiones sin mover > 1R
5. **Volumen cae:** London→NY ratio < 0.8

**Acción:** Cada mañana, chequea estos 5.

---

### Paso 2: Entender las 4 Señales de Degradación

El sistema detecta automáticamente si el edge está muriendo:

1. **MFE/MAE ratio < 1.5** → Winners pequeños vs losers
2. **BE stops > 40%** → Ruido en mercado
3. **Winners no extienden > 60%** → Falta continuidad
4. **Avg duration > 12h** → Setup perdiendo valor

**Si 2+ activas:** Sistema reduce posición a 50%  
**Tu acción:** Acepta la reducción, sigue operando

---

### Paso 3: Entender los 4 Estados

```
🟢 ACTIVE (100% size)
   └─ Normal. Contexto válido, no hay degrada.

🟡 DEGRADED (50% size)
   └─ 2+ señales detectadas. Sistema reduce. Sigue operando.

🟠 OBSERVATION (50% size)
   └─ Degradación persiste 14+ días. Periodo de observación.

🔴 DEAD (0% size)
   └─ Edge colapsó. STOP. Archivar estrategia.
```

**Las transiciones son AUTOMÁTICAS. Tú las respetas.**

---

## TU ROL DIARIO (5 PASOS)

### 1. PRE-MERCADO (antes de operar)

Checklist (5 min):
- [ ] ATR > P30?
- [ ] Rango > 0.7×avg?
- [ ] No post-macro shock?
- [ ] ≥2 expansiones recientes?
- [ ] Volume trend healthy?

**Si TODO ✓** → Puedes operar  
**Si CUALQUIER ✗** → WAIT o REDUCE risk

---

### 2. PRE-TRADE (antes de cada trade)

Validación automática (sistema hace esto):
- ¿Contexto es válido?
- ¿Estado ≠ DEAD?
- ¿Señales < 2?
- ¿DD < 18%?

**Si TODO ✓** → EJECUTA trade  
**Si CUALQUIER ✗** → SKIP trade

---

### 3. TRADE LIVE (mientras está abierto)

Manage per reglas:
- Respetar SL
- Respetar TP1 y TP2
- No mover SL para salvarse
- No extender TP sin razón

---

### 4. POST-TRADE (cuando sale)

Sistema registra automáticamente:
- Entry/exit
- MFE/MAE
- TP1/TP2 hit
- BE stop?

**Tu acción:** Verificar que log registró correctamente

---

### 5. MONITORING (cada hora)

Ver status:
```
State: 🟢 ACTIVE | 🟡 DEGRADED | 🟠 OBSERVATION | 🔴 DEAD
Signals: 0/4 | 1/4 | 2/4 | 3+/4
Context: VALID | INVALID (which?)
Position: 100% | 50% | 50% | 0%
```

**Si cambia state:** Acepta cambio, continúa

---

## LO QUE NUNCA HAGAS

```
❌ Cambiar parámetros antes de 30 trades
❌ Mover SL para reducir pérdida
❌ Operar cuando contexto inválido
❌ Usar 100% posición cuando sistema dice degraded
❌ Intentar "rescatar" la estrategia
❌ Operar cuando dead
❌ Ignorar signal de degradación
```

---

## LO QUE SIEMPRE HAGAS

```
✅ Ejecutar cuando sistema dice YES
✅ Esperar cuando sistema dice NO
✅ Respetar posición sizing (1.0x, 0.5x, 0.0x)
✅ Registrar trades (MFE, MAE, duración)
✅ Monitorear contexto (ATR, range, volume)
✅ Observar señales (4 indicadores)
✅ Aceptar transiciones (degraded, observation, dead)
```

---

## EJEMPLO: DÍA TÍPICO

### 08:00 - Pre-mercado
```
Chequeo:
✓ ATR = 150 vs P30 = 120 → OK
✓ Range = 200 vs Avg = 250 → OK (0.8 ratio)
✓ No macro event → OK
✓ 3 sesiones expansión > 1R → OK
✓ Volume trend = 0.9 → OK

State: 🟢 ACTIVE (100% size)
Decision: CAN TRADE TODAY
```

### 09:30 - Setup 1 appears
```
Validación:
✓ Context valid? YES
✓ State ≠ DEAD? YES
✓ Signals < 2? YES
✓ DD < 18%? YES

Decision: EXECUTE
Position: Base size × 1.0 = FULL
```

### 11:00 - Trade 1 exits
```
Result: +1.2R (winner)
MFE: 1500 pts (good)
MAE: 200 pts (good)
TP1: HIT ✓
TP2: HIT ✓
Duration: 1.5h

Sistema registra → signals still OK
State: Still 🟢 ACTIVE
```

### 13:00 - Setup 2 appears
```
Validación:
✓ Context? YES
✓ State? YES
✓ Signals? YES (still 0)
✓ DD? YES (still 5%)

Execute. Proceeds fine.
```

### 15:00 - Trade 2 exits
```
Result: -0.8R (loser)
MFE: 150 pts (small)
MAE: 800 pts (large)
Ratio: 150/800 = 0.19 (bad)

Trade 3 exits also loss:
Total signal issues: MFE/MAE now 1.2 + BE stops 2/10

Still < 2 signals active. State: 🟢 ACTIVE
Continue
```

### 17:00 - Setup 3 appears
```
Execute, exits as loss.

Now:
MFE/MAE: 1.1 (signal 1 active)
BE stops: 3/10 = 30% (OK)
Winners extend: 1/1 = 100% (OK)
Duration: 1h avg (OK)

Signals: 1/4 active.
System: Still 🟢 ACTIVE
Trader: Keep monitoring
```

### 18:00 - Hourly check
```
State: 🟢 ACTIVE
Signals: 1 (warning, monitor)
Win rate: 1/3 = 33% (within variance for N=3)
Context: Still VALID
Position: 100%

No action needed. Continue.
```

### End of day
```
Total: 3 trades (N=3)
Win rate: 33% (normal variance)
Degradation: 1 signal (monitor)
Decision: Normal operation tomorrow if context valid
```

---

## EJEMPLO: CUANDO DEGRADA

### Day 2 - Trade 4 exits
```
Result: Loss with:
MFE: 100, MAE: 700 → MFE/MAE = 0.14
Plus BE stop

Now signals = 2:
1. MFE/MAE < 1.5 ✓
2. BE stops > 40% ✓

SYSTEM ALERT: Degradation detected!
State transition: 🟢 ACTIVE → 🟡 DEGRADED
Position multiplier: 1.0 → 0.5

TRADER ACTION:
├─ Acknowledge alert
├─ Accept 50% sizing
└─ Continue with same rules
```

### Day 2 onwards - While degraded
```
Trades: 5, 6, 7... at 50% size
Monitoring: Watch if signals clear

After 10 trades:
├─ MFE/MAE improves to 1.6 ✓
├─ BE stops drop to 35% ✓
└─ Signals < 2

SYSTEM: Signals cleared!
Transition: 🟡 DEGRADED → 🟢 ACTIVE
Position: 1.0x restored

TRADER: Continue at 100%
```

---

## EJEMPLO: CUANDO MUERE

### 150 trades de data
```
Windows evaluation (50 trades each):
├─ Window 1 (trades 1-50): Expectancy = -0.1R ✗
├─ Window 2 (trades 51-100): Expectancy = -0.05R ✗
└─ Window 3 (trades 101-150): Expectancy = -0.08R ✗

3 CONSECUTIVE NEGATIVE WINDOWS!
Edge is dead.
```

### System response
```
Transition: 🟡/🟢 → 🔴 DEAD
Position multiplier: 0.0
Message: STRATEGY ARCHIVED
```

### Trader action
```
1. Stop trading (system won't allow anyway)
2. Review post-mortem
3. Document failure cause
4. Archive strategy files
5. Move to next edge/strategy

This is SUCCESS, not failure.
You correctly identified the edge died.
```

---

## FREQUENTLY ASKED QUESTIONS

**Q: Can I override a state transition?**  
A: No. The system is intentionally automated to prevent emotional decisions.

**Q: What if I disagree the edge is dead?**  
A: Irrelevant. The metrics showed it. Trust the system.

**Q: Can I add parameters to improve performance?**  
A: Not before N=30 trades. Let the system speak first.

**Q: What if the context seems invalid but I "feel" market is good?**  
A: Feeling ≠ data. Don't trade. Respect the checklist.

**Q: How often should I review the rules?**  
A: Daily (pre-market) and weekly (full review).

**Q: What happens if I violate these rules?**  
A: You lose the statistical edge and become a gambling trader.

---

## NEXT STEPS

### Step 1 (Today)
- [ ] Read EDGE_DEFINITION_OPERATIONS.md
- [ ] Read TRADER_OPERATIONAL_CHECKLIST.md
- [ ] Accept the system philosophy

### Step 2 (Tomorrow)
- [ ] Study TRADER_TRAINING_MANUAL.md
- [ ] Understand system architecture
- [ ] Review example_edge_integration.py

### Step 3 (This Week)
- [ ] Read SYSTEM_ARCHITECTURE_EDGE.md
- [ ] Test the code modules
- [ ] Prepare execution.py integration

### Step 4 (Next Week)
- [ ] Integrate EdgeEnforcement into execution.py
- [ ] Test end-to-end flows
- [ ] Go live with system active

---

## CONTACTS & SUPPORT

**Questions about philosophy?**  
→ Review TRADER_TRAINING_MANUAL.md section 1-4

**Questions about daily procedure?**  
→ Use TRADER_OPERATIONAL_CHECKLIST.md

**Questions about code?**  
→ See SYSTEM_ARCHITECTURE_EDGE.md

**Questions about implementation?**  
→ See example_edge_integration.py

---

## THE BOTTOM LINE

```
You have a working edge.
It only works in certain conditions.
This system tells you when those conditions exist.
When they don't, it stops you trading.
When they break, it kills you automatically.

Your job: Execute the system perfectly.
System's job: Make smart decisions.

Trust it.
```

---

**Welcome to professional trading.**

Start with the checklist.
Do it daily.
Let the system do the rest.

Good luck.
