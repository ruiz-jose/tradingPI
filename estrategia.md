# Estrategia del bot

Este documento describe qué hace el bot, cómo decide entrar/salir, qué gestión de riesgo
aplica, y qué resultados de validación respaldan (o descartan) cada decisión de diseño.
Es trend-following sobre EMA crossover, con filtros de régimen, gestión de riesgo basada en
ATR, y circuit breakers — corre sobre Binance Futures (USD-M), multi-símbolo, con largos y
cortos. Cada elección de parámetro relevante fue validada con `backtest.py`, `backtest_multi.py`,
`backtest_futures.py` o `walkforward.py`, no por intuición.

## 1. Dónde opera

- **Exchange**: Binance Futures USD-M (`bot.py`), modo de margen `ISOLATED`, apalancamiento `LEVERAGE = 2x`.
- **Símbolos en vivo**: `SYMBOLS = BTCUSDT, ETHUSDT, SOLUSDT` — uno corre independiente del otro,
  compartiendo un solo balance de cuenta y un cap de `MAX_CONCURRENT_POSITIONS = 3` posiciones abiertas.
- **Intervalo principal**: `INTERVAL = 4h`. Probado contra `1h/2h` (Sharpe negativo, puro ruido) y contra
  `8h` (2026-06-21): el grid search de `optimize_portfolio.py` encontró `8h` con Sharpe in-sample de 1.83
  (36m), pero la validación walk-forward out-of-sample (60m totales, train 36/test 3, 8 folds) lo
  RECHAZÓ — ver sección 8 para el detalle. `4h` sigue siendo el único timeframe validado.
- **Filtro HTF (Higher Time Frame)**: `HTF_INTERVAL = 1d`, misma EMA 9/21 sobre velas diarias, para
  confirmar la tendencia macro antes de operar el timeframe principal.

## 2. Señal base: EMA Crossover 9/21

- `EMA_FAST = 9`, `EMA_SLOW = 21`, evaluadas al cierre de cada vela de 4h.
- **BUY** (golden cross): la EMA rápida cruza por encima de la lenta.
- **SELL** (death cross): la EMA rápida cruza por debajo de la lenta.
- Sin cruce → `HOLD`.

## 3. Largos y cortos

El bot puede abrir tanto `LONG` como `SHORT`, pero no de forma simétrica — los shorts están
restringidos por símbolo según lo que muestra la evidencia:

- **LONG**: señal BUY + HTF diario alcista + filtros de la sección 4.
- **SHORT**: señal SELL + HTF diario bajista + filtros simétricos (RSI 20-60 en vez de 40-80)
  + **el símbolo debe estar en `SHORT_ENABLED_SYMBOLS`**.

### Por qué BTCUSDT no tiene shorts habilitados

`SHORT_ENABLED_SYMBOLS = ETHUSDT, SOLUSDT` (BTCUSDT excluido). Validado con `backtest_futures.py`
(36 y 60 meses) y un sweep de 10 combinaciones de filtros (ADX 15/20/25, RSI-sell más estricto,
ER 0.35/0.5, combinados):

| Símbolo | Win rate SHORT | PF SHORT |
|---|---|---|
| BTCUSDT | ~19% | ~0.5 (pérdida neta consistente, en todas las variantes de filtro probadas) |
| ETHUSDT | ~32% | ~1.07 |
| SOLUSDT | ~33% | ~1.12 |

La señal death-cross + HTF-bajista no tiene ventaja real para shortear BTC en el periodo
analizado (sesgo alcista estructural); en ETH/SOL es neutra a levemente positiva. Revalidar
con `backtest_futures.py` antes de añadir BTCUSDT a la lista.

## 4. Filtros de entrada (largos y cortos)

Una señal de cruce no basta — debe pasar todos estos filtros (`can_enter_long` / `can_enter_short`
en `strategy.py`):

1. **RSI dentro de rango**:
   - Largos: `RSI_BUY_MIN=40.0` – `RSI_BUY_MAX=80.0`
   - Cortos: `RSI_SELL_MIN=20.0` – `RSI_SELL_MAX=60.0`
2. **ADX mínimo** (`ADX_MIN = 0.0`, **desactivado**): `walkforward.py` (48 meses, train12/test3,
   8 folds out-of-sample) probó ADX_MIN en [0, 15, 20, 25] — la combinación ganadora out-of-sample
   fue casi siempre 0. El filtro ER (punto 4) ya cubre la detección de mercado lateral; activar ADX
   además solo recorta operaciones sin mejorar el Sharpe real. Revalidar periódicamente con datos nuevos.
3. **Régimen de volatilidad normal** (`ATR_VOL_MIN_RATIO=0.5`, `ATR_VOL_MAX_RATIO=3.0`): no entra si
   `current_atr` es menor al 50% del promedio histórico (mercado muerto) NI mayor al 300% (evento
   extremo / flash-crash, donde el sizing por ATR normal subestima el riesgo real — kill-switch).
4. **Mercado trending** (`REGIME_ER_MIN=0.2`, Efficiency Ratio, periodo 10): se requiere `current_er >= 0.2`.
   ER mide `|movimiento neto| / |suma de movimientos individuales|` — cerca de 1.0 es tendencia
   limpia, cerca de 0.0 es lateral/ruidoso. Este es el filtro de régimen que de verdad importa.

### Filtro HTF

- `ALLOW_BUY_IN_BEARISH_HTF = False`: si el HTF diario no confirma la dirección (bajista para LONG,
  alcista para SHORT), la entrada se omite — no se opera contra-tendencia diaria.
- Validado con `backtest.py` (36 y 60 meses): permitir BUY en HTF bajista (`True`) da Sharpe 0.18-0.50
  y PF 1.08-1.25; con `False` (default) da Sharpe 0.61-1.19 y PF 1.57-2.31. Comprar contra el HTF
  diario con apalancamiento castiga el rendimiento de forma consistente en ambas ventanas.
- *Nota histórica*: hasta hace poco había una discrepancia donde `bot.py` respetaba este flag pero
  `backtest.py` exigía HTF alcista siempre sin importar su valor — los backtests reportados no
  correspondían al comportamiento real en vivo. Ya está corregido en ambos lados.

## 5. Mean-reversion (probado, descartado)

Se construyó un motor adicional (`mr_long_signal` / `mr_short_signal` en `strategy.py`,
`backtest_hybrid.py`) que abre operaciones de reversión a la media (RSI extremo) específicamente
en los periodos que el filtro ER marca como laterales — mutuamente excluyente con la señal de
tendencia por construcción, para intentar operar también cuando la EMA crossover no tiene edge.

**`MR_ENABLED = False`**. Se probaron 5 combinaciones de RSI/SL/TP en BTC, ETH y SOL (36 meses):
en todas, el motor MR resultó neutro o negativo, y entre más permisivo el umbral peor el resultado
(hasta -16% de retorno con RSI 40/60 en BTC). RSI extremo dentro de un régimen ER-lateral no predice
reversión a la media en 4H para estos símbolos con esta implementación. El código queda disponible
para una futura iteración con una señal distinta (Bollinger Bands, confirmación por volumen) — no
activar sin revalidar con `backtest_hybrid.py` + `walkforward.py`.

## 6. Gestión de riesgo y salida

### Sizing (independiente del apalancamiento)

`RiskManager.calculate_position_size()`: arriesga `RISK_PER_TRADE = 0.5%` del balance por operación,
con distancia de stop = ATR × multiplicador adaptativo (ver abajo), y reducido por `vol_multiplier`
(`min(1.0, avg_atr/current_atr)`, reduce tamaño en alta volatilidad). El `LEVERAGE` (2x) solo
determina cuánto margen se bloquea en Binance, no cuánto USDT se arriesga — esa separación es
deliberada para no acoplar riesgo a apalancamiento.

### Trailing stop adaptativo por ADX

En vez de un multiplicador de ATR fijo, `get_trailing_multiplier()` usa:
- `ATR_SL_MULTIPLIER_TREND = 3.0` cuando `ADX >= ADX_TREND_THRESHOLD (25)` — tendencia fuerte,
  stop más ancho para no cortar al ganador antes de tiempo.
- `ATR_SL_MULTIPLIER_CHOP = 2.0` cuando ADX está por debajo — mercado débil, protege beneficios antes.

### Take-profit como techo de seguridad

`ATR_TP_MULTIPLIER = 4.0`: con `TRAILING_STOP=True`, el TP fijo se sigue evaluando como techo de
seguridad (antes era código muerto que nunca se alcanzaba — bug corregido). El trailing normalmente
cierra antes si el precio retrocede, pero el TP fijo cierra si el movimiento es tan favorable que
llega a ese nivel igual.

### Scale-out

`SCALE_OUT_R = 0.0` (**desactivado**). Soportado en el código (toma parcial de beneficios a
`SCALE_OUT_R × ATR` y mueve el stop a break-even), pero no se encontró una combinación que mejorase
el Sharpe de forma consistente al validarlo. Revisar con `walkforward.py` si se ajustan otros parámetros.

### Liquidación (Futuros)

`RiskManager.is_sl_safe_from_liquidation()`: antes de abrir cualquier posición, se estima el precio
de liquidación (aproximado, ISOLATED, ignora funding/tiers reales — `MAINTENANCE_MARGIN_RATE = 0.4%`)
y se exige que la distancia a liquidación sea `>= LIQUIDATION_SAFETY_BUFFER (1.5x)` la distancia al
SL. Si no se cumple, la entrada se aborta — red de seguridad, no un cálculo exacto de margen.

## 7. Circuit breakers y límites operativos

- **Mensual**: `MAX_MONTHLY_DRAWDOWN = 8%` — pausa todas las entradas hasta el próximo mes si el
  balance cae ese % desde el inicio del mes.
- **Diario**: `MAX_DAILY_DRAWDOWN = 4%` — igual que el mensual pero por día UTC.
- **Máximo de operaciones por día**: `MAX_TRADES_PER_DAY = 4`, contado entre todos los símbolos.
- **Cooldown tras pérdidas consecutivas**: `COOLDOWN_AFTER_LOSSES = 3` pérdidas seguidas (en
  cualquier símbolo) activan `COOLDOWN_HOURS = 12` horas sin abrir ninguna posición nueva.

Todos generan notificación por Telegram (`msg_circuit_breaker`, `msg_cooldown`).

## 8. Resultados de validación (resumen)

| Validación | Periodo | PF | Sharpe | Max DD | Veredicto |
|---|---|---|---|---|---|
| `backtest.py` (BTC, long-only) | 36m | 2.31 | 1.19 | 2.8% | BUENA (4/5) |
| `backtest.py` (BTC, long-only) | 60m | 1.57 | 0.61 | 2.8% | ACEPTABLE (3/5) |
| `backtest_multi.py` (BTC+ETH+SOL) | 36m | — | 0.79 | 5.8% | ACEPTABLE (2/4) |
| `walkforward.py` (out-of-sample, BTC) | 60m | 2.96 | 0.26 | — | confirma edge, Sharpe débil |

**Lectura honesta**: gestión de riesgo de nivel profesional (drawdown siempre bajo, 2.8-5.8% en
todas las pruebas) con edge direccional modesto y baja frecuencia de operación (~1-2 trades/mes en
BTC 4h). El Sharpe nunca alcanza el estándar "bueno" (≥1.0) fuera de la ventana más favorable. El
techo actual de la estrategia parece más estructural que un problema de calibración de parámetros.

### Experimento descartado: timeframe 8H (2026-06-21)

`optimize_portfolio.py` (grid search exhaustivo: timeframe × EMA × ER × ATR SL/TP × breakeven,
miles de combinaciones) encontró `INTERVAL=8h` + `ATR_SL_MULTIPLIER_TREND/CHOP=2.5/1.5` +
`ATR_TP_MULTIPLIER=5.0` + `REGIME_ER_PERIOD=12` como ganador in-sample (36m): Sharpe portfolio
**1.83**, PF BTC 3.16, Max DD 1.63% — números muy por encima de cualquier configuración previa.

Validación walk-forward (`find_best_strategy.py: validate_walkforward`, 60m totales, train 36m /
test 3m, 8 folds out-of-sample) **rechazó el cambio**:
- BTC: 4 trades en 24 meses OOS, retorno acumulado +0.84%
- ETH: 13 trades en 24 meses OOS, **Sharpe OOS promedio -1.72** (fold 2026-03→06 con Sharpe -12.52
  sobre solo 2 trades), retorno acumulado -0.12%
- SOL: 9 trades en 24 meses OOS, retorno acumulado +0.74%

Con un timeframe más largo (8h vs 4h) la estrategia opera todavía con menos frecuencia (32
trades/36m in-sample vs 57-107 en 4h) — exactamente el mismo patrón que ya descartó break-even y
scale-out: un grid search exhaustivo sobre una muestra de ~30 operaciones encuentra fácilmente una
combinación que luce excelente in-sample por azar, pero no hay suficientes operaciones OOS para
demostrar que el resultado generaliza, y al menos un símbolo (ETH) muestra reversión clara a
negativo fuera de muestra. **Se mantiene `INTERVAL=4h`** con los parámetros ATR/ER previamente
validados. `find_best_strategy.py` y `optimize_portfolio.py` quedan disponibles para futuras
búsquedas, pero cualquier ganador in-sample debe pasar por `validate_walkforward` con un mínimo de
operaciones OOS razonable antes de aplicarse a `config.py`.

## 9. Mensajes de log relevantes

- `[<symbol>] WebSocket Futures activo. Esperando señales...` → el bot está recibiendo datos para ese símbolo.
- `[<symbol>] HTF actualizado (...) : bajista` → el filtro diario está bajista para ese símbolo.
- `[<symbol>] HTF bajista — LONG omitido (contratendencia)` / `HTF no bajista — SHORT omitido` → entrada rechazada por el filtro HTF.
- `[<symbol>] Filtros no superados — LONG/SHORT omitido | RSI: ... | ADX: ...` → no pasó RSI/ADX/régimen.
- `[<symbol>] Shorts deshabilitados para este símbolo` → señal SHORT rechazada por `SHORT_ENABLED_SYMBOLS`.
- `[<symbol>] LONG/SHORT ABORTADO — SL demasiado cerca del precio de liquidación` → red de seguridad de liquidación activada.
- `[<symbol>] LONG/SHORT ejecutado | Qty: ... | SL: ... | TP: ...` → posición abierta.
- `COOLDOWN activado tras N pérdidas consecutivas` / `CIRCUIT BREAKER: drawdown ... — operaciones suspendidas` → límites operativos activados.

## 10. Resumen de condiciones para abrir una posición

1. No hay cooldown activo y no se alcanzó `MAX_TRADES_PER_DAY`.
2. Señal de cruce EMA (BUY o SELL) en el cierre de la vela de 4h.
3. No hay posición abierta en ese símbolo, y no se alcanzó `MAX_CONCURRENT_POSITIONS` global.
4. HTF diario confirma la dirección (o `ALLOW_BUY_IN_BEARISH_HTF` lo permite — desactivado por default).
5. Si es SHORT: el símbolo está en `SHORT_ENABLED_SYMBOLS`.
6. RSI en rango (largo o corto según corresponda) + régimen ATR normal + mercado trending (ER) + ADX (si está activo).
7. No hay drawdown mensual ni diario por encima de sus límites.
8. El tamaño de posición calculado es positivo.
9. La distancia al SL es segura frente al precio de liquidación estimado.

Si cualquiera de estas condiciones falla, el bot no abre la operación.
