import numpy as np
from numba import jit

@jit(nopython=True)
def calculate_ema(prices: np.ndarray, period: int) -> np.ndarray:
    if len(prices) < period:
        return np.full(len(prices), np.nan)

    ema = np.full(len(prices), np.nan)

    ema[period - 1] = np.mean(prices[:period])

    alpha = 2.0 / (period + 1.0)

    for i in range(period, len(prices)):
        ema[i] = prices[i] * alpha + ema[i - 1] * (1.0 - alpha)

    return ema

import numpy as np
from numba import jit

@jit(nopython=True)
def calculate_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """
    Calcula el Índice de Fuerza Relativa (RSI) de una serie de precios.

    Args:
        prices (np.ndarray): Array de precios de cierre.
        period (int): El período del RSI (por defecto 14).

    Returns:
        np.ndarray: Array con los valores del RSI. Los valores antes de `period-1` son NaN.
    """
    n = len(prices)
    if n <= period:
        return np.full(n, np.nan)

    rsi = np.full(n, np.nan)
    deltas = np.diff(prices)

    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    # Promedios iniciales
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Primer valor del RSI
    if avg_loss == 0:
        rs = np.inf
    else:
        rs = avg_gain / avg_loss
    rsi[period - 1] = 100.0 - (100.0 / (1.0 + rs))

    # RSI iterativo
    for i in range(period, n):
        gain = gains[i - 1]  # i-1 porque deltas tiene longitud n-1
        loss = losses[i - 1]

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            rs = np.inf
        else:
            rs = avg_gain / avg_loss

        rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


# Puedes añadir más indicadores aquí (VWAP, Bandas de Bollinger, ATR, etc.)
# siguiendo el mismo patrón de optimización con Numba.