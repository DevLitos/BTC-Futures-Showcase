import sys
import os
import ingest.kraken_websocket
from pathlib import Path

print(f"Python executable: {sys.executable}")
print(f"ingest source: {ingest.kraken_websocket.__file__}")

cwd = os.getcwd()
print(f"CWD: {cwd}")

is_local = str(Path(ingest.kraken_websocket.__file__).resolve()).startswith(str(Path(cwd).resolve()))

if is_local:
    print("✅ SUCCESS: Running from local source")
else:
    print("❌ FAILURE: Running from installed package (likely stale)")
