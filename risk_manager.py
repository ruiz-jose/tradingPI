from config import config


class RiskManager:
    def is_monthly_drawdown_exceeded(self, current_balance: float, month_start_balance: float) -> bool:
        """Devuelve True si la pérdida desde inicio de mes supera MAX_MONTHLY_DRAWDOWN."""
        if month_start_balance <= 0:
            return False
        drawdown = (month_start_balance - current_balance) / month_start_balance
        return drawdown >= config.MAX_MONTHLY_DRAWDOWN

    def calculate_position_size(
        self, balance: float, price: float, atr: float | None = None, vol_multiplier: float = 1.0
    ) -> float:
        """Calcula la cantidad a comprar arriesgando RISK_PER_TRADE del balance.
        vol_multiplier < 1.0 reduce el riesgo en entornos de alta volatilidad."""
        risk_amount = balance * config.RISK_PER_TRADE * vol_multiplier
        if atr and atr > 0:
            stop_distance = atr * config.ATR_SL_MULTIPLIER
        else:
            stop_distance = price * 0.02  # fallback si ATR no disponible
        qty = risk_amount / stop_distance
        qty = round(qty, 5)
        return qty if qty >= config.MIN_QUANTITY else 0.0

    def get_stop_loss(
        self, entry_price: float, side: str, atr: float | None = None
    ) -> float:
        if atr and atr > 0:
            distance = atr * config.ATR_SL_MULTIPLIER
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
