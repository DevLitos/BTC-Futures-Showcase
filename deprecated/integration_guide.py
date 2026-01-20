# =============================================================================
# NEW ARCHITECTURE - INTEGRATION GUIDE
# =============================================================================
#
# This document explains how to integrate the new modular architecture
# with the existing signal_engine.py codebase.
#
# IMPORTANT: This is a GRADUAL migration. The new modules are designed
# to work alongside existing code, allowing incremental adoption.
#
# =============================================================================

"""
ARCHITECTURE OVERVIEW
====================

OLD ARCHITECTURE:
-----------------
main.py
  └── signal_engine.py (4800+ lines, monolithic)
        ├── Strategy logic
        ├── Position tracking
        ├── Order execution
        ├── Risk management
        ├── State machine
        └── Everything else mixed together

NEW ARCHITECTURE:
-----------------
main.py
  ├── core/
  │     ├── runtime_mode.py    # LIVE/PAPER/BACKTEST guardrails
  │     ├── types.py           # Explicit dataclasses
  │     ├── events.py          # Event bus coordination
  │     └── cost_model.py      # Transaction cost modeling
  │
  ├── accounting/
  │     ├── position_store.py      # Single source of truth
  │     └── execution_analytics.py # Execution quality tracking
  │
  ├── strategy/
  │     └── trade_intent.py    # Intent generation (what we want)
  │
  ├── validation/
  │     └── walk_forward.py    # Research harness
  │
  └── engine/
        └── signal_engine.py   # (Existing, to be refactored gradually)


INTEGRATION STEPS
=================

1. IMMEDIATE (Safety Critical):
-------------------------------
   - Import RuntimeMode in main.py
   - Initialize runtime config FIRST before anything else
   - Validate mode before starting

2. SHORT-TERM (Position Safety):
--------------------------------
   - Use PositionStore as source of truth
   - Wrap existing position tracking to use new store
   - Add reconciliation on every cycle

3. MEDIUM-TERM (Execution Quality):
-----------------------------------
   - Use ExecutionAnalytics to track all fills
   - Feed data to CostModel for calibration
   - Monitor slippage and latency

4. LONGER-TERM (Strategy Separation):
-------------------------------------
   - Use TradeIntentGenerator for signal → intent
   - Separate intent validation from execution
   - Enable clean backtesting


USAGE EXAMPLES
==============
"""

# =============================================================================
# EXAMPLE 1: Runtime Mode Initialization (DO THIS FIRST)
# =============================================================================

async def example_runtime_init():
    """
    CRITICAL: Initialize runtime FIRST before anything else.
    
    Add this to the start of main.py, before any trading logic.
    """
    from core import RuntimeMode, init_runtime, get_runtime_config
    import os
    
    # Get mode from environment
    mode_str = os.getenv('TRADING_MODE', 'BACKTEST').upper()
    mode = RuntimeMode[mode_str]
    
    # Initialize with strict validation
    config = init_runtime(mode)
    
    # In LIVE mode, these are ENFORCED:
    # - config.instant_fill == False
    # - config.equity_source == "exchange"
    # - config.reduce_only_required == True
    
    print(f"Runtime initialized: {config.mode.value}")
    print(f"  Equity source: {config.equity_source}")
    print(f"  Instant fill: {config.instant_fill}")
    print(f"  Reduce only required: {config.reduce_only_required}")
    
    return config


# =============================================================================
# EXAMPLE 2: Position Store Integration
# =============================================================================

async def example_position_integration(exchange):
    """
    Use PositionStore as the single source of truth.
    
    Replace direct position tracking with this.
    """
    from accounting import get_position_store
    
    store = get_position_store()
    
    # ALWAYS sync from exchange before making decisions
    sync_result = await store.sync_from_exchange(exchange)
    
    if sync_result.get('discrepancies'):
        print(f"⚠️ Position discrepancies found!")
        for d in sync_result['discrepancies']:
            print(f"   {d}")
    
    # Get current position (this is now THE TRUTH)
    pos = store.get_position('BTC/USD:USD')
    
    if pos and not pos.is_flat:
        print(f"Position: {pos.side} {pos.size} @ {pos.entry_price}")
        print(f"  PnL: ${pos.realized_pnl:.2f}")
        print(f"  Fees: ${pos.total_fees:.2f}")
    else:
        print("No position")
    
    return pos


# =============================================================================
# EXAMPLE 3: Recording Fills
# =============================================================================

async def example_record_fill(store, fill_data: dict):
    """
    Record fills from exchange to update position state.
    
    This is the ONLY way to update position state (besides sync).
    """
    await store.record_fill(
        order_id=fill_data['order_id'],
        symbol=fill_data['symbol'],
        side=fill_data['side'],  # 'buy' or 'sell'
        size=fill_data['size'],
        price=fill_data['price'],
        fee=fill_data['fee'],
        fill_type=fill_data.get('fill_type', 'UNKNOWN'),
        fill_id=fill_data.get('fill_id', '')
    )


# =============================================================================
# EXAMPLE 4: Cost Model Usage
# =============================================================================

def example_cost_model():
    """
    Estimate costs before trading and record actual costs after.
    """
    from core import get_cost_model
    
    model = get_cost_model(exchange='krakenfutures')
    
    # Before trading: estimate costs
    costs = model.estimate_costs(
        side='LONG',
        size_usd=1000.0,
        entry_price=95000.0,
        is_maker_entry=True,
        is_maker_exit=False,  # SL usually hits as taker
        volatility_percentile=60.0,
        expected_hold_hours=2.0
    )
    
    print(f"Cost estimate:")
    print(f"  Entry fee: ${costs.fee_entry_usd:.4f}")
    print(f"  Exit fee: ${costs.fee_exit_usd:.4f}")
    print(f"  Slippage (entry): {costs.slippage_entry_bps:.2f}bps")
    print(f"  Slippage (exit): {costs.slippage_exit_bps:.2f}bps")
    print(f"  TOTAL: ${costs.total_usd:.4f} ({costs.total_bps:.2f}bps)")
    
    # Calculate breakeven
    breakeven = model.calculate_breakeven_distance(
        size_usd=1000.0,
        entry_price=95000.0,
        is_maker_entry=True,
        is_maker_exit=False
    )
    print(f"  Breakeven distance: ${breakeven:.2f}")
    
    return costs


# =============================================================================
# EXAMPLE 5: Trade Intent Generation
# =============================================================================

def example_trade_intent():
    """
    Generate trade intents (what the strategy wants) separately from execution.
    """
    from strategy import get_trade_intent_generator
    
    generator = get_trade_intent_generator(
        min_rr_ratio=1.5,
        max_cost_bps=30.0,
        min_confidence=0.6
    )
    
    # Generate an entry intent
    intent = generator.create_entry_intent(
        symbol='BTC/USD:USD',
        side='LONG',
        entry_price=95000.0,
        sl_price=94500.0,  # $500 below entry
        tp_price=96500.0,  # $1500 above entry (3:1 RR)
        risk_amount=100.0,  # $100 risk
        confidence=0.85,
        edge_score=2.5,
        reason='BOS confirmed with pullback to demand zone',
        regime='TRENDING',
        session='LONDON'
    )
    
    if intent.status.value == 'VALIDATED':
        print(f"✅ Intent validated: {intent.intent_id}")
        print(f"   Size: {intent.position_size:.6f} BTC")
        print(f"   Notional: ${intent.notional_usd:.2f}")
        print(f"   R:R: {intent.risk_reward_ratio:.2f}")
        print(f"   Est. cost: ${intent.estimated_cost_usd:.4f}")
        
        # Now pass to execution layer
        # orders = execution.execute_intent(intent)
        
    else:
        print(f"❌ Intent rejected: {intent.rejection_reason.value}")
        print(f"   {intent.rejection_details}")
    
    return intent


# =============================================================================
# EXAMPLE 6: Execution Analytics
# =============================================================================

async def example_execution_analytics():
    """
    Track execution quality for all trades.
    """
    from accounting import get_execution_analytics
    
    analytics = get_execution_analytics()
    
    # Start tracking before sending order
    tracker = analytics.start_execution(
        order_id='pending',
        symbol='BTC/USD:USD',
        side='buy',
        size=0.01,
        expected_price=95000.0,
        order_type='limit',
        purpose='entry'
    )
    
    # When order is sent
    analytics.record_order_sent(tracker.execution_id, exchange_order_id='EX123')
    
    # When exchange acknowledges
    analytics.record_order_ack(tracker.execution_id)
    
    # When fill arrives
    analytics.record_fill(
        tracker.execution_id,
        fill_price=95002.50,
        fill_size=0.01,
        fee=0.95,
        fill_type='MAKER'
    )
    
    # Get statistics
    stats = analytics.get_stats()
    print(f"Execution Stats:")
    print(f"  Fill rate: {stats.get('fill_rate_pct', 0):.1f}%")
    print(f"  Avg slippage: {stats['slippage']['mean_bps']:.2f}bps")
    print(f"  Maker rate: {stats['fill_types']['maker_pct']:.1f}%")
    
    return stats


# =============================================================================
# EXAMPLE 7: Event Bus for Coordination
# =============================================================================

async def example_event_bus():
    """
    Use event bus for decoupled component communication.
    """
    from core import get_event_bus, EventType, Event
    
    bus = get_event_bus()
    
    # Register handlers
    async def on_position_opened(event: Event):
        print(f"📈 Position opened: {event.data}")
    
    async def on_position_closed(event: Event):
        print(f"🏁 Position closed: {event.data}")
        # Could trigger: update dashboard, send notification, etc.
    
    bus.subscribe(EventType.POSITION_OPENED, on_position_opened)
    bus.subscribe(EventType.POSITION_CLOSED, on_position_closed)
    
    # Events are emitted automatically by PositionStore
    # No need to manually emit in most cases


# =============================================================================
# MIGRATION HELPER: Wrapper for Existing Signal Engine
# =============================================================================

class SignalEngineWrapper:
    """
    Wrapper to gradually integrate new modules with existing signal_engine.
    
    Use this as an intermediate step before full refactor.
    """
    
    def __init__(self, original_engine, exchange):
        self.engine = original_engine
        self.exchange = exchange
        
        # Initialize new modules
        from core import init_runtime, RuntimeMode, get_cost_model
        from accounting import get_position_store, get_execution_analytics
        from strategy import get_trade_intent_generator
        import os
        
        mode = RuntimeMode[os.getenv('TRADING_MODE', 'BACKTEST').upper()]
        self.config = init_runtime(mode)
        
        self.position_store = get_position_store()
        self.cost_model = get_cost_model()
        self.analytics = get_execution_analytics()
        self.intent_gen = get_trade_intent_generator()
    
    async def pre_cycle(self):
        """Call before each trading cycle."""
        # CRITICAL: Sync positions from exchange
        if self.config.mode == RuntimeMode.LIVE:
            await self.position_store.sync_from_exchange(self.exchange)
    
    async def post_fill(self, fill_data: dict):
        """Call after each fill."""
        # Record in position store
        await self.position_store.record_fill(**fill_data)
        
        # Record in analytics
        # (find the execution tracker and update it)
    
    def estimate_trade_costs(self, size_usd: float, entry_price: float) -> dict:
        """Get cost estimate before trading."""
        costs = self.cost_model.estimate_costs(
            side='LONG',  # Direction doesn't affect cost much
            size_usd=size_usd,
            entry_price=entry_price
        )
        return costs.to_dict()
    
    def get_position_truth(self, symbol: str):
        """Get authoritative position state."""
        return self.position_store.get_position(symbol)


# =============================================================================
# MAIN INTEGRATION POINT
# =============================================================================

async def main_with_new_architecture():
    """
    Example of how main.py should be structured with new architecture.
    """
    import os
    from core import init_runtime, RuntimeMode
    
    # 1. FIRST: Initialize runtime mode
    mode = RuntimeMode[os.getenv('TRADING_MODE', 'BACKTEST').upper()]
    config = init_runtime(mode)
    
    print(f"🚀 Starting in {config.mode.value} mode")
    
    if config.mode == RuntimeMode.LIVE:
        print("⚠️  LIVE MODE - Real money at risk!")
        print(f"   Equity source: {config.equity_source}")
        print(f"   Reduce-only required: {config.reduce_only_required}")
    
    # 2. Initialize exchange
    # exchange = create_exchange(...)
    
    # 3. Initialize trading components with new architecture
    from accounting import get_position_store
    from strategy import get_trade_intent_generator
    
    position_store = get_position_store()
    intent_generator = get_trade_intent_generator()
    
    # 4. Main loop (simplified)
    # while True:
    #     # Sync positions from exchange
    #     if config.mode == RuntimeMode.LIVE:
    #         await position_store.sync_from_exchange(exchange)
    #     
    #     # Generate trade intent
    #     intent = intent_generator.create_entry_intent(...)
    #     
    #     # Execute if valid
    #     if intent.status == IntentStatus.VALIDATED:
    #         # Execute...
    #         pass
    #     
    #     await asyncio.sleep(1)


if __name__ == "__main__":
    # Run examples
    import asyncio
    
    print("=" * 60)
    print("NEW ARCHITECTURE EXAMPLES")
    print("=" * 60)
    
    # Example: Cost model
    print("\n--- Cost Model ---")
    example_cost_model()
    
    # Example: Trade intent
    print("\n--- Trade Intent ---")
    # Note: This requires position_store to be initialized
    # example_trade_intent()
    
    print("\n" + "=" * 60)
    print("See integration_guide.py for full examples")
    print("=" * 60)
