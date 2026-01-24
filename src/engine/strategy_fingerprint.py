"""
Strategy Fingerprint - Identity lock to prevent metric contamination

CRITICAL PROBLEM:
If you change:
- SL logic
- Trailing behavior
- TP targets
- Entry filters

Is it the SAME strategy or a NEW strategy?

Your system doesn't know.

DANGER:
- Mix metrics from different strategy versions
- False expectancy calculations
- Attribute new strategy performance to old strategy
- Self-deception through contaminated samples

PHILOSOPHY:
- Strategy has an identity
- Identity = hash of core logic
- Identity change = statistics reset
- No mixing of incompatible samples
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, UTC
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class StrategyFingerprint:
    """Immutable strategy identity"""
    fingerprint_hash: str
    created_at: datetime
    strategy_version: str
    components: Dict[str, str]  # Component hashes
    description: str


@dataclass
class FingerprintChange:
    """Record of strategy fingerprint change"""
    change_timestamp: datetime
    old_fingerprint: str
    new_fingerprint: str
    changed_components: List[str]
    breaking_change: bool  # Requires stats reset
    reason: Optional[str]


class StrategyIdentityLock:
    """
    Locks strategy identity to prevent metric contamination.
    
    CONCEPT:
    - Strategy is defined by its core components
    - Each component has a hash (logic fingerprint)
    - Overall fingerprint = hash of component hashes
    - Fingerprint change detection triggers appropriate action
    
    COMPONENTS:
    1. Entry logic (signal generation)
    2. Exit logic (SL/TP/trailing)
    3. Position sizing
    4. Risk rules
    5. Execution mode
    """
    
    def __init__(self, strategy_version: str = "1.0"):
        """
        Initialize strategy identity lock.
        
        Args:
            strategy_version: Human-readable version
        """
        self.strategy_version = strategy_version
        self.current_fingerprint: Optional[StrategyFingerprint] = None
        self.fingerprint_history: List[StrategyFingerprint] = []
        self.change_history: List[FingerprintChange] = []
        
        # Component tracking
        self.components: Dict[str, str] = {}
        
    def register_component(self,
                          component_name: str,
                          component_definition: Dict) -> str:
        """
        Register or update a strategy component.
        
        Args:
            component_name: Name of component (e.g., 'entry_logic', 'exit_logic')
            component_definition: Dict defining the component logic/parameters
            
        Returns:
            Hash of the component
        """
        # Create deterministic hash of component
        component_hash = self._hash_component(component_definition)
        
        # Check if component changed
        old_hash = self.components.get(component_name)
        if old_hash and old_hash != component_hash:
            # Component changed
            self._handle_component_change(component_name, old_hash, component_hash)
        
        # Register component
        self.components[component_name] = component_hash
        
        # Update overall fingerprint
        self._update_fingerprint()
        
        return component_hash
    
    def _hash_component(self, definition: Dict) -> str:
        """Create deterministic hash of component definition"""
        # Sort keys for deterministic JSON
        json_str = json.dumps(definition, sort_keys=True)
        
        # Create SHA256 hash
        hash_obj = hashlib.sha256(json_str.encode())
        
        # Return first 16 chars for readability
        return hash_obj.hexdigest()[:16]
    
    def _update_fingerprint(self) -> None:
        """Update overall strategy fingerprint"""
        
        if not self.components:
            return
        
        # Create fingerprint from all components
        components_json = json.dumps(self.components, sort_keys=True)
        overall_hash = hashlib.sha256(components_json.encode()).hexdigest()[:16]
        
        # Check if fingerprint changed
        old_fingerprint = self.current_fingerprint
        
        new_fingerprint = StrategyFingerprint(
            fingerprint_hash=overall_hash,
            created_at=datetime.now(UTC),
            strategy_version=self.strategy_version,
            components=self.components.copy(),
            description=f"Strategy v{self.strategy_version}"
        )
        
        # Record change if fingerprint changed
        if old_fingerprint and old_fingerprint.fingerprint_hash != overall_hash:
            self._record_fingerprint_change(old_fingerprint, new_fingerprint)
        
        # Update current
        self.current_fingerprint = new_fingerprint
        
        # Archive
        self.fingerprint_history.append(new_fingerprint)
        if len(self.fingerprint_history) > 50:
            self.fingerprint_history = self.fingerprint_history[-50:]
    
    def _handle_component_change(self,
                                 component_name: str,
                                 old_hash: str,
                                 new_hash: str) -> None:
        """Handle component change detection"""
        # Component changed - logging for audit trail
        # Actual recording happens in _record_fingerprint_change()
        logger.info(f"Component '{component_name}' changed: {old_hash[:8]}... -> {new_hash[:8]}...")
    
    def _record_fingerprint_change(self,
                                   old_fp: StrategyFingerprint,
                                   new_fp: StrategyFingerprint) -> None:
        """Record fingerprint change"""
        
        # Determine which components changed
        changed_components = []
        for comp_name, comp_hash in new_fp.components.items():
            old_hash = old_fp.components.get(comp_name)
            if old_hash != comp_hash:
                changed_components.append(comp_name)
        
        # Determine if this is a breaking change
        breaking = self._is_breaking_change(changed_components)
        
        change = FingerprintChange(
            change_timestamp=datetime.now(UTC),
            old_fingerprint=old_fp.fingerprint_hash,
            new_fingerprint=new_fp.fingerprint_hash,
            changed_components=changed_components,
            breaking_change=breaking,
            reason=f"Components changed: {', '.join(changed_components)}"
        )
        
        self.change_history.append(change)
        if len(self.change_history) > 100:
            self.change_history = self.change_history[-100:]
    
    def _is_breaking_change(self, changed_components: List[str]) -> bool:
        """
        Determine if component changes require statistics reset.
        
        BREAKING CHANGES (require reset):
        - Entry logic changed
        - Exit logic changed
        - Risk rules changed
        
        NON-BREAKING CHANGES (don't require reset):
        - Logging changes
        - Display/reporting changes
        - Non-core optimizations
        """
        breaking_components = [
            'entry_logic',
            'exit_logic',
            'stop_loss',
            'take_profit',
            'trailing_logic',
            'position_sizing',
            'risk_rules'
        ]
        
        for comp in changed_components:
            if comp in breaking_components:
                return True
        
        return False
    
    def get_current_fingerprint(self) -> Optional[str]:
        """Get current strategy fingerprint hash"""
        if self.current_fingerprint:
            return self.current_fingerprint.fingerprint_hash
        return None
    
    def requires_statistics_reset(self) -> Tuple[bool, str]:
        """
        Check if recent changes require statistics reset.
        
        Returns:
            Tuple of (requires_reset, reason)
        """
        if not self.change_history:
            return False, "No changes detected"
        
        # Check most recent change
        last_change = self.change_history[-1]
        
        if last_change.breaking_change:
            return True, f"Breaking change: {last_change.reason}"
        
        return False, "No breaking changes"
    
    def validate_sample_compatibility(self,
                                     sample_fingerprint: str) -> Tuple[bool, str]:
        """
        Validate if historical sample is compatible with current strategy.
        
        Args:
            sample_fingerprint: Fingerprint when sample was recorded
            
        Returns:
            Tuple of (is_compatible, reason)
        """
        current_fp = self.get_current_fingerprint()
        
        if not current_fp:
            return False, "No current fingerprint"
        
        if sample_fingerprint == current_fp:
            return True, "Exact match"
        
        # Check if they differ by non-breaking changes only
        sample_fp_obj = self._find_fingerprint_in_history(sample_fingerprint)
        
        if not sample_fp_obj:
            return False, "Sample from unknown strategy version"
        
        # Find all changes between sample and current
        breaking_changes = self._count_breaking_changes_between(
            sample_fingerprint,
            current_fp
        )
        
        if breaking_changes > 0:
            return False, f"Incompatible: {breaking_changes} breaking changes since sample"
        
        return True, "Compatible despite non-breaking changes"
    
    def _find_fingerprint_in_history(self, fingerprint_hash: str) -> Optional[StrategyFingerprint]:
        """Find fingerprint in history"""
        for fp in self.fingerprint_history:
            if fp.fingerprint_hash == fingerprint_hash:
                return fp
        return None
    
    def _count_breaking_changes_between(self,
                                        from_fingerprint: str,
                                        to_fingerprint: str) -> int:
        """Count breaking changes between two fingerprints"""
        
        # Find indices in history
        from_idx = None
        to_idx = None
        
        for i, fp in enumerate(self.fingerprint_history):
            if fp.fingerprint_hash == from_fingerprint:
                from_idx = i
            if fp.fingerprint_hash == to_fingerprint:
                to_idx = i
        
        if from_idx is None or to_idx is None:
            return -1  # Unknown
        
        # Count breaking changes in range
        breaking_count = 0
        for change in self.change_history:
            # Find if this change occurred between fingerprints
            change_fp_idx = None
            for i, fp in enumerate(self.fingerprint_history):
                if fp.fingerprint_hash == change.new_fingerprint:
                    change_fp_idx = i
                    break
            
            if change_fp_idx and from_idx < change_fp_idx <= to_idx:
                if change.breaking_change:
                    breaking_count += 1
        
        return breaking_count
    
    def create_component_definitions(self) -> Dict:
        """
        Create component definitions for current strategy.
        
        This should be called with actual strategy parameters.
        
        Returns:
            Dict with component definitions
        """
        # Placeholder - should be populated with actual strategy logic
        return {
            'entry_logic': {
                'type': 'expansion_momentum',
                'expansion_threshold': 1.0,
                'momentum_filter': True
            },
            'exit_logic': {
                'sl_type': 'trailing_intelligent',
                'tp_targets': [1.5, 2.5],
                'be_activation': 1.2
            },
            'position_sizing': {
                'base_risk_percent': 1.0,
                'atr_multiplier': 1.0
            },
            'risk_rules': {
                'max_daily_loss_pct': 3.0,
                'max_position_size': 1.0
            }
        }
    
    def register_full_strategy(self, config: Dict) -> str | None:
        """
        Register complete strategy from configuration.
        
        Args:
            config: Full strategy configuration
            
        Returns:
            Overall fingerprint hash
        """
        # Register each major component
        if 'entry' in config:
            self.register_component('entry_logic', config['entry'])
        
        if 'exit' in config:
            self.register_component('exit_logic', config['exit'])
        
        if 'stop_loss' in config:
            self.register_component('stop_loss', config['stop_loss'])
        
        if 'take_profit' in config:
            self.register_component('take_profit', config['take_profit'])
        
        if 'position_sizing' in config:
            self.register_component('position_sizing', config['position_sizing'])
        
        if 'risk_rules' in config:
            self.register_component('risk_rules', config['risk_rules'])
        
        return self.get_current_fingerprint()
    
    def get_identity_summary(self) -> Dict:
        """Get summary of strategy identity"""
        
        if not self.current_fingerprint:
            return {'status': 'no_fingerprint'}
        
        # Recent changes
        recent_changes = self.change_history[-5:] if self.change_history else []
        
        # Breaking changes count
        breaking_changes = len([c for c in self.change_history if c.breaking_change])
        
        return {
            'current_fingerprint': self.current_fingerprint.fingerprint_hash,
            'strategy_version': self.strategy_version,
            'created_at': self.current_fingerprint.created_at.isoformat(),
            'components_count': len(self.components),
            'total_changes': len(self.change_history),
            'breaking_changes': breaking_changes,
            'requires_reset': self.requires_statistics_reset()[0],
            'recent_changes': [
                {
                    'timestamp': c.change_timestamp.isoformat(),
                    'components': c.changed_components,
                    'breaking': c.breaking_change
                }
                for c in recent_changes
            ]
        }
    
    def export_fingerprint(self) -> Dict:
        """Export fingerprint for storage/comparison"""
        if not self.current_fingerprint:
            return {}
        
        return {
            'fingerprint': self.current_fingerprint.fingerprint_hash,
            'version': self.strategy_version,
            'created': self.current_fingerprint.created_at.isoformat(),
            'components': self.current_fingerprint.components
        }
    
    def import_fingerprint(self, fingerprint_data: Dict) -> bool:
        """
        Import and validate against stored fingerprint.
        
        Returns:
            True if matches current, False otherwise
        """
        stored_hash = fingerprint_data.get('fingerprint')
        current_hash = self.get_current_fingerprint()
        
        return stored_hash == current_hash
