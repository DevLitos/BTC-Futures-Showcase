"""
Script para verificar símbolos disponibles en Kraken Futures
"""
import ccxt

def check_symbols():
    exchange = ccxt.krakenfutures({
        'enableRateLimit': True,
    })
    
    try:
        print(f"  Loading markets from Kraken Futures...")
        exchange.load_markets()
        
        print(f"  Total markets: {len(exchange.symbols)}")
        
        # Buscar símbolos BTC
        btc_symbols = [s for s in exchange.symbols if 'BTC' in s or 'XBT' in s]
        
        print(f"\n📊 Símbolos BTC disponibles en KRAKEN FUTURES:")
        print("=" * 80)
        for symbol in btc_symbols[:20]:
            try:
                market = exchange.market(symbol)
                symbol_type = market.get('type', 'N/A')
                active = market.get('active', False)
                settle = market.get('settle', 'N/A')
                print(f"  {symbol:35} | Type: {symbol_type:10} | Settle: {str(settle):5} | Active: {active}")
            except Exception as e:
                print(f"  {symbol:35} | Error: {e}")
        
        print("\n" + "=" * 80)
        
    except Exception as e:
        print(f"Error loading markets: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    check_symbols()
