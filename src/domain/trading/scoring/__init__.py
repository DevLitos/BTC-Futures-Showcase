"""
Scoring Module - Entry Score System for Signal Engine

Extracted from signal_engine.py (Jan 20, 2026) to reduce god class complexity.

Components:
- EntryScorer: Main scoring class with 4-block system
- Score breakdown: structure, timing, momentum, context + synergy bonus
"""

from src.domain.trading.scoring.entry_scorer import EntryScorer, ScoreBreakdown, get_entry_scorer

__all__ = ['EntryScorer', 'ScoreBreakdown', 'get_entry_scorer']
