#!/usr/bin/env python3
"""Quick compile check for all modified files."""
import py_compile
import sys

files = [
    'src/application/live/trading_app.py',
    'src/application/live/entry_orchestrator.py',
    'src/domain/trading/scoring/entry_scorer.py',
    'src/config/simplified_sizing.py',
    'src/config/trading_config.py',
]

errors = []
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  ✅ {f}")
    except py_compile.PyCompileError as e:
        print(f"  ❌ {f}: {e}")
        errors.append(f)

if errors:
    print(f"\n❌ {len(errors)} files have syntax errors!")
    sys.exit(1)
else:
    print(f"\n✅ All {len(files)} files compile OK")
    sys.exit(0)
