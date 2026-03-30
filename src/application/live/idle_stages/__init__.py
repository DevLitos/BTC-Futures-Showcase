"""
Extracted stages from SignalEngine._handle_idle_state.

Each stage is a function: stage_xxx(engine, ctx) -> bool
Returns True if the idle pipeline should abort (i.e. the caller should `return`).
Returns False to continue to the next stage.
"""

from .context import IdleCtx
from .mode_handlers import stage_range_mode, stage_transition_mode, stage_trend_mode
from .edge_pipeline import stage_counter_trend_htf, stage_edge_detection
from .evaluation_pipeline import (
    stage_location_filter,
    stage_filters_and_ltf,
    stage_finalize_entry,
)

__all__ = [
    'IdleCtx',
    'stage_range_mode',
    'stage_transition_mode',
    'stage_trend_mode',
    'stage_counter_trend_htf',
    'stage_edge_detection',
    'stage_location_filter',
    'stage_filters_and_ltf',
    'stage_finalize_entry',
]
