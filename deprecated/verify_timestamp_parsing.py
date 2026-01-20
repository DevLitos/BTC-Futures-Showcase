
import time

def get_candle_key(timestamp_ms: int, timeframe: str) -> int:
    """Logic from kraken_websocket.py"""
    ts_seconds = timestamp_ms // 1000
    if timeframe == '1m':
        return (ts_seconds // 60) * 60
    return ts_seconds

def test_timestamps():
    print("Testing Timestamp Logic...")
    
    # CASE 1: Timestamp in Milliseconds (Correct)
    ts_ms_1 = 1700000000000  # t=0
    ts_ms_2 = 1700000065000  # t=65s
    
    key_1 = get_candle_key(ts_ms_1, '1m')
    key_2 = get_candle_key(ts_ms_2, '1m')
    
    print(f"MS Input: {ts_ms_1} -> Key: {key_1}")
    print(f"MS Input: {ts_ms_2} -> Key: {key_2}")
    
    if key_1 != key_2:
        print("✅ MS timestamps produce DIFFERENT keys (Candle closed)")
    else:
        print("❌ MS timestamps produce SAME key (Candle NOT closed)")

    print("-" * 30)

    # CASE 2: Timestamp in Seconds (Suspected Issue)
    # If Kraken sends 1700000000.5, and we treat it as MS...
    # It becomes 1700000000 when cast to int
    # 1700000000 // 1000 = 1700000
    # 1 Minute later: 1700000060
    # 1700000060 // 1000 = 1700000
    
    ts_sec_1 = 1700000000.5
    ts_sec_2 = 1700000065.5
    
    # Emulate code: ts = trade_item[0] (which we suspect is seconds)
    # Code passes this directly to get_candle_key expecting MS
    
    key_sec_1 = get_candle_key(int(ts_sec_1), '1m')
    key_sec_2 = get_candle_key(int(ts_sec_2), '1m')
    
    print(f"Sec Input (as MS): {int(ts_sec_1)} -> Key: {key_sec_1}")
    print(f"Sec Input (as MS): {int(ts_sec_2)} -> Key: {key_sec_2}")
    
    if key_sec_1 != key_sec_2:
        print("✅ Sec timestamps produce DIFFERENT keys (Candle closed)")
    else:
        print("❌ Sec timestamps produce SAME key (Candle NOT closed)")

if __name__ == "__main__":
    test_timestamps()
