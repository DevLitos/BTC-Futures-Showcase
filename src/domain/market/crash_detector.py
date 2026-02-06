"""
🔥 CRASH DETECTOR - Feb 06, 2026

Detecta condiciones de PÁNICO/CRASH vs AGOTAMIENTO.

PROBLEMA RESUELTO:
- LOCATION_EXTREME (percentil <= 10%) bloqueaba SHORTs
- Pero en CRASH MODE (pánico real), el mercado sigue cayendo
- No diferenciar PÁNICO de AGOTAMIENTO = perder movimientos

LÓGICA:
- PÁNICO: displacement + ADX alto + volumen alto → SÍ short
- AGOTAMIENTO: precio en suelo pero sin momentum → NO short (o soft mode)
- MELT-UP: mismo concepto pero para LONGs en techos

MÉTRICAS:
- displacement_30m: Caída/subida en últimos 30 min
- adx_5m: Momentum rápido (>= 28 = fuerte)
- adx_1h: Tendencia HTF (>= 45 = muy fuerte)
- vol_ratio: Volumen vs promedio (>= 1.3 = elevado)
"""

from dataclasses import dataclass
from typing import Optional
import numpy as np
import logging

logger = logging.getLogger(__name__)


@dataclass
class CrashDetectorConfig:
    """Configuration for panic/crash detection."""
    
    # Panic mode thresholds
    PANIC_ADX_5M_MIN: float = 28.0         # Fast momentum threshold
    PANIC_ADX_1H_MIN: float = 45.0         # Strong HTF trend
    PANIC_VOL_RATIO_MIN: float = 1.3       # Above average volume
    PANIC_DISPLACEMENT_MIN: float = 0.02   # 2% displacement (absolute)
    
    # Melt-up mode thresholds (same logic, for longs)
    MELTUP_ADX_5M_MIN: float = 28.0
    MELTUP_ADX_1H_MIN: float = 45.0
    MELTUP_VOL_RATIO_MIN: float = 1.3
    MELTUP_DISPLACEMENT_MIN: float = 0.02


@dataclass
class CrashDetectorResult:
    """Result of crash/panic detection."""
    
    is_panic_down: bool = False        # Crash/panic mode (short allowed)
    is_meltup: bool = False            # Melt-up mode (long allowed)
    confidence: float = 0.0            # 0-1 confidence score
    reason: str = ""                   # Human-readable reason
    
    # Components
    has_displacement: bool = False
    has_momentum: bool = False
    has_volume: bool = False
    
    # Raw values
    displacement_pct: float = 0.0
    adx_5m: float = 0.0
    adx_1h: float = 0.0
    vol_ratio: float = 0.0


class CrashDetector:
    """
    Detects panic/crash conditions vs exhaustion.
    
    Use cases:
    - Allow shorts at extreme lows during crashes (panic selling)
    - Allow longs at extreme highs during melt-ups (FOMO buying)
    - Distinguish exhaustion (reversal coming) from continuation
    """
    
    def __init__(self, config: Optional[CrashDetectorConfig] = None):
        self.config = config or CrashDetectorConfig()
        self._last_result: Optional[CrashDetectorResult] = None
    
    def detect(
        self,
        displacement_pct: float,
        adx_5m: float,
        adx_1h: float,
        vol_ratio: float,
        direction: str = 'SHORT'
    ) -> CrashDetectorResult:
        """
        Detect if market is in panic/crash mode.
        
        Args:
            displacement_pct: Recent displacement as percentage (can be negative)
            adx_5m: ADX on 5-minute timeframe
            adx_1h: ADX on 1-hour timeframe
            vol_ratio: Volume relative to average (1.0 = normal)
            direction: 'SHORT' or 'LONG' - which direction to check
        
        Returns:
            CrashDetectorResult with detection status
        """
        result = CrashDetectorResult(
            displacement_pct=displacement_pct,
            adx_5m=adx_5m,
            adx_1h=adx_1h,
            vol_ratio=vol_ratio
        )
        
        cfg = self.config
        
        if direction == 'SHORT':
            # Check for panic/crash (downward)
            result.has_displacement = displacement_pct <= -cfg.PANIC_DISPLACEMENT_MIN
            result.has_momentum = (adx_5m >= cfg.PANIC_ADX_5M_MIN or adx_1h >= cfg.PANIC_ADX_1H_MIN)
            result.has_volume = vol_ratio >= cfg.PANIC_VOL_RATIO_MIN
            
            # Calculate confidence based on how many conditions are met
            confidence_score = 0.0
            reasons = []
            
            if result.has_displacement:
                confidence_score += 0.4
                reasons.append(f"disp={displacement_pct:.1%}")
            
            if result.has_momentum:
                confidence_score += 0.35
                if adx_1h >= cfg.PANIC_ADX_1H_MIN:
                    reasons.append(f"ADX_1h={adx_1h:.0f}")
                else:
                    reasons.append(f"ADX_5m={adx_5m:.0f}")
            
            if result.has_volume:
                confidence_score += 0.25
                reasons.append(f"vol={vol_ratio:.1f}x")
            
            # PANIC = all 3 conditions, or 2 conditions + high momentum
            result.is_panic_down = (
                (result.has_displacement and result.has_momentum and result.has_volume) or
                (result.has_displacement and adx_1h >= 50) or
                (result.has_momentum and result.has_volume and adx_1h >= 55)
            )
            
            result.confidence = min(1.0, confidence_score)
            result.reason = f"PANIC_DOWN: {', '.join(reasons)}" if result.is_panic_down else f"NO_PANIC: {', '.join(reasons)}"
            
        else:  # LONG
            # Check for melt-up (upward)
            result.has_displacement = displacement_pct >= cfg.MELTUP_DISPLACEMENT_MIN
            result.has_momentum = (adx_5m >= cfg.MELTUP_ADX_5M_MIN or adx_1h >= cfg.MELTUP_ADX_1H_MIN)
            result.has_volume = vol_ratio >= cfg.MELTUP_VOL_RATIO_MIN
            
            confidence_score = 0.0
            reasons = []
            
            if result.has_displacement:
                confidence_score += 0.4
                reasons.append(f"disp={displacement_pct:.1%}")
            
            if result.has_momentum:
                confidence_score += 0.35
                if adx_1h >= cfg.MELTUP_ADX_1H_MIN:
                    reasons.append(f"ADX_1h={adx_1h:.0f}")
                else:
                    reasons.append(f"ADX_5m={adx_5m:.0f}")
            
            if result.has_volume:
                confidence_score += 0.25
                reasons.append(f"vol={vol_ratio:.1f}x")
            
            result.is_meltup = (
                (result.has_displacement and result.has_momentum and result.has_volume) or
                (result.has_displacement and adx_1h >= 50) or
                (result.has_momentum and result.has_volume and adx_1h >= 55)
            )
            
            result.confidence = min(1.0, confidence_score)
            result.reason = f"MELTUP: {', '.join(reasons)}" if result.is_meltup else f"NO_MELTUP: {', '.join(reasons)}"
        
        self._last_result = result
        
        # Log if detected
        if result.is_panic_down or result.is_meltup:
            mode = "🔻 PANIC_DOWN" if result.is_panic_down else "🔺 MELT_UP"
            logger.warning(
                f"{mode} DETECTED: conf={result.confidence:.0%} | "
                f"disp={displacement_pct:.1%} ADX_5m={adx_5m:.0f} ADX_1h={adx_1h:.0f} vol={vol_ratio:.1f}x"
            )
        
        return result
    
    @property
    def last_result(self) -> Optional[CrashDetectorResult]:
        """Get last detection result."""
        return self._last_result
