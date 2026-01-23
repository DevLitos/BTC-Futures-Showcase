#!/bin/bash
# ============================================================
# DayTrader 2.1 - Docker Deployment Script
# ============================================================
#
# USO:
#   ./scripts/docker-deploy.sh [comando]
#
# COMANDOS:
#   setup     - Configuración inicial (crear archivos, permisos)
#   build     - Construir imagen con versión
#   up        - Iniciar servicios
#   down      - Detener servicios
#   logs      - Ver logs
#   status    - Estado de servicios
#   rollback  - Volver a versión anterior
#   kill-on   - Activar kill switch
#   kill-off  - Desactivar kill switch
#
# ============================================================

set -euo pipefail

# Colores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Directorio del proyecto
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Versión actual
GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
VERSION="${VERSION:-2.1.0}"

log_info()  { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
log_error() { echo -e "${RED}[✗]${NC} $1"; }
log_header() {
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}  DayTrader 2.1 - $1${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

# ============================================================
# SETUP - Configuración inicial segura
# ============================================================
cmd_setup() {
    log_header "Setup Inicial"
    
    # Crear directorios
    log_info "Creando directorios..."
    mkdir -p logs storage config output trade_logs backups
    
    # Crear archivo de secretos si no existe
    if [ ! -f ".env.secrets" ]; then
        if [ -f ".env.secrets.example" ]; then
            cp .env.secrets.example .env.secrets
            log_warn "Creado .env.secrets - ¡EDITAR CON TUS CREDENCIALES!"
        else
            log_error ".env.secrets.example no encontrado"
            exit 1
        fi
    fi
    
    # ⚠️ SEGURIDAD: Permisos restrictivos para secretos
    log_info "Configurando permisos de seguridad..."
    chmod 600 .env.secrets
    chmod 700 logs storage backups
    
    # Kill switch (vacío = trading permitido)
    touch KILL_SWITCH
    > KILL_SWITCH
    
    # Verificar .gitignore
    if ! grep -q ".env.secrets" .gitignore 2>/dev/null; then
        log_warn "Añadiendo .env.secrets a .gitignore"
        echo ".env.secrets" >> .gitignore
    fi
    
    log_info "Setup completado"
    echo ""
    log_warn "SIGUIENTE PASO: Editar .env.secrets con tus credenciales reales"
    echo "    nano .env.secrets"
}

# ============================================================
# BUILD - Construir con versión
# ============================================================
cmd_build() {
    log_header "Build"
    
    IMAGE_TAG="daytrader:${GIT_COMMIT}"
    
    log_info "Building: $IMAGE_TAG"
    log_info "Commit: $GIT_COMMIT"
    log_info "Time: $BUILD_TIME"
    
    docker build \
        --build-arg GIT_COMMIT="$GIT_COMMIT" \
        --build-arg BUILD_TIME="$BUILD_TIME" \
        --build-arg VERSION="$VERSION" \
        -t "$IMAGE_TAG" \
        -t "daytrader:latest" \
        .
    
    log_info "Build completado: $IMAGE_TAG"
    
    # Guardar versión actual para rollback
    echo "$GIT_COMMIT" > .last_version
}

# ============================================================
# UP - Iniciar servicios
# ============================================================
cmd_up() {
    log_header "Iniciando Servicios"
    
    # Verificar secretos
    if [ ! -f ".env.secrets" ]; then
        log_error ".env.secrets no encontrado. Ejecuta: ./scripts/docker-deploy.sh setup"
        exit 1
    fi
    
    # Verificar permisos
    PERMS=$(stat -c "%a" .env.secrets 2>/dev/null || stat -f "%Lp" .env.secrets)
    if [ "$PERMS" != "600" ]; then
        log_warn "Corrigiendo permisos de .env.secrets"
        chmod 600 .env.secrets
    fi
    
    # Verificar que hay credenciales
    if grep -q "^KRAKEN_FUTURES_API_KEY=$" .env.secrets; then
        log_error "API KEY vacía en .env.secrets"
        exit 1
    fi
    
    export GIT_COMMIT BUILD_TIME VERSION
    docker compose up -d
    
    log_info "Servicios iniciados"
    echo ""
    echo "URLs:"
    echo "  Health:     http://localhost:8001/healthz"
    echo "  Readiness:  http://localhost:8001/readyz"
    echo "  Prometheus: http://localhost:9090"
    echo "  Grafana:    http://localhost:3000"
}

# ============================================================
# DOWN - Detener servicios
# ============================================================
cmd_down() {
    log_header "Deteniendo Servicios"
    docker compose down
    log_info "Servicios detenidos"
}

# ============================================================
# LOGS
# ============================================================
cmd_logs() {
    SERVICE="${2:-trading-bot}"
    docker compose logs -f "$SERVICE"
}

# ============================================================
# STATUS
# ============================================================
cmd_status() {
    log_header "Estado"
    
    docker compose ps
    echo ""
    
    # Health checks
    echo "Health Checks:"
    if curl -sf http://localhost:8001/healthz > /dev/null 2>&1; then
        log_info "Trading Bot: HEALTHY"
    else
        log_error "Trading Bot: NOT RESPONDING"
    fi
    
    if curl -sf http://localhost:8001/readyz > /dev/null 2>&1; then
        log_info "Trading Bot: READY"
    else
        log_warn "Trading Bot: NOT READY"
    fi
    
    # Versión actual
    if [ -f ".last_version" ]; then
        echo ""
        echo "Versión desplegada: $(cat .last_version)"
    fi
    
    # Kill switch
    echo ""
    if [ -s "KILL_SWITCH" ]; then
        log_warn "KILL SWITCH: ACTIVO - Trading PAUSADO"
    else
        log_info "Kill Switch: OFF - Trading permitido"
    fi
}

# ============================================================
# ROLLBACK - Volver a versión anterior
# ============================================================
cmd_rollback() {
    log_header "Rollback"
    
    TARGET="${2:-}"
    
    if [ -z "$TARGET" ]; then
        echo "Imágenes disponibles:"
        docker images daytrader --format "table {{.Tag}}\t{{.CreatedAt}}\t{{.Size}}"
        echo ""
        echo "Uso: ./scripts/docker-deploy.sh rollback <tag>"
        exit 1
    fi
    
    if ! docker image inspect "daytrader:$TARGET" > /dev/null 2>&1; then
        log_error "Imagen daytrader:$TARGET no encontrada"
        exit 1
    fi
    
    log_warn "Rolling back a: daytrader:$TARGET"
    read -p "¿Continuar? (y/N) " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker tag "daytrader:$TARGET" daytrader:latest
        docker compose up -d
        echo "$TARGET" > .last_version
        log_info "Rollback completado a: $TARGET"
    else
        log_info "Rollback cancelado"
    fi
}

# ============================================================
# KILL SWITCH
# ============================================================
cmd_kill_on() {
    log_header "Kill Switch ON"
    echo "KILL_ACTIVE=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > KILL_SWITCH
    log_warn "Trading PAUSADO"
}

cmd_kill_off() {
    log_header "Kill Switch OFF"
    > KILL_SWITCH
    log_info "Trading PERMITIDO"
}

# ============================================================
# CLEAN - Limpieza completa
# ============================================================
cmd_clean() {
    log_header "Limpieza"
    log_warn "Esto eliminará contenedores, imágenes y volúmenes"
    read -p "¿Continuar? (y/N) " -n 1 -r
    echo
    
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker compose down -v --rmi local
        log_info "Limpieza completada"
    fi
}

# ============================================================
# HELP
# ============================================================
cmd_help() {
    log_header "Ayuda"
    echo ""
    echo "Uso: $0 [comando]"
    echo ""
    echo "Comandos:"
    echo "  setup      Configuración inicial (EJECUTAR PRIMERO)"
    echo "  build      Construir imagen Docker con versión"
    echo "  up         Iniciar servicios"
    echo "  down       Detener servicios"
    echo "  logs       Ver logs (default: trading-bot)"
    echo "  status     Estado de servicios y health checks"
    echo "  rollback   Volver a versión anterior"
    echo "  kill-on    Activar kill switch (pausar trading)"
    echo "  kill-off   Desactivar kill switch (reanudar)"
    echo "  clean      Limpiar contenedores e imágenes"
    echo ""
    echo "Ejemplo completo:"
    echo "  $0 setup"
    echo "  nano .env.secrets  # Añadir credenciales"
    echo "  $0 build"
    echo "  $0 up"
    echo "  $0 status"
}

# ============================================================
# MAIN
# ============================================================
case "${1:-help}" in
    setup)    cmd_setup ;;
    build)    cmd_build ;;
    up)       cmd_up ;;
    down)     cmd_down ;;
    logs)     cmd_logs "$@" ;;
    status)   cmd_status ;;
    rollback) cmd_rollback "$@" ;;
    kill-on)  cmd_kill_on ;;
    kill-off) cmd_kill_off ;;
    clean)    cmd_clean ;;
    help|*)   cmd_help ;;
esac
