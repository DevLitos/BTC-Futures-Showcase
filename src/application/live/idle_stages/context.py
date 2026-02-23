"""Shared mutable context for the idle pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class IdleCtx:
    """
    Carries local variables through the idle-state pipeline stages.

    All fields that were local variables in _handle_idle_state and are needed
    by more than one stage live here.  Fields are mutable — stages may update
    ``bias``, ``htf_passed``, etc.
    """

    # ── Core (set by _handle_idle_state before entering the pipeline) ──
    price: float
    atr_1m: float
    time: datetime
    bias: str | None                # Mutable — stages can flip it
    mode: str                       # 'RANGE' | 'TRANSITION' | 'TREND'
    regime_score: float
    effective_adx: float
    df_1h: pd.DataFrame
    candles_5m: list
    core_sources: tuple

    # HTF result — carried from early HTF filter to the final HTF check
    htf_passed: bool = True
    htf_reason: str = ''

    # ── Set by counter-trend / HTF stage ──
    allow_countertrend: bool = False

    # ── Set by edge pipeline stage ──
    atr_1h: float = 0.0
    closes_1h: Any = None           # np.ndarray | None
    highs_1h: Any = None            # np.ndarray | None
    lows_1h: Any = None             # np.ndarray | None
