import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Binance API
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    TESTNET: bool = os.getenv("TESTNET", "true").lower() == "true"

    # Par y temporalidad
    SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT")

    # ── INTERVALO PRINCIPAL: 4H ──────────────────────────────────────────
    # El EMA crossover en 1H generaba 55+ señales anuales con solo 30% win rate
    # (puro ruido de mercado). En 4H, genera ~6-10 señales anuales con 50% win rate.
    INTERVAL: str = "4h"
    LOOKBACK_CANDLES: int = 100   # EMA 55 + ATR 14 + margen extra

    # Filtro HTF (Higher Time Frame): diario confirma la tendencia macro
    HTF_INTERVAL: str = "1d"
    HTF_LOOKBACK_CANDLES: int = 120     # EMA55 diario necesita 55+ velas; 120 da margen amplio
    ALLOW_BUY_IN_BEARISH_HTF: bool = True  # permitir compras incluso si el HTF diario es bajista

    # Parámetros EMA — mismos periodos, ahora sobre velas 4H
    EMA_FAST: int = 9
    EMA_SLOW: int = 21

    # Gestión de riesgo
    RISK_PER_TRADE: float = 0.005        # 0.5% del balance por operación
    ATR_PERIOD: int = 14
    ATR_SL_MULTIPLIER: float = 2.5       # SL = entrada - 2.5×ATR
    ATR_TP_MULTIPLIER: float = 4.0       # TP fijo (solo si TRAILING_STOP=False)
    MIN_QUANTITY: float = 0.00001        # mínimo real de Binance BTCUSDT spot (stepSize)
    TRAILING_STOP_MIN_MOVE: float = 0.005  # mover trailing stop solo si sube ≥0.5% del precio

    # Trailing stop: mueve el SL hacia arriba con cada cierre de vela.
    # En live, cancela el stop-limit anterior y coloca uno nuevo cuando sube ≥0.5%.
    TRAILING_STOP: bool = True

    # Filtros de entrada
    RSI_BUY_MIN: float = 40.0           # evitar entradas en zonas muy sobrevendidas (lateral)
    RSI_BUY_MAX: float = 80.0           # en tendencias fuertes RSI 70-80 = momentum válido
    ADX_PERIOD: int = 14
    ADX_MIN: float = 0               # solo operar cuando hay momentum (ADX > 15)

    # Normalización de volatilidad — mejora Sharpe ratio
    ATR_VOL_PERIOD: int = 50       # EMA para calcular ATR promedio histórico
    ATR_VOL_MIN_RATIO: float = 0.5 # no entrar si ATR < 50% del promedio (mercado muy quieto)
    # vol_multiplier = min(1.0, avg_atr / current_atr): reduce posición en alta volatilidad

    # Toma parcial de beneficios (scale-out)
    # Al llegar a SCALE_OUT_R × riesgo inicial, cerrar SCALE_OUT_RATIO de la posición
    # y mover el stop al break-even. Con SCALE_OUT_R=3.0 y SL_MULT=2.5 → trigger en 7.5×ATR
    # DESACTIVADO (=0.0): EMA crossover con 35% win-rate necesita que los ganadores corran;
    # cualquier R probado (3-15) reduce Sharpe porque recorta la media más que la varianza.
    SCALE_OUT_R:     float = 0.0   # 0 = desactivado | >0 = activo (múltiplos de R)
    SCALE_OUT_RATIO: float = 0.5   # fracción de la posición a cerrar (0.5 = 50 %)

    # Filtro de régimen: Efficiency Ratio (detecta mercados laterales/choppy)
    # ER = |movimiento neto en N velas| / |suma de movimientos individuales|
    # ER → 1.0 = tendencia limpia; ER → 0.0 = lateral/ruidoso
    # REGIME_ER_MIN = 0 desactiva el filtro
    REGIME_ER_PERIOD: int   = 10    # ventana de cálculo (10 × 4H ≈ 1.7 días)
    REGIME_ER_MIN:    float = 0.2   # umbral mínimo para considerar mercado trending

    # Notificaciones Telegram
    # Obtén el token con @BotFather y el chat_id con @userinfobot
    TELEGRAM_TOKEN:   str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID",   "")

    # Circuit breaker mensual
    MAX_MONTHLY_DRAWDOWN: float = 0.08


config = Config()
