from collections import deque


class EMAStrategy:
    """
    Estrategia EMA Crossover con gestión de riesgo mejorada:
      - ATR(14): gestión de riesgo adaptativa
      - RSI(14): filtro de entrada
      - ADX(14): disponible para diagnóstico

    Señal BUY: golden cross EMA (fast > slow) confirmado por HTF alcista
    Señal SELL: death cross EMA (fast < slow)
    Gestión de salida: trailing stop ATR cuando TRAILING_STOP=True
    """

    def __init__(
        self,
        fast_period: int,
        slow_period: int,
        atr_period: int = 14,
        rsi_period: int = 14,
        rsi_min: float = 40.0,
        rsi_max: float = 70.0,
        adx_period: int = 14,
        adx_min: float = 0.0,
        atr_vol_period: int = 50,
        atr_vol_min_ratio: float = 0.5,
        er_period: int = 10,
        er_min: float = 0.0,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.rsi_period = rsi_period
        self.rsi_min = rsi_min
        self.rsi_max = rsi_max
        self.adx_min = adx_min

        self.ema_fast: float | None = None
        self.ema_slow: float | None = None
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

        self._candle_count: int = 0
        self._prices: deque = deque(maxlen=slow_period)

        # ATR
        self._prev_close: float | None = None
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._true_ranges: deque = deque(maxlen=atr_period)
        self.current_atr: float | None = None

        # RSI (Wilder)
        self._rsi_gains: deque = deque(maxlen=rsi_period)
        self._rsi_losses: deque = deque(maxlen=rsi_period)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self.current_rsi: float | None = None

        # ADX
        self._plus_dm_smooth: float | None = None
        self._minus_dm_smooth: float | None = None
        self._tr_smooth: float | None = None
        self._dx_values: deque = deque(maxlen=atr_period)
        self._adx_smooth: float | None = None
        self.current_adx: float | None = None

        # Normalización de volatilidad
        self._atr_vol_period = atr_vol_period
        self._atr_vol_min_ratio = atr_vol_min_ratio
        self._avg_atr: float | None = None

        # Efficiency Ratio: detecta regímenes trending vs choppy
        # ER = |precio[N] - precio[0]| / suma(|cambios individuales|)
        # ER → 1.0 = tendencia limpia; ER → 0.0 = mercado lateral/ruidoso
        self._er_period = er_period
        self._er_min    = er_min
        self._er_closes: deque = deque(maxlen=er_period + 1)
        self.current_er: float | None = None

    def update(
        self,
        close: float,
        high: float | None = None,
        low: float | None = None,
        volume: float | None = None,
        closed: bool = True,
    ) -> None:
        if not closed:
            return

        if self._prev_close is not None and high is not None and low is not None:
            # ── ATR ─────────────────────────────────────────────────────
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
            self._true_ranges.append(tr)
            if len(self._true_ranges) == self.atr_period:
                if self.current_atr is None:
                    self.current_atr = sum(self._true_ranges) / self.atr_period
                else:
                    self.current_atr = (self.current_atr * (self.atr_period - 1) + tr) / self.atr_period
                # ATR promedio histórico para normalización de volatilidad
                k_vol = 2.0 / (self._atr_vol_period + 1)
                if self._avg_atr is None:
                    self._avg_atr = self.current_atr
                else:
                    self._avg_atr = self._avg_atr * (1 - k_vol) + self.current_atr * k_vol

            # ── ADX ─────────────────────────────────────────────────────
            if self._prev_high is not None and self._prev_low is not None:
                up   = high - self._prev_high
                down = self._prev_low - low
                plus_dm  = up   if (up > down and up > 0)   else 0.0
                minus_dm = down if (down > up and down > 0) else 0.0
                k = self.atr_period
                if self._plus_dm_smooth is None:
                    self._plus_dm_smooth  = plus_dm
                    self._minus_dm_smooth = minus_dm
                    self._tr_smooth       = tr
                else:
                    self._plus_dm_smooth  = (self._plus_dm_smooth  * (k - 1) + plus_dm)  / k
                    self._minus_dm_smooth = (self._minus_dm_smooth * (k - 1) + minus_dm) / k
                    self._tr_smooth       = (self._tr_smooth       * (k - 1) + tr)       / k
                if self._tr_smooth and self._tr_smooth > 0:
                    plus_di  = 100 * self._plus_dm_smooth  / self._tr_smooth
                    minus_di = 100 * self._minus_dm_smooth / self._tr_smooth
                    di_sum = plus_di + minus_di
                    if di_sum > 0:
                        dx = 100 * abs(plus_di - minus_di) / di_sum
                        self._dx_values.append(dx)
                        if len(self._dx_values) == self.atr_period:
                            if self._adx_smooth is None:
                                self._adx_smooth = sum(self._dx_values) / self.atr_period
                            else:
                                self._adx_smooth = (self._adx_smooth * (k - 1) + dx) / k
                            self.current_adx = self._adx_smooth

            # ── RSI ─────────────────────────────────────────────────────
            change = close - self._prev_close
            gain   = max(change, 0.0)
            loss   = max(-change, 0.0)
            self._rsi_gains.append(gain)
            self._rsi_losses.append(loss)
            if len(self._rsi_gains) == self.rsi_period:
                if self._avg_gain is None:
                    self._avg_gain = sum(self._rsi_gains) / self.rsi_period
                    self._avg_loss = sum(self._rsi_losses) / self.rsi_period
                else:
                    self._avg_gain = (self._avg_gain * (self.rsi_period - 1) + gain) / self.rsi_period
                    self._avg_loss = (self._avg_loss * (self.rsi_period - 1) + loss) / self.rsi_period
                self.current_rsi = (
                    100.0 if self._avg_loss == 0
                    else 100 - (100 / (1 + self._avg_gain / self._avg_loss))
                )

        self._prev_close = close
        self._prev_high  = high
        self._prev_low   = low
        self._prices.append(close)
        self._candle_count += 1

        self._prev_fast = self.ema_fast
        self._prev_slow = self.ema_slow

        if self._candle_count == self.fast_period:
            self.ema_fast = sum(list(self._prices)[-self.fast_period:]) / self.fast_period
        elif self._candle_count > self.fast_period:
            k = 2 / (self.fast_period + 1)
            self.ema_fast = close * k + self.ema_fast * (1 - k)

        if self._candle_count == self.slow_period:
            self.ema_slow = sum(self._prices) / self.slow_period
        elif self._candle_count > self.slow_period:
            k = 2 / (self.slow_period + 1)
            self.ema_slow = close * k + self.ema_slow * (1 - k)

        # ── Efficiency Ratio ─────────────────────────────────────────
        self._er_closes.append(close)
        if len(self._er_closes) == self._er_period + 1:
            ec = list(self._er_closes)
            direction  = abs(ec[-1] - ec[0])
            volatility = sum(abs(ec[i] - ec[i - 1]) for i in range(1, len(ec)))
            self.current_er = direction / volatility if volatility > 0 else 0.0

    def get_signal(self) -> str:
        """Golden cross → BUY, death cross → SELL."""
        if None in (self.ema_fast, self.ema_slow, self._prev_fast, self._prev_slow):
            return "HOLD"

        if self._prev_fast <= self._prev_slow and self.ema_fast > self.ema_slow:
            return "BUY"

        if self._prev_fast >= self._prev_slow and self.ema_fast < self.ema_slow:
            return "SELL"

        return "HOLD"

    @property
    def vol_multiplier(self) -> float:
        """Multiplicador de posición [0,1]: reduce cuando ATR > promedio.
        En entornos de alta volatilidad, la posición ATR-sizing ya es pequeña.
        Reducirla más normaliza la distribución de retornos entre operaciones.
        - ATR > avg: multiplier = avg/current < 1.0 (reduce en alta vol)
        - ATR ≤ avg: multiplier = 1.0 (tamaño completo en volatilidad normal/baja)
        """
        if self.current_atr is None or self._avg_atr is None or self._avg_atr == 0:
            return 1.0
        return min(1.0, self._avg_atr / self.current_atr)

    @property
    def _atr_regime_ok(self) -> bool:
        """True si la volatilidad actual está dentro de régimen normal (no demasiado quieto)."""
        if self.current_atr is None or self._avg_atr is None:
            return True  # sin datos → no bloquear
        return self.current_atr >= self._atr_vol_min_ratio * self._avg_atr

    @property
    def is_trending(self) -> bool:
        """True si el régimen actual es trending (ER ≥ er_min). Si er_min=0, siempre True."""
        if self._er_min <= 0:
            return True
        return self.current_er is not None and self.current_er >= self._er_min

    @property
    def can_enter_long(self) -> bool:
        """RSI en rango + ADX (si activo) + régimen ATR normal + ER trending."""
        if self.current_rsi is None:
            return False
        rsi_ok = self.rsi_min <= self.current_rsi <= self.rsi_max
        if self.adx_min > 0:
            if self.current_adx is None:
                return False
            adx_ok = self.current_adx >= self.adx_min
        else:
            adx_ok = True
        return rsi_ok and adx_ok and self._atr_regime_ok and self.is_trending

    @property
    def is_bullish(self) -> bool:
        if self.ema_fast is None or self.ema_slow is None:
            return False
        return self.ema_fast > self.ema_slow

    @property
    def is_ready(self) -> bool:
        return self._candle_count >= self.slow_period
