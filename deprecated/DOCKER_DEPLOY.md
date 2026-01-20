# ============================================================
# DayTrader 2.1 - Docker Deployment (PRODUCCIÓN)
# ============================================================

## ⚠️ ADVERTENCIA

Este sistema está configurado en **LIVE MODE por defecto**.  
Dinero real. Sin red de seguridad.

---

## Quick Start

```bash
# 1. Setup inicial (OBLIGATORIO primero)
./scripts/docker-deploy.sh setup

# 2. Configurar credenciales REALES
nano .env.secrets

# 3. Build con versión
./scripts/docker-deploy.sh build

# 4. Iniciar
./scripts/docker-deploy.sh up

# 5. Verificar
./scripts/docker-deploy.sh status
```

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                    STACK MÍNIMO (por defecto)               │
│                                                             │
│  ┌─────────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │  trading-bot    │  │  prometheus │  │   grafana   │     │
│  │  (LIVE MODE)    │  │   :9090     │  │   :3000     │     │
│  │  :8000 :8001    │  │             │  │             │     │
│  └────────┬────────┘  └──────┬──────┘  └──────┬──────┘     │
│           │                  │                 │            │
│           └──────────────────┴─────────────────┘            │
│                     trading-network                         │
└─────────────────────────────────────────────────────────────┘

Kafka: OPCIONAL (--profile full). Solo si necesitas audit trail/replay.
```

---

## Archivos de Configuración

| Archivo | Descripción | En Git? |
|---------|-------------|---------|
| `.env.secrets` | **CREDENCIALES** (API keys) | ❌ NUNCA |
| `.env.config` | Settings operativos | ✅ Sí |
| `docker-compose.yml` | Servicios Docker | ✅ Sí |
| `Dockerfile` | Imagen del bot | ✅ Sí |

### Seguridad de `.env.secrets`

```bash
# OBLIGATORIO: Permisos restrictivos
chmod 600 .env.secrets

# Verificar que está en .gitignore
grep ".env.secrets" .gitignore
```

---

## Comandos

| Comando | Descripción |
|---------|-------------|
| `./scripts/docker-deploy.sh setup` | Configuración inicial |
| `./scripts/docker-deploy.sh build` | Build con versión (git commit) |
| `./scripts/docker-deploy.sh up` | Iniciar servicios |
| `./scripts/docker-deploy.sh down` | Detener servicios |
| `./scripts/docker-deploy.sh logs` | Ver logs del bot |
| `./scripts/docker-deploy.sh status` | Health checks + estado |
| `./scripts/docker-deploy.sh rollback <tag>` | Volver a versión anterior |
| `./scripts/docker-deploy.sh kill-on` | **PAUSAR TRADING** |
| `./scripts/docker-deploy.sh kill-off` | Reanudar trading |

---

## Rollback

Cada build crea una imagen versionada:

```bash
# Ver versiones disponibles
docker images daytrader

# Rollback a versión específica
./scripts/docker-deploy.sh rollback abc1234
```

---

## Health Checks

El bot expone dos endpoints críticos:

| Endpoint | Qué verifica |
|----------|--------------|
| `/healthz` | Proceso vivo (liveness) |
| `/readyz` | Listo para operar (readiness) |

### Qué incluye `/readyz`:
- ✅ Proceso vivo
- ✅ Conexión a exchange activa
- ✅ Datos recibidos en últimos 60s
- ✅ Sin errores críticos
- ✅ Clock sync < 500ms drift

---

## Límites de Recursos

Configurados en `docker-compose.yml`:

```yaml
deploy:
  resources:
    limits:
      cpus: '1.0'
      memory: 1G
    reservations:
      cpus: '0.25'
      memory: 256M
```

---

## Logs

Rotación automática configurada:
- Max 10MB por archivo
- Max 5 archivos
- Compresión habilitada

```bash
# Ver logs en tiempo real
./scripts/docker-deploy.sh logs

# Logs de servicio específico
./scripts/docker-deploy.sh logs prometheus
```

---

## Kill Switch

Pausa inmediata de trading:

```bash
# ACTIVAR (pausar trading)
./scripts/docker-deploy.sh kill-on

# DESACTIVAR (reanudar)
./scripts/docker-deploy.sh kill-off
```

---

## Backups

```bash
# Crear backup (sin secretos)
./scripts/backup.sh

# Backups en ./backups/
# Retención: 30 días
```

**NUNCA incluye**: `.env.secrets`, `logs/`

---

## Troubleshooting

### Bot no arranca
```bash
# Ver logs
docker compose logs trading-bot

# Verificar secretos
cat .env.secrets | grep API_KEY
```

### Health check falla
```bash
# Verificar manualmente
curl http://localhost:8001/healthz
curl http://localhost:8001/readyz
```

### Rollback de emergencia
```bash
# Ver versiones
docker images daytrader

# Restaurar versión anterior
./scripts/docker-deploy.sh rollback <version>
```

---

## Checklist Pre-Producción

- [ ] `.env.secrets` con credenciales reales
- [ ] `chmod 600 .env.secrets`
- [ ] `.env.secrets` en `.gitignore`
- [ ] Telegram configurado para alertas
- [ ] Health checks respondiendo
- [ ] Grafana dashboard funcionando
- [ ] Backup script probado
- [ ] Rollback procedure probado
- [ ] Kill switch probado
