from config import config


class RiskManager:
    def is_monthly_drawdown_exceeded(self, current_balance: float, month_start_balance: float) -> bool:
        """Devuelve True si la pérdida desde inicio de mes supera MAX_MONTHLY_DRAWDOWN."""
        if month_start_balance <= 0:
            return False
        drawdown = (month_start_balance - current_balance) / month_start_balance
        return drawdown >= config.MAX_MONTHLY_DRAWDOWN

    def is_daily_drawdown_exceeded(self, current_balance: float, day_start_balance: float) -> bool:
        """Devuelve True si la pérdida desde inicio del día (UTC) supera MAX_DAILY_DRAWDOWN."""
        if day_start_balance <= 0:
            return False
        drawdown = (day_start_balance - current_balance) / day_start_balance
        return drawdown >= config.MAX_DAILY_DRAWDOWN

    def get_liquidation_price(
        self, entry_price: float, side: str, leverage: int | None = None, mmr: float | None = None
    ) -> float:
        """Estimación aproximada del precio de liquidación en margen ISOLATED, ignorando
        funding/PnL no realizado y los tiers reales de margen de mantenimiento de Binance.
        Solo sirve como red de seguridad (ver is_sl_safe_from_liquidation), no para sizing."""
        leverage = leverage or config.LEVERAGE
        mmr = config.MAINTENANCE_MARGIN_RATE if mmr is None else mmr
        if side == "LONG":
            return entry_price * (1 - 1 / leverage + mmr)
        return entry_price * (1 + 1 / leverage - mmr)

    def is_sl_safe_from_liquidation(
        self, entry_price: float, sl_price: float, side: str, leverage: int | None = None
    ) -> bool:
        """True si el SL se dispararía mucho antes de llegar al precio de liquidación
        estimado (margen >= LIQUIDATION_SAFETY_BUFFER). Si no, abrir la posición es
        peligroso: un gap o slippage podría liquidar antes de que el SL ejecute."""
        liq_price = self.get_liquidation_price(entry_price, side, leverage)
        if side == "LONG":
            sl_distance  = entry_price - sl_price
            liq_distance = entry_price - liq_price
        else:
            sl_distance  = sl_price - entry_price
            liq_distance = liq_price - entry_price
        if sl_distance <= 0 or liq_distance <= 0:
            return False
        return liq_distance >= config.LIQUIDATION_SAFETY_BUFFER * sl_distance

    def calculate_position_size(
        self, balance: float, price: float, atr: float | None = None,
        vol_multiplier: float = 1.0, adx: float | None = None,
    ) -> float:
        """Calcula la cantidad a comprar/vender arriesgando RISK_PER_TRADE del balance.
        vol_multiplier < 1.0 reduce el riesgo en entornos de alta volatilidad.
        El apalancamiento (config.LEVERAGE) no entra en esta fórmula: el riesgo objetivo
        en USDT es el mismo con o sin leverage, solo cambia el margen bloqueado."""
        risk_amount = balance * config.RISK_PER_TRADE * vol_multiplier
        if atr and atr > 0:
            stop_distance = atr * self.get_trailing_multiplier(adx)
        else:
            stop_distance = price * 0.02  # fallback si ATR no disponible
        qty = risk_amount / stop_distance
        qty = round(qty, 5)
        return qty if qty >= config.MIN_QUANTITY else 0.0

    def calculate_position_size_capped(
        self, balance: float, open_risk_usdt: float, price: float, atr: float | None = None,
        vol_multiplier: float = 1.0, adx: float | None = None,
    ) -> tuple[float, float]:
        """Igual que calculate_position_size, pero recorta el riesgo nominal de la nueva
        posición para que el riesgo abierto total (open_risk_usdt + esta posición) no supere
        PORTFOLIO_RISK_CAP del balance. Devuelve (qty, risk_usdt_asignado) — risk_usdt_asignado
        es el riesgo NOMINAL (pre-recorte) que debe registrarse en el estado del símbolo, para
        que el cap se aplique sobre el riesgo objetivo de cada posición y no se vaya diluyendo
        con recortes sucesivos."""
        nominal_risk = balance * config.RISK_PER_TRADE * vol_multiplier
        available_risk = max(0.0, balance * config.PORTFOLIO_RISK_CAP - open_risk_usdt)
        risk_amount = min(nominal_risk, available_risk)

        if risk_amount <= 0:
            return 0.0, 0.0

        if atr and atr > 0:
            stop_distance = atr * self.get_trailing_multiplier(adx)
        else:
            stop_distance = price * 0.02  # fallback si ATR no disponible
        qty = round(risk_amount / stop_distance, 5)
        if qty < config.MIN_QUANTITY:
            return 0.0, 0.0
        return qty, nominal_risk

    def get_trailing_multiplier(self, adx: float | None) -> float:
        """Multiplicador de ATR para el trailing stop, adaptado a la fuerza de tendencia.
        Tendencia fuerte (ADX alto) → stop más ancho, deja correr al ganador.
        Mercado débil/choppy (ADX bajo) → stop más ajustado, protege beneficios antes."""
        if adx is not None and adx >= config.ADX_TREND_THRESHOLD:
            return config.ATR_SL_MULTIPLIER_TREND
        return config.ATR_SL_MULTIPLIER_CHOP

    def get_stop_loss(
        self, entry_price: float, side: str, atr: float | None = None, adx: float | None = None
    ) -> float:
        if atr and atr > 0:
            distance = atr * self.get_trailing_multiplier(adx)
        else:
            distance = entry_price * 0.02  # fallback 2%
        if side == "BUY":
            return round(entry_price - distance, 2)
        return round(entry_price + distance, 2)

    def get_take_profit(
        self, entry_price: float, side: str, atr: float | None = None
    ) -> float:
        if atr and atr > 0:
            distance = atr * config.ATR_TP_MULTIPLIER
        else:
            distance = entry_price * 0.04  # fallback 4%
        if side == "BUY":
            return round(entry_price + distance, 2)
        return round(entry_price - distance, 2)
