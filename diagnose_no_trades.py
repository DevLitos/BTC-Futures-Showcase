#!/usr/bin/env python3
"""
SCRIPT DE DIAGNÓSTICO: Bot Intradía Sin Trades
Ejecuta: python diagnose_no_trades.py

Este script analiza el estado del sistema y genera un reporte de diagnóstico.
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Añadir el directorio raíz al path
sys.path.insert(0, str(Path(__file__).parent))

from engine.config import (
    FundingRateControl, SessionQuality, DivergenceFilters,
    VPOCFilters, CorrelationFilters, HTFFilters, LTFFilters,
    KillConditions, RiskManagement, PostTrade, OrderExecution
)


def print_header(title: str):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def check_mark(condition: bool) -> str:
    return "✅" if condition else "❌"


def diagnose_config():
    """Analiza la configuración actual de filtros"""
    print_header("1. CONFIGURACIÓN DE FILTROS")
    
    print("\n📊 FILTROS TIER 2 (FIX #6-#12):")
    print(f"  {check_mark(not FundingRateControl.ENABLED)} Funding Rate Control: {'DESHABILITADO' if not FundingRateControl.ENABLED else f'HABILITADO (max {FundingRateControl.MAX_FUNDING_RATE_PCT}%)'}")
    print(f"  {check_mark(not SessionQuality.ENABLED)} Session Quality: {'DESHABILITADO' if not SessionQuality.ENABLED else 'HABILITADO'}")
    print(f"  {check_mark(not DivergenceFilters.ENABLED)} HTF Divergence: {'DESHABILITADO' if not DivergenceFilters.ENABLED else 'HABILITADO'}")
    print(f"  {check_mark(not VPOCFilters.ENABLED)} VPOC Filter: {'DESHABILITADO' if not VPOCFilters.ENABLED else 'HABILITADO'}")
    print(f"  {check_mark(not CorrelationFilters.ENABLED)} BTC Correlation: {'DESHABILITADO' if not CorrelationFilters.ENABLED else 'HABILITADO'}")
    
    # Contar filtros habilitados
    tier2_enabled = sum([
        FundingRateControl.ENABLED,
        SessionQuality.ENABLED,
        DivergenceFilters.ENABLED,
        VPOCFilters.ENABLED,
        CorrelationFilters.ENABLED
    ])
    
    if tier2_enabled >= 4:
        print(f"\n  ⚠️ ALERTA: {tier2_enabled}/5 filtros Tier 2 están habilitados.")
        print("     Esto puede bloquear ~80%+ de señales válidas.")
    
    print("\n📊 FILTROS HTF (1h):")
    print(f"  • MIN_ADX: {HTFFilters.MIN_ADX}")
    print(f"  • MIN_ADX_SLOPE: {HTFFilters.MIN_ADX_SLOPE}")
    print(f"  • MIN_ATR_VS_MA: {HTFFilters.MIN_ATR_VS_MA}")
    print(f"  • SMART_SLOPE_FILTER: {HTFFilters.SMART_SLOPE_FILTER}")
    
    print("\n📊 FILTROS LTF (1m):")
    print(f"  • MIN_RELATIVE_VOLUME: {LTFFilters.MIN_RELATIVE_VOLUME}")
    print(f"  • MIN_ORDERBOOK_IMBALANCE: {LTFFilters.MIN_ORDERBOOK_IMBALANCE}")
    
    print("\n📊 SESSION QUALITY (si habilitado):")
    if SessionQuality.ENABLED:
        print(f"  • WEEKEND_TRADING: {SessionQuality.WEEKEND_TRADING}")
        print(f"  • MIN_ADX por sesión: {SessionQuality.MIN_ADX_BY_SESSION}")
        print(f"  • MIN_VOLATILITY_BPS: {SessionQuality.MIN_VOLATILITY_BPS}")


def diagnose_session():
    """Analiza si la sesión actual pasaría el filtro"""
    print_header("2. ANÁLISIS DE SESIÓN ACTUAL")
    
    now = datetime.utcnow()
    hour = now.hour
    day = now.weekday()
    
    print(f"\n  📅 Fecha/Hora UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  📅 Día de la semana: {['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'][day]}")
    
    # Determinar sesión
    if 8 <= hour < 16:
        session = 'london'
    elif 14 <= hour < 23:
        session = 'newyork'
    else:
        session = 'asia'
    
    print(f"  📍 Sesión actual: {session.upper()}")
    
    # Check weekend
    is_weekend = (day == 4 and hour >= 17) or day >= 5
    if is_weekend:
        print(f"\n  ⚠️ ES FIN DE SEMANA")
        if not SessionQuality.WEEKEND_TRADING:
            print(f"  ❌ WEEKEND_TRADING está DESHABILITADO - NO SE GENERARÁN TRADES")
    
    # Mostrar requisitos de sesión
    if SessionQuality.ENABLED:
        min_adx = SessionQuality.MIN_ADX_BY_SESSION.get(session, 20)
        min_vol = SessionQuality.MIN_VOLATILITY_BPS.get(session, 80)
        print(f"\n  📋 Requisitos para sesión '{session}':")
        print(f"     • ADX mínimo: {min_adx}")
        print(f"     • Volatilidad mínima: {min_vol} bps")


def diagnose_cooldowns():
    """Analiza configuración de cooldowns"""
    print_header("3. CONFIGURACIÓN DE COOLDOWNS")
    
    print(f"\n  • Post-trade cooldown: {PostTrade.COOLDOWN_MINUTES} min")
    print(f"  • Black Swan cooldown: {KillConditions.BLACK_SWAN_COOLDOWN_MINUTES} min")
    print(f"  • Entry timeout: {OrderExecution.ENTRY_TIMEOUT_CANDLES} velas ({OrderExecution.FILL_TIMEOUT_SECONDS}s)")
    
    # Buscar cooldown de 60 minutos en signal_engine
    signal_engine_path = Path(__file__).parent / 'engine' / 'signal_engine.py'
    if signal_engine_path.exists():
        with open(signal_engine_path, 'r') as f:
            content = f.read()
            if 'start_cooldown(60)' in content:
                print(f"\n  ⚠️ ALERTA: Se detectó cooldown de 60 minutos tras fallo de orden")
                print(f"     Esto puede dejar el bot inactivo por 1 hora tras cualquier error.")
            elif 'start_cooldown(5)' in content:
                print(f"\n  ✅ Cooldown tras fallo de orden: 5 minutos (FIX aplicado)")


def diagnose_kill_conditions():
    """Analiza Kill Conditions"""
    print_header("4. KILL CONDITIONS")
    
    print(f"\n  • Max Daily Drawdown: {KillConditions.MAX_DAILY_DRAWDOWN_PCT}%")
    print(f"  • Max Consecutive Losses: {KillConditions.MAX_CONSECUTIVE_LOSSES}")
    print(f"  • Max Funding Rate: {KillConditions.MAX_FUNDING_RATE * 100:.2f}%")
    print(f"  • WebSocket Timeout: {KillConditions.WEBSOCKET_TIMEOUT_SECONDS}s")
    print(f"  • Black Swan Multiplier: {KillConditions.BLACK_SWAN_MULTIPLIER}x ATR")
    print(f"  • Defensive Mode Multiplier: {KillConditions.DEFENSIVE_MODE_MULTIPLIER}x ATR")


def diagnose_risk_management():
    """Analiza Risk Management"""
    print_header("5. RISK MANAGEMENT")
    
    print(f"\n  • Default Leverage: {RiskManagement.DEFAULT_LEVERAGE}x")
    print(f"  • Risk London/NY: {RiskManagement.RISK_LONDON_NY_PCT}%")
    print(f"  • Risk NY Open: {RiskManagement.RISK_NY_OPEN_PCT}%")
    print(f"  • Risk Asia/Weekend: {RiskManagement.RISK_ASIA_WEEKEND_PCT}%")
    print(f"  • Min Position Size: ${OrderExecution.MIN_POSITION_SIZE_USD}")
    print(f"  • TP1 Multiple: {RiskManagement.TP1_RISK_MULTIPLE}R")
    print(f"  • Trailing Stop ATR: {RiskManagement.TRAILING_STOP_ATR_MULTIPLIER}x")


def estimate_filter_probability():
    """Estima probabilidad de que una señal pase todos los filtros"""
    print_header("6. ESTIMACIÓN DE PROBABILIDAD DE TRADE")
    
    # Probabilidades aproximadas basadas en configuración
    p_htf = 0.60  # HTF pasa ~60% del tiempo
    p_ltf = 0.50  # LTF pasa ~50% del tiempo
    
    # Tier 2
    p_funding = 0.85 if FundingRateControl.ENABLED else 1.0
    p_session = 0.50 if SessionQuality.ENABLED else 1.0  # Muy restrictivo
    p_divergence = 0.75 if DivergenceFilters.ENABLED else 1.0
    p_vpoc = 0.70 if VPOCFilters.ENABLED else 1.0
    p_btc = 0.65 if CorrelationFilters.ENABLED else 1.0
    
    p_total = p_htf * p_ltf * p_funding * p_session * p_divergence * p_vpoc * p_btc
    
    print(f"\n  📊 Probabilidad estimada de que una señal RAW pase todos los filtros:")
    print(f"\n     P(HTF pass) ≈ {p_htf*100:.0f}%")
    print(f"     P(LTF pass) ≈ {p_ltf*100:.0f}%")
    print(f"     P(Funding pass) ≈ {p_funding*100:.0f}%")
    print(f"     P(Session pass) ≈ {p_session*100:.0f}%")
    print(f"     P(Divergence pass) ≈ {p_divergence*100:.0f}%")
    print(f"     P(VPOC pass) ≈ {p_vpoc*100:.0f}%")
    print(f"     P(BTC trend pass) ≈ {p_btc*100:.0f}%")
    print(f"\n     ────────────────────────────")
    print(f"     P(TRADE EJECUTADO) ≈ {p_total*100:.1f}%")
    
    if p_total < 0.10:
        print(f"\n  🔴 CRÍTICO: Solo ~{p_total*100:.0f}% de señales llegarán a ejecutarse")
        print(f"     Considera deshabilitar algunos filtros Tier 2.")
    elif p_total < 0.25:
        print(f"\n  🟡 ADVERTENCIA: Solo ~{p_total*100:.0f}% de señales llegarán a ejecutarse")


def check_observation_deadlock():
    """Verifica si hay riesgo de deadlock en OBSERVATION mode"""
    print_header("7. ANÁLISIS DE OBSERVATION MODE")
    
    state_machine_path = Path(__file__).parent / 'engine' / 'state_machine.py'
    if state_machine_path.exists():
        with open(state_machine_path, 'r') as f:
            content = f.read()
            
            has_min_trades = 'trades_in_observation < 5' in content or 'trades_in_observation >= 5' in content
            has_wr_check = 'wins_in_observation' in content
            has_timeout = 'OBSERVATION_MAX_HOURS' in content or '24 * 60' in content or 'observation_timeout' in content.lower()
            
            print(f"\n  Estado de OBSERVATION mode:")
            print(f"     • Requiere 5 trades para salir: {'✅ Sí' if has_min_trades else '❓ No detectado'}")
            print(f"     • Verifica Win Rate: {'✅ Sí' if has_wr_check else '❓ No detectado'}")
            print(f"     • Tiene timeout de emergencia: {'✅ Sí' if has_timeout else '❌ NO - Riesgo de deadlock'}")
            
            if has_min_trades and not has_timeout:
                print(f"\n  ⚠️ ALERTA: OBSERVATION mode requiere 5 trades para salir,")
                print(f"     pero el bot NO genera trades en OBSERVATION mode.")
                print(f"     Esto es un DEADLOCK potencial.")


def generate_recommendations():
    """Genera recomendaciones basadas en el diagnóstico"""
    print_header("8. RECOMENDACIONES")
    
    recommendations = []
    completed = []
    
    # Tier 2 filters
    tier2_enabled = sum([
        FundingRateControl.ENABLED,
        SessionQuality.ENABLED,
        DivergenceFilters.ENABLED,
        VPOCFilters.ENABLED,
        CorrelationFilters.ENABLED
    ])
    
    if tier2_enabled >= 3:
        recommendations.append("🔧 Deshabilitar más filtros Tier 2 para diagnóstico")
    else:
        completed.append("✅ Filtros Tier 2 reducidos (3/5 deshabilitados)")
    
    if SessionQuality.ENABLED:
        recommendations.append("🔧 Revisar Session Quality - puede bloquear en Asia/Weekend")
    else:
        completed.append("✅ Session Quality deshabilitado")
    
    if SessionQuality.WEEKEND_TRADING:
        completed.append("✅ Weekend Trading habilitado")
    else:
        recommendations.append("🔧 Habilitar WEEKEND_TRADING para diagnóstico")
    
    # Cooldowns - check actual file
    signal_engine_path = Path(__file__).parent / 'engine' / 'signal_engine.py'
    if signal_engine_path.exists():
        with open(signal_engine_path, 'r') as f:
            content = f.read()
            if 'start_cooldown(5)' in content:
                completed.append("✅ Cooldown post-error reducido a 5min")
            elif 'start_cooldown(60)' in content:
                recommendations.append("🔧 Reducir cooldown post-error de 60min a 5min")
    
    # OBSERVATION timeout
    state_machine_path = Path(__file__).parent / 'engine' / 'state_machine.py'
    if state_machine_path.exists():
        with open(state_machine_path, 'r') as f:
            content = f.read()
            if 'OBSERVATION_MAX_HOURS' in content:
                completed.append("✅ Timeout de 24h para OBSERVATION añadido")
            else:
                recommendations.append("🔧 Añadir timeout de 24h para OBSERVATION mode")
    
    # Leverage
    if RiskManagement.DEFAULT_LEVERAGE >= 1:
        completed.append(f"✅ Leverage configurado: {RiskManagement.DEFAULT_LEVERAGE}x")
    else:
        recommendations.append("🔧 Configurar leverage >= 1x")
    
    # Logging
    recommendations.append("🔧 Añadir logging después de cada filtro Tier 2")
    
    if completed:
        print("\n  ✅ Cambios ya aplicados:\n")
        for i, rec in enumerate(completed, 1):
            print(f"     {i}. {rec}")
    
    if recommendations:
        print("\n  📋 Acciones pendientes:\n")
        for i, rec in enumerate(recommendations, 1):
            print(f"     {i}. {rec}")


def main():
    print("\n" + "🔍" * 30)
    print("   DIAGNÓSTICO: BOT INTRADÍA SIN TRADES")
    print("🔍" * 30)
    print(f"\nFecha de análisis: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    diagnose_config()
    diagnose_session()
    diagnose_cooldowns()
    diagnose_kill_conditions()
    diagnose_risk_management()
    estimate_filter_probability()
    check_observation_deadlock()
    generate_recommendations()
    
    print("\n" + "=" * 60)
    print("  FIN DEL DIAGNÓSTICO")
    print("=" * 60)
    print("\n📄 Ver DIAGNOSTICO_BOT_SIN_TRADES.md para análisis detallado")
    print()


if __name__ == "__main__":
    main()
