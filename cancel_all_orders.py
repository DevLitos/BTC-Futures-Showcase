"""
Emergency script to cancel ALL open orders on Kraken Futures
"""
import asyncio
import os
from pathlib import Path
import ccxt.async_support as ccxt


def load_env_file():
    """Load environment variables from ~/.trad2_env"""
    env_file = Path.home() / '.trad2_env'
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.startswith('export '):
                        line = line[7:].strip()
                    if '=' in line:
                        key, value = line.split('=', 1)
                        value = value.strip().strip('"').strip("'")
                        os.environ[key.strip()] = value


async def main():
    load_env_file()
    
    api_key = os.getenv('KRAKEN_API_KEY', '')
    api_secret = os.getenv('KRAKEN_SECRET', '')
    
    if not api_key or not api_secret:
        print("❌ Error: KRAKEN_API_KEY or KRAKEN_SECRET not found in ~/.trad2_env")
        return
    
    creds = {
        'apiKey': api_key,
        'secret': api_secret,
        'options': {'defaultType': 'swap'}
    }
    
    exchange = ccxt.krakenfutures(creds)
    await exchange.load_markets()
    
    symbol = 'BTC/USD:USD'
    
    print(f"Fetching open orders for {symbol}...")
    orders = await exchange.fetch_open_orders(symbol)
    
    print(f"\n{'='*60}")
    print(f"Found {len(orders)} open order(s):")
    print(f"{'='*60}\n")
    
    for order in orders:
        order_id = order['id']
        order_type = order.get('type', 'unknown')
        side = order.get('side', 'unknown')
        amount = order.get('amount', 0)
        price = order.get('price', 0)
        
        print(f"  Order ID: {order_id}")
        print(f"  Type: {order_type}")
        print(f"  Side: {side}")
        print(f"  Amount: {amount}")
        print(f"  Price: {price}")
        print(f"  {'-'*50}")
    
    if orders:
        print(f"\n⚠️  Cancelling ALL {len(orders)} order(s)...\n")
        
        for order in orders:
            try:
                await exchange.cancel_order(order['id'], symbol)
                print(f"✅ Cancelled: {order['id']} ({order.get('type')} {order.get('side')})")
            except Exception as e:
                print(f"❌ Failed to cancel {order['id']}: {e}")
        
        print(f"\n{'='*60}")
        print(f"✅ Done! All orders cancelled.")
        print(f"{'='*60}\n")
    else:
        print("\n✅ No open orders found.\n")
    
    await exchange.close()

if __name__ == '__main__':
    asyncio.run(main())
