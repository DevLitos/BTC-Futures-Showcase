"""
Version Manager - V2.1 Parameter Freeze

PASO 1 del roadmap: Congelar parámetros actuales.

Tu sistema ahora es:
- Rentable
- Estable  
- Con drawdown razonable

→ Congélalo. Llama a esto Versión 2.1.0.
Solo medimos. No tocamos.

This module provides:
1. Version tracking and metadata
2. Parameter snapshot/freeze functionality
3. Version comparison tools
"""

import json
import hashlib
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# VERSION INFO
# =============================================================================

VERSION = "2.1.0"
VERSION_NAME = "Palancas de Multiplicación"
VERSION_DATE = "2026-01-05"

VERSION_FEATURES = [
    "Palanca 1: Riesgo dinámico por equity (< 2k=1%, 2k-5k=0.75%, >5k=0.5%)",
    "Palanca 2: Compounding obligatorio (position_size = risk% * equity / stop)",
    "Palanca 3: Parciales inteligentes (50% TP1 + 30% TP2 + 20% runner)",
    "Palanca 4: Leverage funcional 2x (libera capital, no aumenta riesgo)",
    "Trade Metrics: MAE/MFE, R-multiples, session analysis",
    "Friction Simulation: slippage, fees, flash stop penalty",
    "Structured Trade Logging: JSONL + summary stats",
]

VERSION_NOTES = """
SAGRADO (NO TOCAR):
- Condiciones de entrada
- Filtros HTF/LTF
- Timing estructural
- Lógica de invalidación

AJUSTADO:
- Riesgo por trade → dinámico según equity
- Compounding → obligatorio
- Parciales → redistribuidos (50/30/20)
- Leverage → 2x funcional

AÑADIDO:
- Sistema de métricas por trade
- Simulación de fricción realista
- Logging estructurado para análisis
"""


@dataclass
class VersionInfo:
    """Version metadata."""
    version: str
    name: str
    date: str
    features: list
    notes: str
    parameters_hash: str = ""
    frozen_at: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# PARAMETER SNAPSHOT
# =============================================================================

def get_frozen_parameters() -> Dict[str, Any]:
    """
    Get all configurable parameters as a frozen snapshot.
    
    This captures the exact state of the system for reproducibility.
    """
    from src.engine.config import (
        DynamicRiskConfig, RiskManagement, HTFFilters, MTFFilters, 
        LTFFilters, KillConditions, TimeBasedExits
    )
    
    return {
        "version": VERSION,
        "frozen_at": datetime.now(UTC).isoformat(),
        
        # Dynamic Risk (Palanca 1)
        "dynamic_risk": {
            "risk_tiers": DynamicRiskConfig.RISK_TIERS,
            "min_risk_pct": DynamicRiskConfig.MIN_RISK_PCT,
            "max_risk_pct": DynamicRiskConfig.MAX_RISK_PCT,
        },
        
        # Leverage (Palanca 4)
        "leverage": {
            "default": DynamicRiskConfig.DEFAULT_LEVERAGE,
            "max": DynamicRiskConfig.MAX_LEVERAGE,
        },
        
        # Compounding (Palanca 2)
        "compounding": {
            "enabled": DynamicRiskConfig.COMPOUNDING_ENABLED,
        },
        
        # Parciales (Palanca 3) - ahora consolidado en DynamicRiskConfig
        "partials": {
            "enabled": getattr(DynamicRiskConfig, 'SMART_PARTIALS_ENABLED', True),
            "tp1_pct": getattr(DynamicRiskConfig, 'PARTIAL_TP1_PCT', 50.0),
            "tp2_pct": getattr(DynamicRiskConfig, 'PARTIAL_TP2_PCT', 30.0),
            "runner_pct": getattr(DynamicRiskConfig, 'PARTIAL_RUNNER_PCT', 20.0),
            "tp1_r_target": getattr(DynamicRiskConfig, 'TP1_R_TARGET', 1.0),
            "tp2_r_target": getattr(DynamicRiskConfig, 'TP2_R_TARGET', 2.0),
        },
        
        # Risk Management (core - NO TOCAR)
        "risk_management": {
            "tp1_risk_multiple": RiskManagement.TP1_RISK_MULTIPLE,
            "tp1_close_pct": RiskManagement.TP1_CLOSE_PCT,
            "trailing_stop_atr_mult": RiskManagement.TRAILING_STOP_ATR_MULTIPLIER,
            "max_trade_duration_min": RiskManagement.MAX_TRADE_DURATION_MINUTES,
        },
        
        # HTF Filters (SAGRADO)
        "htf_filters": {
            "min_adx": HTFFilters.MIN_ADX,
            "min_adx_slope": HTFFilters.MIN_ADX_SLOPE,
            "min_atr_vs_ma": HTFFilters.MIN_ATR_VS_MA,
        },
        
        # MTF Filters (SAGRADO)
        "mtf_filters": {
            "min_fibo_retracement": MTFFilters.MIN_FIBO_RETRACEMENT,
            "max_fibo_retracement": MTFFilters.MAX_FIBO_RETRACEMENT,
            "bos_lookback_periods": MTFFilters.BOS_LOOKBACK_PERIODS,
        },
        
        # LTF Filters (SAGRADO)
        "ltf_filters": {
            "min_relative_volume": LTFFilters.MIN_RELATIVE_VOLUME,
            "min_orderbook_imbalance": LTFFilters.MIN_ORDERBOOK_IMBALANCE,
        },
        
        # Kill Conditions
        "kill_conditions": {
            "max_daily_drawdown_pct": KillConditions.MAX_DAILY_DRAWDOWN_PCT,
            "max_consecutive_losses": KillConditions.MAX_CONSECUTIVE_LOSSES,
        },
        
        # Time Based Exits
        "time_exits": {
            "enabled": TimeBasedExits.ENABLED,
            "max_hold_minutes": TimeBasedExits.MAX_HOLD_MINUTES,
            "stagnation_enabled": TimeBasedExits.STAGNATION_EXIT_ENABLED,
            "min_progress_r": TimeBasedExits.MIN_PROGRESS_R,
        },
    }


def compute_parameters_hash(params: Dict[str, Any]) -> str:
    """Compute SHA256 hash of parameters for version comparison."""
    # Remove timestamp for hash computation
    params_copy = {k: v for k, v in params.items() if k != 'frozen_at'}
    json_str = json.dumps(params_copy, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode()).hexdigest()[:16]


# =============================================================================
# VERSION MANAGER
# =============================================================================

class VersionManager:
    """
    Manages version snapshots and comparisons.
    
    Purpose: Ensure we can always reproduce exact trading conditions
    and detect when parameters have drifted.
    """
    
    def __init__(self, snapshots_dir: str = "output/versions"):
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
    
    def get_current_version(self) -> VersionInfo:
        """Get current version info."""
        params = get_frozen_parameters()
        return VersionInfo(
            version=VERSION,
            name=VERSION_NAME,
            date=VERSION_DATE,
            features=VERSION_FEATURES,
            notes=VERSION_NOTES,
            parameters_hash=compute_parameters_hash(params),
            frozen_at=datetime.now(UTC).isoformat()
        )
    
    def freeze_version(self, tag: str | None = None) -> str:
        """
        Freeze current version to a snapshot file.
        
        Args:
            tag: Optional tag for the snapshot (e.g., "pre_experiment")
        
        Returns:
            Path to snapshot file
        """
        params = get_frozen_parameters()
        version_info = self.get_current_version()
        
        snapshot = {
            "version_info": version_info.to_dict(),
            "parameters": params
        }
        
        # Create filename
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        tag_suffix = f"_{tag}" if tag else ""
        filename = f"v{VERSION}{tag_suffix}_{timestamp}.json"
        filepath = self.snapshots_dir / filename
        
        # Save
        with open(filepath, 'w') as f:
            json.dump(snapshot, f, indent=2, default=str)
        
        logger.info(f"🔒 Version {VERSION} frozen to {filepath}")
        logger.info(f"   Parameters hash: {version_info.parameters_hash}")
        
        return str(filepath)
    
    def load_snapshot(self, filepath: str) -> Dict[str, Any]:
        """Load a version snapshot."""
        with open(filepath, 'r') as f:
            return json.load(f)
    
    def compare_with_snapshot(self, snapshot_path: str) -> Dict[str, Any]:
        """
        Compare current parameters with a snapshot.
        
        Returns dict with:
        - same: bool
        - differences: list of changed parameters
        """
        snapshot = self.load_snapshot(snapshot_path)
        snapshot_params = snapshot['parameters']
        current_params = get_frozen_parameters()
        
        snapshot_hash = compute_parameters_hash(snapshot_params)
        current_hash = compute_parameters_hash(current_params)
        
        differences = []
        
        def compare_dict(d1, d2, path=""):
            for key in set(list(d1.keys()) + list(d2.keys())):
                full_path = f"{path}.{key}" if path else key
                if key not in d1:
                    differences.append(f"ADDED: {full_path} = {d2[key]}")
                elif key not in d2:
                    differences.append(f"REMOVED: {full_path}")
                elif isinstance(d1[key], dict) and isinstance(d2[key], dict):
                    compare_dict(d1[key], d2[key], full_path)
                elif d1[key] != d2[key]:
                    differences.append(f"CHANGED: {full_path}: {d1[key]} → {d2[key]}")
        
        compare_dict(snapshot_params, current_params)
        
        return {
            "same": snapshot_hash == current_hash,
            "snapshot_hash": snapshot_hash,
            "current_hash": current_hash,
            "snapshot_version": snapshot['version_info']['version'],
            "current_version": VERSION,
            "differences": differences
        }
    
    def list_snapshots(self) -> list:
        """List all saved snapshots."""
        return sorted([str(f) for f in self.snapshots_dir.glob("*.json")])
    
    def print_version_info(self) -> None:
        """Print current version info to console."""
        info = self.get_current_version()
        params = get_frozen_parameters()
        
        print("\n" + "="*60)
        print(f"🏷️  VERSION {info.version} - {info.name}")
        print("="*60)
        print(f"Date: {info.date}")
        print(f"Hash: {info.parameters_hash}")
        print(f"\nFeatures:")
        for f in info.features:
            print(f"  ✓ {f}")
        print(f"\n{info.notes}")
        print("="*60)
        
        print("\n📋 KEY PARAMETERS:")
        print(f"  Risk Tiers: {params['dynamic_risk']['risk_tiers']}")
        print(f"  Leverage: {params['leverage']['default']}x")
        print(f"  Compounding: {'ON' if params['compounding']['enabled'] else 'OFF'}")
        print(f"  Partials: {params['partials']['tp1_pct']}/{params['partials']['tp2_pct']}/{params['partials']['runner_pct']}")
        print("="*60 + "\n")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_version_manager: Optional[VersionManager] = None


def get_version_manager() -> VersionManager:
    """Get global VersionManager instance."""
    global _version_manager
    if _version_manager is None:
        _version_manager = VersionManager()
    return _version_manager


def get_version() -> str:
    """Get current version string."""
    return VERSION


def freeze_current_version(tag: str | None = None) -> str:
    """Freeze current version to file."""
    return get_version_manager().freeze_version(tag)


def print_version() -> None:
    """Print version info."""
    get_version_manager().print_version_info()


def check_version_drift(snapshot_path: str) -> bool:
    """
    Check if current parameters have drifted from snapshot.
    
    Returns True if parameters match, False if drifted.
    """
    result = get_version_manager().compare_with_snapshot(snapshot_path)
    
    if result['same']:
        logger.info(f"✅ Parameters match snapshot (hash: {result['current_hash']})")
        return True
    else:
        logger.warning(f"⚠️ Parameters have drifted from snapshot!")
        for diff in result['differences']:
            logger.warning(f"   {diff}")
        return False
