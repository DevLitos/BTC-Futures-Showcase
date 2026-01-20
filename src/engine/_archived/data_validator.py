"""
Data Validator - Validates data sufficiency before making trading decisions.

This module ensures that edge/activity calculations have enough data
to be meaningful. When data is insufficient, it returns explicit
warnings rather than silently allowing trades with poor data.

POLICY: "Deny by default" when data is insufficient, not "allow by default".
"""

from dataclasses import dataclass
from typing import Tuple, List, Optional
import numpy as np
import logging

logger = logging.getLogger(__name__)


@dataclass
class DataQuality:
    """Assessment of data quality and sufficiency"""
    is_sufficient: bool = False
    confidence: float = 0.0  # 0-1 scale
    warnings: List[str] = None
    min_samples_required: int = 0
    actual_samples: int = 0
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class DataValidator:
    """
    Validates data sufficiency for edge calculations.
    
    Implements "deny by default" policy when data is poor.
    """
    
    # Minimum requirements for different calculations
    MIN_CANDLES_FOR_ACTIVITY = 60       # 1 hour at 1m
    MIN_CANDLES_FOR_ATR = 50            # ATR needs history
    MIN_CANDLES_FOR_VOLUME = 30         # Volume MA needs history
    MIN_TRADES_FOR_DEGRADATION = 20     # Degradation detection
    MIN_TRADES_FOR_EXPECTANCY = 30      # Statistical significance
    MIN_ATR_HISTORY = 100               # ATR percentile calculation
    
    def __init__(self, strict_mode: bool = True):
        """
        Initialize validator.
        
        Args:
            strict_mode: If True, require full data for high confidence.
                        If False, allow reduced requirements for backtest.
        """
        self.strict_mode = strict_mode
        self._multiplier = 1.0 if strict_mode else 0.5
    
    def set_mode(self, strict: bool) -> None:
        """Change validation strictness."""
        self.strict_mode = strict
        self._multiplier = 1.0 if strict else 0.5
    
    def validate_activity_data(self, 
                               highs: np.ndarray,
                               lows: np.ndarray,
                               candle_minutes: int = 1) -> DataQuality:
        """
        Validate data for activity range calculation.
        
        Args:
            highs: High prices array
            lows: Low prices array
            candle_minutes: Minutes per candle
            
        Returns:
            DataQuality assessment
        """
        min_required = int(self.MIN_CANDLES_FOR_ACTIVITY * self._multiplier)
        actual = min(len(highs), len(lows))
        
        warnings = []
        
        if actual < min_required:
            warnings.append(
                f"Insufficient candles for activity: {actual} < {min_required}"
            )
        
        # Check for data quality issues
        if np.any(np.isnan(highs)) or np.any(np.isnan(lows)):
            warnings.append("NaN values detected in price data")
        
        if np.any(highs < lows):
            warnings.append("Invalid OHLC: some highs are below lows")
        
        confidence = min(actual / min_required, 1.0) if min_required > 0 else 0.0
        
        return DataQuality(
            is_sufficient=len(warnings) == 0 and actual >= min_required,
            confidence=confidence,
            warnings=warnings,
            min_samples_required=min_required,
            actual_samples=actual
        )
    
    def validate_atr_data(self,
                          atr_values: np.ndarray,
                          current_atr: float) -> DataQuality:
        """
        Validate data for ATR percentile calculation.
        
        Args:
            atr_values: Historical ATR values
            current_atr: Current ATR value
            
        Returns:
            DataQuality assessment
        """
        min_required = int(self.MIN_ATR_HISTORY * self._multiplier)
        actual = len(atr_values[~np.isnan(atr_values)]) if len(atr_values) > 0 else 0
        
        warnings = []
        
        if actual < min_required:
            warnings.append(
                f"Insufficient ATR history: {actual} < {min_required}"
            )
        
        if np.isnan(current_atr) or current_atr <= 0:
            warnings.append(f"Invalid current ATR: {current_atr}")
        
        confidence = min(actual / min_required, 1.0) if min_required > 0 else 0.0
        
        return DataQuality(
            is_sufficient=len(warnings) == 0 and actual >= min_required,
            confidence=confidence,
            warnings=warnings,
            min_samples_required=min_required,
            actual_samples=actual
        )
    
    def validate_volume_data(self, volumes: np.ndarray) -> DataQuality:
        """
        Validate data for volume analysis.
        
        Args:
            volumes: Volume array
            
        Returns:
            DataQuality assessment
        """
        min_required = int(self.MIN_CANDLES_FOR_VOLUME * self._multiplier)
        
        # Filter valid volumes
        valid_volumes = volumes[~np.isnan(volumes)]
        valid_volumes = valid_volumes[valid_volumes > 0]
        actual = len(valid_volumes)
        
        warnings = []
        
        if actual < min_required:
            warnings.append(
                f"Insufficient volume data: {actual} valid samples < {min_required}"
            )
        
        # Check for all-zero volumes (common in some data sources)
        if len(volumes) > 0 and np.all(volumes == 0):
            warnings.append("All volume values are zero - volume analysis disabled")
        
        confidence = min(actual / min_required, 1.0) if min_required > 0 else 0.0
        
        return DataQuality(
            is_sufficient=len(warnings) == 0 and actual >= min_required,
            confidence=confidence,
            warnings=warnings,
            min_samples_required=min_required,
            actual_samples=actual
        )
    
    def validate_trade_history(self,
                               trade_count: int,
                               purpose: str = "degradation") -> DataQuality:
        """
        Validate trade history sufficiency.
        
        Args:
            trade_count: Number of trades recorded
            purpose: 'degradation' or 'expectancy'
            
        Returns:
            DataQuality assessment
        """
        if purpose == "expectancy":
            min_required = int(self.MIN_TRADES_FOR_EXPECTANCY * self._multiplier)
        else:
            min_required = int(self.MIN_TRADES_FOR_DEGRADATION * self._multiplier)
        
        warnings = []
        
        if trade_count < min_required:
            warnings.append(
                f"Insufficient trades for {purpose}: {trade_count} < {min_required}"
            )
        
        confidence = min(trade_count / min_required, 1.0) if min_required > 0 else 0.0
        
        return DataQuality(
            is_sufficient=len(warnings) == 0 and trade_count >= min_required,
            confidence=confidence,
            warnings=warnings,
            min_samples_required=min_required,
            actual_samples=trade_count
        )
    
    def validate_all(self,
                    highs: np.ndarray,
                    lows: np.ndarray,
                    closes: np.ndarray,
                    volumes: np.ndarray,
                    atr_values: np.ndarray,
                    current_atr: float,
                    trade_count: int = 0,
                    candle_minutes: int = 1) -> Tuple[bool, List[str], float]:
        """
        Validate all data requirements for edge calculation.
        
        Args:
            highs, lows, closes: Price arrays
            volumes: Volume array
            atr_values: ATR history
            current_atr: Current ATR
            trade_count: Number of recorded trades
            candle_minutes: Candle size in minutes
            
        Returns:
            Tuple (is_sufficient, all_warnings, overall_confidence)
        """
        all_warnings = []
        confidences = []
        
        # Activity data
        activity_q = self.validate_activity_data(highs, lows, candle_minutes)
        all_warnings.extend(activity_q.warnings)
        confidences.append(activity_q.confidence)
        
        # ATR data
        atr_q = self.validate_atr_data(atr_values, current_atr)
        all_warnings.extend(atr_q.warnings)
        confidences.append(atr_q.confidence)
        
        # Volume data (only warn, don't block)
        volume_q = self.validate_volume_data(volumes)
        if volume_q.warnings:
            # Volume warnings are informational, not blocking
            logger.debug(f"Volume data quality: {volume_q.warnings}")
        
        # Trade history (if provided)
        if trade_count > 0:
            trade_q = self.validate_trade_history(trade_count)
            if not trade_q.is_sufficient:
                all_warnings.extend(trade_q.warnings)
            confidences.append(trade_q.confidence)
        
        # Overall assessment
        is_sufficient = activity_q.is_sufficient and atr_q.is_sufficient
        overall_confidence = np.mean(confidences) if confidences else 0.0
        
        if all_warnings:
            logger.warning(f"Data quality warnings: {all_warnings}")
        
        return is_sufficient, all_warnings, float(overall_confidence)


# Global singleton (for backwards compatibility)
# Prefer explicit instantiation for new code
_validator: Optional[DataValidator] = None

def get_data_validator(strict_mode: bool = True, instance: Optional[DataValidator] = None) -> DataValidator:
    """
    Get or create DataValidator instance.
    
    Args:
        strict_mode: Validation strictness
        instance: Optional explicit instance to use (DI pattern)
        
    Returns:
        DataValidator instance
    """
    global _validator
    
    # DI: si se pasa instancia explícita, usarla
    if instance is not None:
        return instance
    
    # Singleton fallback
    if _validator is None or _validator.strict_mode != strict_mode:
        _validator = DataValidator(strict_mode=strict_mode)
    return _validator


def reset_validator():
    """Reset singleton (for testing)"""
    global _validator
    _validator = None
