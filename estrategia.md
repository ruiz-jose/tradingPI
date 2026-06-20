# Estrategia del bot

Este documento describe cuándo el bot decide operar y qué condiciones debe cumplir antes de abrir una posición.

## 1. Marco temporal principal

- El bot trabaja con velas de `4h` como intervalo principal (`config.INTERVAL = "4h"`).
- Las decisiones de entrada se toman al cierre de una vela de `4h`.

## 2. Señales de entrada

La estrategia genera señales basadas en medias móviles exponenciales (EMA):

- `EMA_FAST = 9`
- `EMA_SLOW = 21`

### 2.1 Señal BUY

Se produce una señal `BUY` cuando ocurre un cruce dorado en el cierre de la vela:
- la EMA rápida (`EMA_FAST`) cruza por encima de la EMA lenta (`EMA_SLOW`).

### 2.2 Señal SELL

Se produce una señal `SELL` cuando ocurre un cruce bajista:
- la EMA rápida cruza por debajo de la EMA lenta.

### 2.3 Señal HOLD

Si no hay cruce relevante, la señal es `HOLD`.

## 3. Filtro HTF (Higher Time Frame)

El bot usa un filtro de tendencia en un marco temporal mayor:

- `HTF_INTERVAL = "1d"` (diario)
- El HTF se actualiza periódicamente y su tendencia se determina con las mismas EMAs (9 y 21) sobre velas diarias.

### Condición para operar BUY

- `ALLOW_BUY_IN_BEARISH_HTF = False` (default): si el HTF diario está `bajista`, el bot omite la entrada BUY.
- Validado con backtest.py (36 y 60 meses): permitir BUY en HTF bajista (`True`) reduce el Sharpe
  de ~1.19 a ~0.50 y el Profit Factor de ~2.31 a ~1.25 — comprar contra la tendencia diaria con
  apalancamiento castiga el rendimiento. Antes había una discrepancia donde bot.py respetaba este
  flag pero backtest.py exigía HTF alcista siempre sin importar su valor; ya está corregido en
  ambos lados.

## 4. Filtros adicionales de entrada

Además del cruce de EMAs y del HTF alcista, el bot exige que la estrategia principal pase estos filtros:

1. RSI dentro del rango de compra:
   - `RSI_BUY_MIN = 40.0`
   - `RSI_BUY_MAX = 80.0`

2. ADX mínimo (si está activo):
   - `ADX_MIN = 0` en tu configuración actual, por lo que el ADX no bloquea operaciones.
   - Si ADX estuviera activado (> 0), se requeriría `current_adx >= ADX_MIN`.

3. Régimen de volatilidad normal:
   - El bot compara el ATR actual contra un ATR promedio histórico.
   - `ATR_VOL_MIN_RATIO = 0.5`
   - No entra si `current_atr < ATR_VOL_MIN_RATIO * avg_atr`.

4. Mercado trending según Efficiency Ratio (ER):
   - `REGIME_ER_MIN = 0.2`
   - Se requiere `current_er >= 0.2` para considerar el mercado con tendencia limpia.

## 5. Gestión de riesgo antes de abrir posición

Antes de crear una orden, el bot verifica:

- Si el drawdown mensual supera `MAX_MONTHLY_DRAWDOWN = 0.08` (8 %), no opera.
- Calcula el tamaño de posición con `RiskManager.calculate_position_size(...)`.
- Si el resultado `qty <= 0`, la orden se omite.

## 6. Resumen de condiciones para abrir BUY

El bot abre una orden `BUY` sólo si se cumplen todas estas condiciones:

1. Señal principal = `BUY`.
2. No existen posiciones abiertas (`not self.in_position`).
3. HTF diario es `alcista`.
4. `current_rsi` entre 40 y 80.
5. Si ADX está activo: `current_adx >= ADX_MIN`.
6. ATR actual en régimen normal: `current_atr >= ATR_VOL_MIN_RATIO * avg_atr`.
7. Mercado trending: `current_er >= REGIME_ER_MIN`.
8. No hay drawdown mensual superior a 8 %.
9. El tamaño de posición calculado es positivo.

## 7. Mensajes de log relevantes

Estos son los mensajes que indican acción o bloqueo:

- `WebSocket activo. Esperando señales...` → el bot está recibiendo datos.
- `HTF [1d] actualizado (...) : bajista` → el filtro diario está bajista.
- `HTF [1d] bajista — BUY omitido (señal en contratendencia)` → la señal BUY se rechazó porque el HTF no estaba alcista.
- `Filtros no superados — BUY omitido | RSI: ... | ADX: ...` → no pasó los filtros de RSI/ADX/regimen.
- `COMPRA ejecutada | Qty: ... | Precio: ...` → el bot abrió una posición BUY.

## 8. Conclusión

El bot opera sólo cuando hay una señal de cruce EMA en el intervalo principal y cuando el contexto diario y los filtros de volatilidad, RSI y ER permiten la entrada. Si el HTF está bajista o algún filtro no se cumple, el bot no abre la operación.
