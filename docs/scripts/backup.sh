#!/bin/bash
# ============================================================
# DayTrader 2.1 - Backup Script (SEGURO)
# ============================================================
#
# IMPORTANTE: Este script NUNCA incluye secretos en backups
#
# ============================================================

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="daytrader_backup_${DATE}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
log_error() { echo -e "${RED}[✗]${NC} $1"; }

echo "============================================================"
echo "  DayTrader 2.1 - Backup Seguro"
echo "============================================================"
echo ""

# Crear directorio de backup
mkdir -p "$BACKUP_DIR"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$BACKUP_PATH"

log_info "Creando backup: $BACKUP_NAME"

# ============================================================
# ARCHIVOS A INCLUIR (sin secretos)
# ============================================================

# Storage (parquet files, state)
if [ -d "storage" ]; then
    log_info "Backing up storage..."
    cp -r storage "$BACKUP_PATH/"
fi

# Trade logs
if [ -d "trade_logs" ]; then
    log_info "Backing up trade_logs..."
    cp -r trade_logs "$BACKUP_PATH/"
fi

# Config (sin secretos)
if [ -d "config" ]; then
    log_info "Backing up config..."
    cp -r config "$BACKUP_PATH/"
fi

# Versión desplegada
if [ -f ".last_version" ]; then
    cp .last_version "$BACKUP_PATH/"
fi

# ============================================================
# ⚠️ ARCHIVOS EXCLUIDOS (SEGURIDAD)
# ============================================================
log_warn "Excluidos (seguridad): .env.secrets, logs/"

# Crear archivo de metadata
cat > "$BACKUP_PATH/BACKUP_INFO.txt" << EOF
Backup: $BACKUP_NAME
Fecha: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
Host: $(hostname)
Versión: $(cat .last_version 2>/dev/null || echo "unknown")

CONTENIDO:
- storage/     (estado persistente)
- trade_logs/  (historial de trades)
- config/      (configuración pública)

EXCLUIDO (seguridad):
- .env.secrets (credenciales)
- logs/        (potencialmente sensible)

Para restaurar:
  tar -xzf ${BACKUP_NAME}.tar.gz
  cp -r ${BACKUP_NAME}/storage ./
  cp -r ${BACKUP_NAME}/trade_logs ./
EOF

# Crear tarball
log_info "Comprimiendo..."
cd "$BACKUP_DIR"
tar -czf "${BACKUP_NAME}.tar.gz" "$BACKUP_NAME"
rm -rf "$BACKUP_NAME"
cd - > /dev/null

BACKUP_SIZE=$(ls -lh "$BACKUP_DIR/${BACKUP_NAME}.tar.gz" | awk '{print $5}')
log_info "Backup creado: $BACKUP_DIR/${BACKUP_NAME}.tar.gz ($BACKUP_SIZE)"

# Limpiar backups antiguos
log_info "Limpiando backups > $RETENTION_DAYS días..."
find "$BACKUP_DIR" -name "daytrader_backup_*.tar.gz" -mtime +$RETENTION_DAYS -delete 2>/dev/null || true

# Listar backups
echo ""
echo "Backups disponibles:"
ls -lh "$BACKUP_DIR"/*.tar.gz 2>/dev/null || echo "  (ninguno)"

echo ""
log_info "Backup completado"
