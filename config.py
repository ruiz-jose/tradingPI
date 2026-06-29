import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Binance Spot API (usada por los scripts de backtest single-symbol)
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    TESTNET: bool = os.getenv("TESTNET", "true").lower() == "true"

    # Binance Futures API — requiere keys propias de testnet.binancefuture.com,
    # distintas de las de Spot. Si no se configuran, cae en las de Spot (no funcionará
    # contra Futures Testnet real, pero permite que el resto del bot no rompa).
    FUTURES_API_KEY:    str  = os.getenv("BINANCE_FUTURES_API_KEY", "") or API_KEY
    FUTURES_API_SECRET: str  = os.getenv("BINANCE_FUTURES_API_SECRET", "") or API_SECRET
    FUTURES_TESTNET:    bool = os.getenv("FUTURES_TESTNET", "true").lower() == "true"

    # Apalancamiento y tipo de margen para Futuros (USD-M).
    # El riesgo real por operación lo sigue definiendo RISK_PER_TRADE + distancia de SL;
    # el leverage solo determina cuánto margen se bloquea, no cuánto se arriesga.
    LEVERAGE:    int = int(os.getenv("LEVERAGE", "2"))
    MARGIN_TYPE: str = os.getenv("MARGIN_TYPE", "ISOLATED")

    # Estimación de liquidación (aproximada, ignora funding/PnL no realizado y tiers de
    # margen de mantenimiento reales de Binance — solo sirve como red de seguridad para
    # rechazar una entrada si el SL queda peligrosamente cerca del precio de liquidación).
    MAINTENANCE_MARGIN_RATE:    float = 0.004   # ~0.4%, típico en tiers bajos de notional
    LIQUIDATION_SAFETY_BUFFER:  float = 1.5     # la distancia a liquidación debe ser >= 1.5x la distancia al SL

    # Par y temporalidad
    SYMBOL: str = os.getenv("SYMBOL", "BTCUSDT")          # usado por backtest.py (single-symbol)
    SYMBOLS: list[str] = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")  # bot.py en vivo
    MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))

    # Símbolos donde se permite abrir SHORT. Validado con backtest_futures.py (36-60 meses):
    # en BTCUSDT los shorts tienen win rate ~19% y PF ~0.5 (pérdida neta consistente) incluso
    # endureciendo ADX/RSI/ER — la señal death-cross+HTF-bajista no tiene ventaja real ahí.
    # En ETHUSDT (PF 1.07) y SOLUSDT (PF 1.12) son neutros a levemente positivos.
    # Revalidar con walkforward.py/backtest_futures.py antes de añadir BTCUSDT a esta lista.
    SHORT_ENABLED_SYMBOLS: list[str] = os.getenv("SHORT_ENABLED_SYMBOLS", "ETHUSDT,SOLUSDT").split(",")

    # ── INTERVALO PRINCIPAL: 4H ──────────────────────────────────────────
    # El EMA crossover en 1H generaba 55+ señales anuales con solo 30% win rate
    # (puro ruido de mercado). En 4H, genera ~6-10 señales anuales con 50% win rate.
    # 8H probado y RECHAZADO 2026-06-21: ver sección de walk-forward en estrategia.md.
    INTERVAL: str = "4h"
    LOOKBACK_CANDLES: int = 100   # EMA 55 + ATR 14 + margen extra

    # Filtro HTF (Higher Time Frame): diario confirma la tendencia macro
    HTF_INTERVAL: str = "1d"
    HTF_LOOKBACK_CANDLES: int = 120     # EMA55 diario necesita 55+ velas; 120 da margen amplio
    # FALSE por defecto: con el bug de backtest-vs-live corregido (antes backtest.py exigía
    # HTF alcista SIEMPRE, sin importar este flag, mientras bot.py sí lo respetaba — los
    # resultados reportados no correspondían al comportamiento real en vivo), validé ambos
    # valores con datos reales: True da Sharpe 0.18-0.50 y PF 1.08-1.25 (MEJORABLE);
    # False da Sharpe 0.61-1.19 y PF 1.57-2.31 (ACEPTABLE/BUENA) en 36m y 60m.
    # Comprar contra el HTF diario bajista con apalancamiento castiga el rendimiento.
    ALLOW_BUY_IN_BEARISH_HTF: bool = False

    # Parámetros EMA — mismos periodos, ahora sobre velas 4H
    EMA_FAST: int = 9
    EMA_SLOW: int = 21

    # Gestión de riesgo
    RISK_PER_TRADE: float = 0.005        # 0.5% del balance por operación
    # Cap de riesgo simultáneo entre todos los símbolos abiertos (BTC/ETH/SOL están altamente
    # correlacionados: 3 posiciones a la vez no son 3 apuestas independientes de 0.5% cada una,
    # es casi una sola apuesta de hasta 1.5% si el mercado se mueve junto). En vez de estimar
    # correlación en vivo (ruidoso con tan pocos trades), se asume el caso conservador
    # correlación≈1 y se limita el riesgo nominal abierto total. Validar con backtest_multi.py
    # (--portfolio-risk-cap) antes de ajustar este valor.
    PORTFOLIO_RISK_CAP: float = float(os.getenv("PORTFOLIO_RISK_CAP", "0.01"))  # 1% del balance
    ATR_PERIOD: int = 14
    ATR_SL_MULTIPLIER: float = 2.5       # SL = entrada ∓ 2.5×ATR (fallback si no hay régimen ADX)
    ATR_TP_MULTIPLIER: float = 4.0       # TP fijo — actúa como techo de seguridad aunque TRAILING_STOP=True
    MIN_QUANTITY: float = 0.00001        # mínimo real de Binance BTCUSDT spot (stepSize)
    TRAILING_STOP_MIN_MOVE: float = 0.005  # mover trailing stop solo si sube ≥0.5% del precio

    # Trailing stop adaptativo por fuerza de tendencia (ADX):
    # tendencia fuerte → stop más ancho (deja correr al ganador); mercado débil/choppy → stop más ajustado.
    ATR_SL_MULTIPLIER_TREND: float = 3.0   # usado cuando ADX >= ADX_TREND_THRESHOLD
    ATR_SL_MULTIPLIER_CHOP:  float = 2.0   # usado cuando ADX < ADX_TREND_THRESHOLD
    ADX_TREND_THRESHOLD:     float = 25.0

    # Trailing stop: mueve el SL hacia arriba con cada cierre de vela.
    # En live, cancela el stop-limit anterior y coloca uno nuevo cuando sube ≥0.5%.
    TRAILING_STOP: bool = True

    # Filtros de entrada — largos
    RSI_BUY_MIN: float = 40.0           # evitar entradas en zonas muy sobrevendidas (lateral)
    RSI_BUY_MAX: float = 80.0           # en tendencias fuertes RSI 70-80 = momentum válido

    # Filtros de entrada — cortos (espejo de los de largo)
    RSI_SELL_MIN: float = 20.0          # evitar shorts en sobreventa extrema (riesgo de rebote)
    RSI_SELL_MAX: float = 60.0          # en tendencias bajistas fuertes RSI 40-60 = momentum válido

    ADX_PERIOD: int = 14
    # walkforward.py (48 meses, train12/test3) probó ADX_MIN en [0,15,20,25] y la combinación
    # ganadora out-of-sample fue casi siempre 0 (filtro desactivado) — el filtro ER
    # (REGIME_ER_MIN) ya cubre la detección de mercado lateral sin necesitar el de ADX.
    # Re-ejecutar walkforward.py periódicamente con datos nuevos para revalidar este valor.
    ADX_MIN: float = 0.0

    # Normalización de volatilidad — mejora Sharpe ratio
    ATR_VOL_PERIOD: int = 50       # EMA para calcular ATR promedio histórico
    ATR_VOL_MIN_RATIO: float = 0.5 # no entrar si ATR < 50% del promedio (mercado muy quieto)
    ATR_VOL_MAX_RATIO: float = 3.0 # kill-switch: no entrar si ATR > 300% del promedio (evento extremo)
    # vol_multiplier = min(1.0, avg_atr / current_atr): reduce posición en alta volatilidad

    # Toma parcial de beneficios (scale-out)
    # Al llegar a SCALE_OUT_R × riesgo inicial, cerrar SCALE_OUT_RATIO de la posición
    # y mover el stop al break-even. Con SCALE_OUT_R=3.0 y SL_MULT=2.5 → trigger en 7.5×ATR
    # Calibrar con walkforward.py (out-of-sample) antes de fijar el valor final: el comentario
    # previo decía que cualquier R reducía Sharpe, pero esa prueba fue sin el filtro ADX/ER
    # activado — puede que cambien las conclusiones.
    SCALE_OUT_R:     float = 0.0   # 0 = desactivado | >0 = activo (múltiplos de R)
    SCALE_OUT_RATIO: float = 0.5   # fracción de la posición a cerrar (0.5 = 50 %)

    # Break-even: al llegar a BREAKEVEN_R × riesgo inicial (R = |entrada - SL inicial|),
    # mueve el SL a entrada + BREAKEVEN_BUFFER (cubre fees/slippage de la salida).
    # Objetivo: limitar la cola de "ganador que se convierte en perdedor" sin capar
    # el upside como hace el scale-out (que ya se descartó por reducir Sharpe).
    # Calibrar con backtest.py + walkforward.py antes de fijar el valor final.
    BREAKEVEN_R:      float = 0.0     # 0 = desactivado | >0 = activo (múltiplos de R)
    BREAKEVEN_BUFFER: float = 0.0015  # 0.15% sobre entrada — cubre comisión + slippage de salida

    # Estimación de costo de funding en Futuros (se cobra/paga cada 8h sobre el notional).
    # Usado solo como fallback en backtest_futures.py cuando no hay dato histórico real
    # disponible para un periodo (datos faltantes en el endpoint de Binance).
    FUNDING_RATE_ASSUMPTION: float = 0.0001   # 0.01% por periodo de 8h (valor típico histórico)

    # Filtro de funding rate: evita abrir una posición cuando el funding actual ya es
    # desfavorable para ese lado (en Binance, funding positivo = los LONG pagan a los SHORT;
    # funding negativo = los SHORT pagan a los LONG). Mantener una posición varios días contra
    # un funding persistente puede comerse buena parte del edge de la estrategia.
    # 0 = filtro desactivado.
    FUNDING_FILTER_ENABLED:    bool  = True
    FUNDING_RATE_MAX_FOR_LONG: float = 0.0003   # no abrir LONG si funding actual > 0.03%/8h
    FUNDING_RATE_MIN_FOR_SHORT: float = -0.0003  # no abrir SHORT si funding actual < -0.03%/8h

    # Filtro de régimen: Efficiency Ratio (detecta mercados laterales/choppy)
    # ER = |movimiento neto en N velas| / |suma de movimientos individuales|
    # ER → 1.0 = tendencia limpia; ER → 0.0 = lateral/ruidoso
    # REGIME_ER_MIN = 0 desactiva el filtro
    REGIME_ER_PERIOD: int   = 10    # ventana de cálculo (10 × 4H ≈ 1.7 días)
    REGIME_ER_MIN:    float = 0.2   # umbral mínimo para considerar mercado trending

    # Mean-reversion para los periodos laterales/choppy (ER < REGIME_ER_MIN), probada en
    # backtest_hybrid.py como complemento no correlacionado a la EMA crossover de tendencia.
    # DESACTIVADA por defecto: validado en BTC/ETH/SOL (36 meses) con 5 combinaciones de
    # RSI/SL/TP — en TODAS, el motor MR resultó neutro o negativo, y entre más permisivo el
    # umbral, peor (hasta -16% de retorno en RSI 40/60). RSI extremo en régimen ER lateral
    # no predice reversión a la media en 4H para estos símbolos con esta implementación.
    # El código queda disponible para futuras iteraciones (ej. Bollinger Bands, confirmación
    # por volumen) — no activar sin volver a validar con backtest_hybrid.py + walkforward.py.
    MR_ENABLED:           bool  = False
    MR_RSI_OVERSOLD:      float = 25.0   # RSI <= esto en régimen lateral -> long de reversión
    MR_RSI_OVERBOUGHT:    float = 75.0   # RSI >= esto en régimen lateral -> short de reversión
    MR_SL_ATR_MULTIPLIER: float = 1.5    # stop más ajustado que el de tendencia (trade más corto)
    MR_TP_ATR_MULTIPLIER: float = 2.0    # objetivo de retorno a la media, no se deja correr

    # Notificaciones Telegram
    # Obtén el token con @BotFather y el chat_id con @userinfobot
    TELEGRAM_TOKEN:   str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID",   "")

    # Circuit breakers
    MAX_MONTHLY_DRAWDOWN: float = 0.08
    MAX_DAILY_DRAWDOWN:   float = 0.04   # pérdida máxima diaria (UTC) antes de pausar el día
    MAX_TRADES_PER_DAY:   int   = 4      # tope de aperturas nuevas por día, entre todos los símbolos
    COOLDOWN_AFTER_LOSSES: int  = 3      # pérdidas consecutivas que activan el cooldown
    COOLDOWN_HOURS:        float = 12.0  # horas sin abrir posiciones nuevas tras el cooldown


config = Config()
