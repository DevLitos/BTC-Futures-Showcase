"""
Quick Integration Guide - Edge Definition & Operations Policy

This script demonstrates how to integrate the edge enforcement system
into your trading execution flow.

TLDR:
1. Import EdgeEnforcement
2. Before each trade: validate_pre_trade()
3. After each trade: record_completed_trade()
4. Respect position_size_multiplier
5. Check logs for state transitions
"""

import logging
from datetime import datetime
from engine.edge_enforcement import EdgeEnforcement


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TradeExecutionWithEdge:
    """
    Example: Integrating edge enforcement into trade execution.
    """
    
    def __init__(self):
        """Initialize execution with edge enforcement"""
        self.enforcement = EdgeEnforcement()
        self.trade_counter = 0
    
    async def pre_trade_validation(self,
                                  current_atr: float,
                                  atr_p30: float,
                                  range_size: float,
                                  range_avg: float,
                                  current_drawdown: float) -> bool:
        """
        Step 1: Validate edge context and state before opening a trade.
        
        Returns:
            True if trade is allowed, False otherwise
        """
        
        # ===== VALIDATE EDGE CONTEXT =====
        context = self.enforcement.validate_edge_context(
            atr_current=current_atr,
            atr_p30=atr_p30,
            session_range=range_size,
            session_range_avg=range_avg,
            is_macro_event_day=False,  # Check your macro calendar
            expansion_sessions=2,        # Count from your analysis
            volume_trend=0.95            # London/NY volume ratio
        )
        
        # ===== VALIDATE PRE-TRADE CONDITIONS =====
        can_trade, blockers = self.enforcement.validate_pre_trade(
            context=context,
            current_drawdown=current_drawdown
        )
        
        if not can_trade:
            logger.warning(f"⛔ Trade BLOCKED: {blockers}")
            return False
        
        logger.info(f"✅ Trade APPROVED - proceeding")
        return True
    
    async def execute_trade(self,
                           symbol: str,
                           side: str,
                           entry_price: float,
                           stop_loss: float,
                           take_profit: float,
                           base_size: float) -> Optional[dict]:
        """
        Step 2: Execute trade with edge-aware position sizing.
        
        Returns:
            Order dict or None if failed
        """
        
        # ===== APPLY POSITION SIZING =====
        multiplier = self.enforcement.get_position_size_multiplier()
        position_size = base_size * multiplier
        
        if multiplier < 1.0:
            logger.warning(f"⚠️  Position size reduced: {multiplier*100:.0f}%")
        
        if multiplier == 0.0:
            logger.error(f"🔴 NO TRADING - Strategy is dead")
            return None
        
        # ===== PLACE ORDER =====
        logger.info(f"Placing {side} order: {position_size} {symbol} @ {entry_price}")
        
        # Your order placement logic here
        # order = await self.exchange.place_order(...)
        # order['size'] = position_size
        # order['entry_time'] = datetime.utcnow()
        
        self.trade_counter += 1
        
        return {
            'id': self.trade_counter,
            'symbol': symbol,
            'size': position_size,
            'entry': entry_price,
            'sl': stop_loss,
            'tp': take_profit,
            'entry_time': datetime.utcnow()
        }
    
    async def on_trade_exit(self,
                           trade_id: int,
                           entry_price: float,
                           exit_price: float,
                           entry_time: datetime,
                           exit_time: datetime,
                           mfe: float,
                           mae: float) -> None:
        """
        Step 3: Record completed trade for edge monitoring.
        
        This updates degradation signals and may trigger state transitions.
        """
        
        # Determine if TP1/TP2 were reached
        is_winner = exit_price > entry_price
        reached_tp1 = is_winner  # Simplification - implement properly
        reached_tp2 = False      # Check if extended beyond TP1
        is_be_stop = abs(exit_price - entry_price) < 0.001  # Break-even
        
        # Record trade
        self.enforcement.record_completed_trade(
            trade_id=trade_id,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            mfe=mfe,
            mae=mae,
            reached_tp1=reached_tp1,
            reached_tp2=reached_tp2,
            is_be_stop=is_be_stop
        )
        
        # Check current health
        report = self.enforcement.get_health_report()
        logger.info(f"Health: {report['degradation_health']} | "
                   f"Signals: {report['degradation_signals']} | "
                   f"Win rate: {report['win_rate_pct']:.1f}%")
    
    async def periodic_monitoring(self) -> None:
        """
        Step 4: Periodic monitoring (hourly/daily).
        
        Logs system health and state transitions.
        """
        
        # Hourly check
        self.enforcement.log_hourly_status()
        
        # After every 30 trades (once)
        if self.enforcement.edge_validator.get_trade_count() % 30 == 0:
            logger.info("📊 30-TRADE CHECKPOINT REACHED")
            self.enforcement.log_weekly_report()
    
    def get_current_state(self) -> str:
        """Get human-readable state description"""
        state = self.enforcement.state_machine.current_state.value
        multiplier = self.enforcement.get_position_size_multiplier()
        
        if state == "operating":
            return f"🟢 ACTIVE ({multiplier*100:.0f}% size)"
        elif state == "reduced_size_50pct":
            return f"🟡 DEGRADED (50% size)"
        elif state == "post_alert_monitoring":
            remaining = self.enforcement.state_machine.is_observation_period_remaining()
            return f"🟠 OBSERVATION ({remaining.days}d remaining)"
        elif state == "archived":
            return f"🔴 DEAD (archived)"
        return "❓ UNKNOWN"


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

async def example_trading_session():
    """
    Example of a complete trading session with edge enforcement.
    """
    trader = TradeExecutionWithEdge()
    
    # Session start
    logger.info("Starting trading session with edge enforcement active")
    logger.info(f"Current state: {trader.get_current_state()}")
    logger.info("")
    
    # ===== TRADE 1 =====
    logger.info("[TRADE 1]")
    
    # Pre-trade validation
    can_trade = await trader.pre_trade_validation(
        current_atr=150.0,
        atr_p30=120.0,
        range_size=200.0,
        range_avg=250.0,
        current_drawdown=0.05
    )
    
    if can_trade:
        # Execute
        order = await trader.execute_trade(
            symbol="BTC/USD",
            side="long",
            entry_price=42000.0,
            stop_loss=41500.0,
            take_profit=42500.0,
            base_size=1.0
        )
        
        if order:
            # Simulate exit
            await trader.on_trade_exit(
                trade_id=order['id'],
                entry_price=42000.0,
                exit_price=42300.0,
                entry_time=order['entry_time'],
                exit_time=datetime.utcnow(),
                mfe=500.0,
                mae=100.0
            )
    
    logger.info("")
    
    # ===== MONITORING =====
    await trader.periodic_monitoring()
    
    logger.info("")
    logger.info(f"Final state: {trader.get_current_state()}")


# ============================================================================
# KEY TAKEAWAYS
# ============================================================================

"""
1. EDGE ENFORCEMENT IS MANDATORY
   ❌ Never skip validation
   ✅ Every trade must pass validate_pre_trade()

2. POSITION SIZING IS AUTOMATIC
   ❌ Don't hardcode position sizes
   ✅ Use get_position_size_multiplier()
   
3. STATE TRANSITIONS ARE AUTOMATIC
   ❌ You don't decide when to degrade/observation/kill
   ✅ System transitions based on metrics
   
4. TRADER ROLE IS EXECUTOR + OBSERVER
   ✅ Execute when system says yes
   ✅ Observe metrics (don't judge them)
   ❌ Don't override system decisions
   
5. SAMPLE SIZE MATTERS
   ❌ Don't evaluate before N=30
   ✅ Only trust conclusions at N=30, 50, 100+

6. EARLY SIGNALS MATTER
   ✅ MFE/MAE, BE stops, duration, extension
   These lead losses by 10-15 trades
   
7. DEAD IS PERMANENT
   If strategy dies → archive it
   ❌ Don't try to resurrect
   ✅ Move to next opportunity
"""
