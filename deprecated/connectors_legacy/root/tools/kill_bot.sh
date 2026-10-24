#!/bin/bash
# Kill any running main.py process
echo "Killing bot processes..."
pkill -9 -f "python.*main.py" 2>/dev/null
pkill -9 -f "uv run main.py" 2>/dev/null
sleep 2
# Verify
if pgrep -f "main.py" > /dev/null 2>&1; then
    echo "❌ Bot still running!"
    pgrep -af "main.py"
else
    echo "✅ Bot killed successfully"
fi
