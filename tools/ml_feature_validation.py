#!/usr/bin/env python3
"""
ML Feature Validation Tool
--------------------------
Validates that all ML features are properly captured and consistent
across the system. Checks:
1. ENTRY_FEATURES completeness
2. Feature coverage in trade logs
3. Feature drift detection
4. Missing/null feature audit

Usage:
    python -m tools.ml_feature_validation
    python -m tools.ml_feature_validation --analyze-logs
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any, Optional
import statistics

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ml.feature_extractor import ENTRY_FEATURES, FEATURE_DEFAULTS


def validate_feature_definitions() -> Dict[str, Any]:
    """Validate that all required features are defined with defaults."""
    results = {
        'valid': True,
        'total_features': len(ENTRY_FEATURES),
        'missing_defaults': [],
        'extra_defaults': [],
        'features_list': ENTRY_FEATURES.copy()
    }
    
    # Check each feature has a default
    for feature in ENTRY_FEATURES:
        if feature not in FEATURE_DEFAULTS:
            results['missing_defaults'].append(feature)
            results['valid'] = False
    
    # Check for extra defaults (feature removed but default remains)
    for feature in FEATURE_DEFAULTS:
        if feature not in ENTRY_FEATURES:
            results['extra_defaults'].append(feature)
    
    return results


def analyze_trade_log_features(log_path: Path) -> Dict[str, Any]:
    """Analyze feature coverage in trade logs."""
    results = {
        'trades_analyzed': 0,
        'trades_with_features': 0,
        'feature_coverage': {},  # feature -> count of trades with this feature
        'feature_nulls': {},     # feature -> count of null/missing values
        'feature_stats': {},     # feature -> {min, max, mean, std}
        'issues': []
    }
    
    if not log_path.exists():
        results['issues'].append(f"Log file not found: {log_path}")
        return results
    
    feature_values = {f: [] for f in ENTRY_FEATURES}
    
    try:
        with open(log_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                    
                try:
                    trade = json.loads(line)
                    results['trades_analyzed'] += 1
                    
                    snapshot = trade.get('feature_snapshot', {})
                    if snapshot:
                        results['trades_with_features'] += 1
                        
                        for feature in ENTRY_FEATURES:
                            if feature in snapshot and snapshot[feature] is not None:
                                results['feature_coverage'][feature] = results['feature_coverage'].get(feature, 0) + 1
                                feature_values[feature].append(float(snapshot[feature]))
                            else:
                                results['feature_nulls'][feature] = results['feature_nulls'].get(feature, 0) + 1
                                
                except json.JSONDecodeError:
                    continue
        
        # Calculate statistics for each feature
        for feature, values in feature_values.items():
            if len(values) >= 2:
                results['feature_stats'][feature] = {
                    'min': round(min(values), 4),
                    'max': round(max(values), 4),
                    'mean': round(statistics.mean(values), 4),
                    'std': round(statistics.stdev(values), 4),
                    'count': len(values)
                }
            elif len(values) == 1:
                results['feature_stats'][feature] = {
                    'min': values[0],
                    'max': values[0],
                    'mean': values[0],
                    'std': 0.0,
                    'count': 1
                }
        
        # Flag issues
        if results['trades_analyzed'] > 0 and results['trades_with_features'] == 0:
            results['issues'].append("No trades have feature_snapshot data")
        
        coverage_rate = results['trades_with_features'] / max(1, results['trades_analyzed'])
        if coverage_rate < 0.9:
            results['issues'].append(f"Low feature coverage: {coverage_rate:.1%}")
        
        # Check for features always null
        for feature, null_count in results['feature_nulls'].items():
            if null_count == results['trades_with_features']:
                results['issues'].append(f"Feature always null: {feature}")
                
    except Exception as e:
        results['issues'].append(f"Error parsing log: {e}")
    
    return results


def check_feature_drift(feature_stats: Dict[str, Dict]) -> List[str]:
    """Check for potential feature drift issues."""
    warnings = []
    
    for feature, stats in feature_stats.items():
        if 'std' not in stats:
            continue
            
        # Check for zero variance (constant feature)
        if stats['std'] == 0 and stats['count'] > 5:
            warnings.append(f"⚠️ {feature}: Zero variance (constant value)")
        
        # Check for extreme ranges
        if stats['max'] - stats['min'] > 1000:
            warnings.append(f"⚠️ {feature}: Very wide range [{stats['min']:.1f}, {stats['max']:.1f}]")
        
        # Check for potential normalization issues
        if feature.endswith('_score') and (stats['max'] > 10 or stats['min'] < -10):
            warnings.append(f"⚠️ {feature}: Score outside expected range")
    
    return warnings


def print_report(def_results: Dict, log_results: Optional[Dict] = None):
    """Print validation report."""
    print("\n" + "="*60)
    print("ML FEATURE VALIDATION REPORT")
    print("="*60)
    print(f"Generated: {datetime.now().isoformat()}")
    
    # Feature Definition Validation
    print("\n📋 FEATURE DEFINITIONS")
    print("-"*40)
    print(f"Total features defined: {def_results['total_features']}")
    
    if def_results['valid']:
        print("✅ All features have defaults defined")
    else:
        print("❌ VALIDATION FAILED")
        for missing in def_results['missing_defaults']:
            print(f"   Missing default: {missing}")
    
    if def_results['extra_defaults']:
        print("⚠️ Extra defaults (unused):")
        for extra in def_results['extra_defaults']:
            print(f"   {extra}")
    
    print("\nFeature List:")
    for i, feature in enumerate(def_results['features_list'], 1):
        default = FEATURE_DEFAULTS.get(feature, 'N/A')
        print(f"  {i:2d}. {feature:<30} (default: {default})")
    
    # Trade Log Analysis
    if log_results:
        print("\n📊 TRADE LOG ANALYSIS")
        print("-"*40)
        print(f"Trades analyzed: {log_results['trades_analyzed']}")
        print(f"Trades with features: {log_results['trades_with_features']}")
        
        if log_results['issues']:
            print("\n❌ ISSUES FOUND:")
            for issue in log_results['issues']:
                print(f"   {issue}")
        
        if log_results['feature_stats']:
            print("\n📈 Feature Statistics:")
            for feature in ENTRY_FEATURES:
                stats = log_results['feature_stats'].get(feature)
                if stats:
                    print(f"  {feature:<30} μ={stats['mean']:8.3f} σ={stats['std']:7.3f} [{stats['min']:.2f}, {stats['max']:.2f}]")
                else:
                    print(f"  {feature:<30} NO DATA")
            
            # Check for drift warnings
            drift_warnings = check_feature_drift(log_results['feature_stats'])
            if drift_warnings:
                print("\n⚠️ DRIFT WARNINGS:")
                for warning in drift_warnings:
                    print(f"   {warning}")
    
    print("\n" + "="*60)


def main():
    parser = argparse.ArgumentParser(description='Validate ML features')
    parser.add_argument('--analyze-logs', action='store_true', 
                        help='Also analyze trade logs for feature coverage')
    parser.add_argument('--log-file', type=str, 
                        default='trade_logs/trades.jsonl',
                        help='Path to trade log file')
    args = parser.parse_args()
    
    # Validate feature definitions
    def_results = validate_feature_definitions()
    
    # Optionally analyze trade logs
    log_results = None
    if args.analyze_logs:
        log_path = Path(args.log_file)
        if not log_path.is_absolute():
            log_path = Path(__file__).parent.parent / log_path
        log_results = analyze_trade_log_features(log_path)
    
    # Print report
    print_report(def_results, log_results)
    
    # Return exit code based on validation
    if not def_results['valid']:
        return 1
    if log_results and log_results['issues']:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
