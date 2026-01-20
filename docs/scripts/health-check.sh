#!/bin/bash
# ============================================================
# DayTrader 2.1 - Health Check Operativo
# ============================================================
#
# Verifica estado OPERATIVO, no solo técnico:
# - Proceso vivo
# - Conexión a exchange
# - Datos recientes
# - Latencia aceptable
# - Sin errores críticos
#
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
log_fail() { echo -e "${RED}[✗]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[!]${NC} $1"; }

ERRORS=0

echo "============================================================"
echo "  DayTrader 2.1 - Health Check"
echo "============================================================"
echo ""

# ============================================================
# 1. Trading Bot - Liveness
# ============================================================
echo "1. Trading Bot (Liveness)..."
if curl -sf http://localhost:8001/healthz > /dev/null 2>&1; then
    log_ok "Proceso VIVO"
else
    log_fail "Proceso NO RESPONDE"
    ERRORS=$((ERRORS + 1))
fi

# ============================================================
# 2. Trading Bot - Readiness (operativo)
# ============================================================
echo ""
echo "2. Trading Bot (Readiness)..."
READY_RESPONSE=$(curl -sf http://localhost:8001/readyz 2>/dev/null || echo "FAILED")
if [ "$READY_RESPONSE" != "FAILED" ]; then
    log_ok "READY para operar"
    # Mostrar detalles si es JSON
    if echo "$READY_RESPONSE" | grep -q "{"; then
        echo "   $READY_RESPONSE"
    fi
else
    log_warn "NO READY (puede estar iniciando)"
    ERRORS=$((ERRORS + 1))
fi

# ============================================================
# 3. Prometheus
# ============================================================
echo ""
echo "3. Prometheus..."
if curl -sf http://localhost:9090/-/healthy > /dev/null 2>&1; then
    log_ok "Prometheus OK"
else
    log_warn "Prometheus NO RESPONDE"
fi

# ============================================================
# 4. Grafana
# ============================================================
echo ""
echo "4. Grafana..."
if curl -sf http://localhost:3000/api/health > /dev/null 2>&1; then
    log_ok "Grafana OK"
else
    log_warn "Grafana NO RESPONDE"
fi

# ============================================================
# 5. Kill Switch
# ============================================================
echo ""
echo "5. Kill Switch..."
if [ -f "KILL_SWITCH" ] && [ -s "KILL_SWITCH" ]; then
    log_warn "KILL SWITCH ACTIVO - Trading PAUSADO"
    cat KILL_SWITCH
else
    log_ok "Kill Switch OFF - Trading permitido"
fi

# ============================================================
# 6. Recursos del sistema
# ============================================================
echo ""
echo "6. Recursos..."

# Disco
DISK_PCT=$(df -h . | tail -1 | awk '{gsub(/%/,""); print $5}')
if [ "$DISK_PCT" -lt 80 ]; then
    log_ok "Disco: ${DISK_PCT}%"
elif [ "$DISK_PCT" -lt 90 ]; then
    log_warn "Disco: ${DISK_PCT}% (alto)"
else
    log_fail "Disco: ${DISK_PCT}% (CRÍTICO)"
    ERRORS=$((ERRORS + 1))
fi

# Memoria de contenedores
echo ""
echo "7. Contenedores..."
docker stats --no-stream --format "   {{.Name}}: {{.MemUsage}} | CPU: {{.CPUPerc}}" 2>/dev/null || true

# ============================================================
# Resumen
# ============================================================
echo ""
echo "============================================================"
if [ $ERRORS -eq 0 ]; then
    log_ok "Sistema OPERATIVO"
    exit 0
else
    log_fail "Sistema con $ERRORS problema(s)"
    exit 1
fi
