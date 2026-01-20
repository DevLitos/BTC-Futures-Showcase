#!/usr/bin/env python3
"""
V2.1 System Demo - Verificación completa de las mejoras implementadas

Este script demuestra todas las funcionalidades V2.1:
1. Palanca 1: Riesgo dinámico por equity
2. Palanca 2: Compounding obligatorio
3. Palanca 3: Parciales inteligentes (50/30/20)
4. Palanca 4: Leverage funcional 2x
5. Trade Metrics: MAE/MFE, R-multiples, session analysis
6. Capital Weighting: Setup quality + time-based
7. Runner Optimization: Trailing estructural tras +1.2R

Ejecutar: python demo_v21.py
"""

from datetime import datetime, UTC
import sys


def print_section(title: str):
    """Print a section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def demo_dynamic_risk():
    """Demo: Palanca 1 - Riesgo dinámico por equity."""
    print_section("PALANCA 1: RIESGO DINÁMICO POR EQUITY")
    
    from engine.core_utils import get_dynamic_risk_pct
    
    print("""
    Regla profesional:
    - < 2.000€: 1.0% por trade (más agresivo para cuentas pequeñas)
    - 2k-5k€:   0.75% por trade
    - > 5k€:    0.5% por trade (conservador para cuentas grandes)
    
    Esto NO cambia el edge, solo el multiplicador monetario.
    """)
    
    equities = [500, 1000, 1500, 2000, 3000, 5000, 10000, 50000]
    
    print("    Equity         | Risk %")
    print("    " + "-"*30)
    for equity in equities:
        risk = get_dynamic_risk_pct(equity)
        bar = "█" * int(risk * 100)
        print(f"    €{equity:>7,.0f}       | {risk*100:.2f}% {bar}")


def demo_compounding():
    """Demo: Palanca 2 - Compounding obligatorio."""
    print_section("PALANCA 2: COMPOUNDING OBLIGATORIO")
    
    from engine.core_utils import calculate_compounding_size, get_dynamic_risk_pct
    
    print("""
    Fórmula: position_size = (equity × risk%) / stop_distance
    
    Esto crea crecimiento exponencial:
    - Cuando vas bien → el € aumenta automáticamente
    - Cuando vas mal → el € disminuye (protección natural)
    """)
    
    # Simulate equity growth
    equity = 1000.0
    entry_price = 100000
    stop_distance = 500  # 500 USD SL
    
    print("    Simulación de 10 trades ganadores (+2R cada uno):")
    print("    " + "-"*50)
    
    for i in range(10):
        risk_pct = get_dynamic_risk_pct(equity)
        risk_amount = equity * risk_pct
        position_size = calculate_compounding_size(equity, entry_price, entry_price - stop_distance)
        
        # Win +2R
        profit = risk_amount * 2
        new_equity = equity + profit
        
        print(f"    Trade {i+1}: €{equity:>8,.2f} × {risk_pct*100:.2f}% = "
              f"arriesga €{risk_amount:>6,.2f} → gana €{profit:>6,.2f} → €{new_equity:>8,.2f}")
        
        equity = new_equity
    
    print(f"\n    💰 De €1,000 a €{equity:,.2f} en 10 trades (+2R cada uno)")


def demo_smart_partials():
    """Demo: Palanca 3 - Parciales inteligentes."""
    print_section("PALANCA 3: PARCIALES INTELIGENTES (50/30/20)")
    
    from engine.core_utils import get_smart_partials
    
    print("""
    Redistribución de tamaño sin cambiar entrada:
    - 50% → TP1 (bloqueas riesgo, aseguras profit)
    - 30% → TP2 (captura swing estructural)
    - 20% → Runner (trailing estructural, deja correr ganadores)
    
    El SL y la entrada NO cambian.
    """)
    
    sp = get_smart_partials()
    total_size = 0.01  # 0.01 BTC
    
    partials = sp.calculate_partial_sizes(total_size)
    tp_prices = sp.calculate_tp_prices(
        entry_price=100000,
        stop_loss=99500,
        direction='LONG'
    )
    
    print(f"    Ejemplo: Posición de {total_size} BTC, Entry: $100,000, SL: $99,500")
    print("    " + "-"*50)
    print(f"    TP1 @ ${tp_prices['tp1']:,.0f} (+{tp_prices['tp1_r']}R): {partials['tp1']:.6f} BTC ({partials['percentages']['tp1_pct']:.0f}%)")
    print(f"    TP2 @ ${tp_prices['tp2']:,.0f} (+{tp_prices['tp2_r']}R): {partials['tp2']:.6f} BTC ({partials['percentages']['tp2_pct']:.0f}%)")
    print(f"    Runner (trailing):            {partials['runner']:.6f} BTC ({partials['percentages']['runner_pct']:.0f}%)")


def demo_leverage():
    """Demo: Palanca 4 - Leverage funcional."""
    print_section("PALANCA 4: LEVERAGE FUNCIONAL 2x")
    
    from engine.config import DynamicRiskConfig
    
    print("""
    Leverage NO es subir riesgo.
    Leverage es liberar capital inmovilizado.
    
    Ejemplo con €1,000 y riesgo €10/trade:
    - Sin leverage: necesitas €1,000 en margen
    - Con 2x:       necesitas €500 en margen
    
    Mismo SL = mismo riesgo real. Solo liberas capital.
    """)
    
    print(f"    Configuración actual:")
    print(f"    - Default leverage: {DynamicRiskConfig.DEFAULT_LEVERAGE}x")
    print(f"    - Max leverage: {DynamicRiskConfig.MAX_LEVERAGE}x")
    
    # Example calculation
    capital = 1000
    risk_per_trade = 10  # €10
    btc_price = 100000
    sl_distance = 500
    
    position_size_btc = risk_per_trade / sl_distance
    notional = position_size_btc * btc_price
    margin_1x = notional
    margin_2x = notional / 2
    
    print(f"\n    Ejemplo: €{capital} capital, €{risk_per_trade} riesgo, BTC @ ${btc_price:,}")
    print(f"    - Position size: {position_size_btc:.6f} BTC")
    print(f"    - Valor nocional: ${notional:,.2f}")
    print(f"    - Margen 1x: ${margin_1x:,.2f}")
    print(f"    - Margen 2x: ${margin_2x:,.2f} ← Liberas ${margin_1x - margin_2x:,.2f}")


def demo_trade_metrics():
    """Demo: Sistema de métricas por trade."""
    print_section("TRADE METRICS: MAE/MFE/R-MULTIPLES")
    
    from engine.trade_metrics import TradeMetricsCalculator, FrictionConfig
    
    print("""
    Sin estos datos, todo lo demás es a ciegas.
    Estos métricas revelan:
    - Dónde dejamos dinero en la mesa
    - Qué setups valen más
    - En qué horarios el edge empeora
    """)
    
    calc = TradeMetricsCalculator()
    
    # Example winning trade
    print("\n    Ejemplo: LONG BTC @ $100,000, SL $99,500, Exit $101,500")
    print("    Durante el trade: máximo $102,000, mínimo $99,700")
    
    r = calc.calculate_r_multiple(100000, 101500, 99500, 'LONG')
    excursions = calc.calculate_mae_mfe(100000, 102000, 99700, 99500, 'LONG')
    friction = calc.apply_friction(100000, 101500, 0.01)
    
    print(f"\n    Resultados:")
    print(f"    - R realizado: {r:.2f}R")
    print(f"    - MAE (peor momento): {excursions['mae_r']:.2f}R")
    print(f"    - MFE (mejor momento): {excursions['mfe_r']:.2f}R")
    print(f"    - MFE/MAE ratio: {excursions['mfe_mae_ratio']:.2f}")
    print(f"    - Fricción total: ${friction['total_friction']:.4f} ({friction['friction_pct']:.3f}%)")


def demo_capital_weighting():
    """Demo: Capital weighting por setup y tiempo."""
    print_section("CAPITAL WEIGHTING: SETUP + TIEMPO")
    
    from engine.capital_weighting import (
        get_capital_weight_calculator, SetupQuality, calculate_weighted_risk
    )
    
    print("""
    En vez de: "¿qué trades eliminar?"
    Pregunta correcta: "¿a qué trades asigno más riesgo?"
    
    Setup A+ (mejor expectancy): 125% del riesgo base
    Setup A (sólido): 100% del riesgo base
    Setup B (normal): 75% del riesgo base
    Setup C (débil): 50% del riesgo base
    """)
    
    calc = get_capital_weight_calculator()
    
    print("\n    Por horario (UTC, Martes):")
    scenarios = [
        (8, "London Early"),
        (14, "NY Open (overlap)"),
        (22, "Dead Hours"),
    ]
    
    for hour, desc in scenarios:
        test_time = datetime(2026, 1, 6, hour, 0, tzinfo=UTC)  # Tuesday
        mult, breakdown = calc.calculate_total_weight(
            setup_quality=SetupQuality.A,
            current_time=test_time
        )
        session = breakdown['session']
        print(f"    {hour:02d}:00 ({desc:15}): {session:20} → {mult:.2f}x")
    
    print("\n    Ejemplo combinado:")
    # Best case: A+ setup during NY Open
    best_time = datetime(2026, 1, 6, 14, 0, tzinfo=UTC)
    adjusted, breakdown = calculate_weighted_risk(0.01, current_time=best_time)
    print(f"    - Base risk: 1.00%")
    print(f"    - A+ setup @ NY Open: {adjusted*100:.2f}%")
    
    # Worst case: C setup during dead hours
    worst_time = datetime(2026, 1, 11, 23, 0, tzinfo=UTC)  # Sunday 23:00
    calc2 = get_capital_weight_calculator()
    mult, _ = calc2.calculate_total_weight(
        setup_quality=SetupQuality.C,
        current_time=worst_time
    )
    print(f"    - C setup @ Sunday Dead Hours: {0.01 * mult * 100:.2f}% (o bloquear)")


def demo_runner_optimization():
    """Demo: Runner optimization."""
    print_section("RUNNER OPTIMIZATION: TRAILING TRAS +1.2R")
    
    from engine.capital_weighting import get_runner_manager
    
    print("""
    Pregunta clave: ¿Hasta dónde llegan los ganadores ANTES de morir?
    
    Solución:
    - NO hacer trailing hasta +1.2R
    - Usar estructura (swings), no solo ATR
    - Lock-in automático en niveles clave
    """)
    
    rm = get_runner_manager()
    
    print("\n    Lock-in levels (profit garantizado):")
    for threshold, lock in rm.config.LOCK_IN_LEVELS:
        print(f"    - Al alcanzar +{threshold}R → bloquear +{lock}R")
    
    print(f"\n    Extension mode: tras +{rm.config.EXTENSION_MODE_THRESHOLD_R}R, trailing más amplio")
    
    # Simulate trade progression
    print("\n    Simulación: LONG @ $100,000, SL $99,500")
    
    prices = [100300, 100750, 101000, 101500, 102000, 102500]
    
    for price in prices:
        status = rm.get_runner_status(100000, price, 99500, 'LONG')
        new_stop, reason = rm.calculate_trailing_stop(
            100000, price, 99500, status['current_r'], 200, 'LONG'
        )
        
        print(f"    Price ${price:,}: +{status['current_r']:.2f}R | "
              f"Mode: {status['mode']:10} | Lock: {status['locked_profit_r']}R | "
              f"Trail: ${new_stop:,.0f}")


def demo_version_freeze():
    """Demo: Version manager - congelar parámetros."""
    print_section("VERSION MANAGER: CONGELAR V2.1")
    
    from engine.version_manager import get_version, get_version_manager
    
    print("""
    PASO 1 del roadmap: Congelar parámetros actuales.
    
    Tu sistema ahora es rentable, estable, con DD razonable.
    → Congélalo. Solo medimos. No tocamos.
    """)
    
    vm = get_version_manager()
    info = vm.get_current_version()
    
    print(f"    Versión actual: {info.version} - {info.name}")
    print(f"    Hash de parámetros: {info.parameters_hash}")
    print(f"\n    Features:")
    for f in info.features[:4]:
        print(f"    ✓ {f}")
    print(f"    ... y {len(info.features) - 4} más")


def main():
    """Run all demos."""
    print("\n" + "="*60)
    print("  🚀 DEMO V2.1: PALANCAS DE MULTIPLICACIÓN")
    print("="*60)
    print("""
    Este demo muestra todas las mejoras implementadas:
    
    PALANCAS (multiplican sin cambiar edge):
    1. Riesgo dinámico por equity
    2. Compounding obligatorio
    3. Parciales inteligentes (50/30/20)
    4. Leverage funcional 2x
    
    OBSERVABILIDAD:
    5. Trade metrics (MAE/MFE/R)
    6. Capital weighting (setup + tiempo)
    7. Runner optimization
    
    SAGRADO (NO se toca):
    - Condiciones de entrada
    - Filtros HTF/LTF
    - Timing estructural
    - Lógica de invalidación
    """)
    
    demos = [
        demo_dynamic_risk,
        demo_compounding,
        demo_smart_partials,
        demo_leverage,
        demo_trade_metrics,
        demo_capital_weighting,
        demo_runner_optimization,
        demo_version_freeze,
    ]
    
    for demo in demos:
        try:
            demo()
        except Exception as e:
            print(f"\n    ❌ Error en demo: {e}")
    
    print_section("FIN DEL DEMO")
    print("""
    Próximos pasos:
    
    1. Ejecutar backtest con fricción real
    2. Analizar trades por sesión/hora
    3. Clasificar setups por MFE/MAE
    4. Ajustar capital weighting con datos reales
    5. Forward test 4-8 semanas
    
    Comando: python backtest_runner.py --symbol BTC/USD:USD
    """)


if __name__ == "__main__":
    main()
