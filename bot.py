import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from binance import AsyncClient, BinanceSocketManager
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
)

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from logger import TradeLogger
from notifier import (
    notify,
    msg_bot_started, msg_trade_open, msg_trade_open_short,
    msg_trade_close_signal, msg_trade_close_sl, msg_trade_close_tp,
    msg_scale_out, msg_circuit_breaker, msg_cooldown,
)

log = logging.getLogger(__name__)

_INTERVAL_SECONDS = {"1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400}



@dataclass
class SymbolState:
    """Estado de trading por símbolo — el bot corre uno de estos por cada
    par en config.SYMBOLS, compartiendo un único AsyncClient/balance de Futures."""
    symbol: str
    strategy: EMAStrategy
    htf_strategy: EMAStrategy
    in_position:    bool  = False
    side:           str | None = None   # "LONG" | "SHORT"
    entry_price:    float = 0.0
    entry_atr:      float = 0.0
    current_qty:    float = 0.0
    scale_out_done: bool  = False
    sl_order_id:    str | None = None
    tp_order_id:    str | None = None
    current_sl:     float = 0.0
    last_htf_open_time: int = 0


class TradingBot:
    """Bot multi-símbolo sobre Binance Futures (USD-M): permite largos y cortos,
    sizing por riesgo ATR (independiente del apalancamiento), trailing stop adaptativo
    por ADX, scale-out parcial y circuit breaker mensual de drawdown."""

    def __init__(self):
        self.client: AsyncClient | None = None
        self.risk_manager = RiskManager()
        self.trade_logger = TradeLogger()
        self.states: dict[str, SymbolState] = {}
        for symbol in config.SYMBOLS:
            strategy = EMAStrategy(
                config.EMA_FAST, config.EMA_SLOW, config.ATR_PERIOD,
                rsi_min=config.RSI_BUY_MIN, rsi_max=config.RSI_BUY_MAX,
                adx_period=config.ADX_PERIOD, adx_min=config.ADX_MIN,
                atr_vol_period=config.ATR_VOL_PERIOD,
                atr_vol_min_ratio=config.ATR_VOL_MIN_RATIO,
                atr_vol_max_ratio=config.ATR_VOL_MAX_RATIO,
                er_period=config.REGIME_ER_PERIOD,
                er_min=config.REGIME_ER_MIN,
                rsi_sell_min=config.RSI_SELL_MIN,
                rsi_sell_max=config.RSI_SELL_MAX,
                mr_rsi_oversold=config.MR_RSI_OVERSOLD,
                mr_rsi_overbought=config.MR_RSI_OVERBOUGHT,
            )
            htf_strategy = EMAStrategy(config.EMA_FAST, config.EMA_SLOW)
            self.states[symbol] = SymbolState(symbol=symbol, strategy=strategy, htf_strategy=htf_strategy)
        self.month_start_balance: float = 0.0
        self._current_month: int = 0
        self.day_start_balance: float = 0.0
        self._current_day = None
        self.trades_today: int = 0
        self.consecutive_losses: int = 0
        self.cooldown_until: datetime | None = None

    # ------------------------------------------------------------------ #
    # Ciclo principal                                                      #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self.client = await AsyncClient.create(
            config.FUTURES_API_KEY,
            config.FUTURES_API_SECRET,
        )
        if config.FUTURES_TESTNET:
            self.client.FUTURES_URL = self.client.FUTURES_TESTNET_URL
        mode = "TESTNET" if config.FUTURES_TESTNET else "LIVE"
        log.info("Conectado a Binance Futures [%s] | Pares: %s | Intervalo: %s",
                  mode, ", ".join(config.SYMBOLS), config.INTERVAL)

        for symbol in config.SYMBOLS:
            await self._setup_symbol(symbol)
            await self._warmup(self.states[symbol])

        initial_balance = await self._get_usdt_balance()
        self.month_start_balance = initial_balance
        self._current_month = datetime.now(timezone.utc).month
        self.day_start_balance = initial_balance
        self._current_day = datetime.now(timezone.utc).date()
        log.info("Balance base mensual: %.2f USDT (mes %d)", self.month_start_balance, self._current_month)

        await notify(msg_bot_started(config.SYMBOLS, config.INTERVAL, initial_balance, mode))

        tasks = []
        for symbol in config.SYMBOLS:
            tasks.append(self._run_socket(self.states[symbol]))
            tasks.append(self._refresh_htf_loop(self.states[symbol]))
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        if self.client:
            await self.client.close_connection()
            log.info("Conexión cerrada.")

    # ------------------------------------------------------------------ #
    # Configuración inicial por símbolo: leverage + tipo de margen        #
    # ------------------------------------------------------------------ #

    async def _setup_symbol(self, symbol: str) -> None:
        try:
            await self.client.futures_change_margin_type(symbol=symbol, marginType=config.MARGIN_TYPE)
        except Exception as exc:
            log.info("Margin type %s ya configurado para %s (%s)", config.MARGIN_TYPE, symbol, exc)
        try:
            await self.client.futures_change_leverage(symbol=symbol, leverage=config.LEVERAGE)
            log.info("Leverage %dx configurado para %s", config.LEVERAGE, symbol)
        except Exception as exc:
            log.warning("Error configurando leverage para %s: %s", symbol, exc)

    # ------------------------------------------------------------------ #
    # Calentamiento: carga velas históricas                               #
    # ------------------------------------------------------------------ #

    async def _warmup(self, state: SymbolState) -> None:
        klines = await self.client.futures_klines(
            symbol=state.symbol,
            interval=config.INTERVAL,
            limit=config.LOOKBACK_CANDLES,
        )
        for k in klines[:-1]:
            state.strategy.update(float(k[4]), high=float(k[2]), low=float(k[3]), volume=float(k[5]), closed=True)

        log.info(
            "[%s] Warmup %s: %d velas | EMA%d=%.2f | EMA%d=%.2f | ATR=%.2f",
            state.symbol, config.INTERVAL, len(klines) - 1,
            config.EMA_FAST, state.strategy.ema_fast or 0,
            config.EMA_SLOW, state.strategy.ema_slow or 0,
            state.strategy.current_atr or 0,
        )

        htf_klines = await self.client.futures_klines(
            symbol=state.symbol,
            interval=config.HTF_INTERVAL,
            limit=config.HTF_LOOKBACK_CANDLES,
        )
        for k in htf_klines[:-1]:
            state.htf_strategy.update(float(k[4]), closed=True)

        if len(htf_klines) >= 2:
            state.last_htf_open_time = int(htf_klines[-2][0])

        htf_trend = "alcista" if state.htf_strategy.is_bullish else "bajista"
        log.info(
            "[%s] Warmup HTF [%s]: %d velas | Tendencia: %s",
            state.symbol, config.HTF_INTERVAL, len(htf_klines) - 1, htf_trend,
        )

    # ------------------------------------------------------------------ #
    # Refresco periódico del filtro HTF                                   #
    # ------------------------------------------------------------------ #

    async def _refresh_htf_loop(self, state: SymbolState) -> None:
        sleep_secs = _INTERVAL_SECONDS.get(config.INTERVAL, 3600)
        while True:
            await asyncio.sleep(sleep_secs)
            try:
                klines = await self.client.futures_klines(
                    symbol=state.symbol, interval=config.HTF_INTERVAL, limit=10,
                )
                new_count = 0
                for k in klines[:-1]:
                    if int(k[0]) > state.last_htf_open_time:
                        state.htf_strategy.update(float(k[4]), closed=True)
                        new_count += 1
                if new_count and len(klines) >= 2:
                    state.last_htf_open_time = int(klines[-2][0])
                htf_trend = "alcista" if state.htf_strategy.is_bullish else "bajista"
                log.info("[%s] HTF actualizado (%d nuevas velas): %s", state.symbol, new_count, htf_trend)
            except Exception as exc:
                log.warning("[%s] Error al refrescar HTF: %s", state.symbol, exc)

    # ------------------------------------------------------------------ #
    # WebSocket de Futuros con reconexión automática                      #
    # ------------------------------------------------------------------ #

    async def _run_socket(self, state: SymbolState) -> None:
        backoff = 5
        while True:
            try:
                bm = BinanceSocketManager(self.client)
                async with bm.kline_futures_socket(symbol=state.symbol, interval=config.INTERVAL) as stream:
                    log.info("[%s] WebSocket Futures activo. Esperando señales...", state.symbol)
                    backoff = 5
                    while True:
                        msg = await stream.recv()
                        await self._process_message(state, msg)
            except Exception as exc:
                log.warning("[%s] WebSocket desconectado: %s — reconectando en %ds...", state.symbol, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _process_message(self, state: SymbolState, msg: dict) -> None:
        if msg.get("e") == "error":
            log.error("[%s] Error WebSocket: %s", state.symbol, msg)
            return

        kline = msg.get("k", msg)   # futures kline_futures_socket anida igual que spot
        close = float(kline["c"])
        high = float(kline["h"])
        low = float(kline["l"])
        volume = float(kline["v"])
        is_closed = kline["x"]

        state.strategy.update(close, high=high, low=low, volume=volume, closed=is_closed)

        if not is_closed:
            return

        await self._sync_orders(state)
        if state.in_position:
            await self._check_scale_out(state, close)
            await self._update_trailing_stop(state, close)

        signal = state.strategy.get_signal()
        htf_trend = "alcista" if state.htf_strategy.is_bullish else "bajista"
        log.info(
            "[%s] Vela cerrada | Precio: %.2f | EMA%d: %.2f | EMA%d: %.2f | ATR: %.2f | "
            "RSI: %.1f | ADX: %.1f | ER: %.2f | HTF: %s | Posición: %s | Señal: %s",
            state.symbol, close,
            config.EMA_FAST, state.strategy.ema_fast or 0,
            config.EMA_SLOW, state.strategy.ema_slow or 0,
            state.strategy.current_atr or 0,
            state.strategy.current_rsi or 0,
            state.strategy.current_adx or 0,
            state.strategy.current_er or 0,
            htf_trend, state.side or "FLAT", signal,
        )

        if state.in_position and state.side == "LONG" and signal == "SELL":
            await self._close_position(state, close)
        elif state.in_position and state.side == "SHORT" and signal == "BUY":
            await self._close_position(state, close)
        elif not state.in_position and signal == "BUY":
            await self._open_position(state, close, "LONG")
        elif not state.in_position and signal == "SELL":
            await self._open_position(state, close, "SHORT")

    # ------------------------------------------------------------------ #
    # Gestión mensual                                                     #
    # ------------------------------------------------------------------ #

    async def _refresh_month_balance(self, current_balance: float) -> None:
        now_month = datetime.now(timezone.utc).month
        if now_month != self._current_month:
            self._current_month = now_month
            self.month_start_balance = current_balance
            log.info("Nuevo mes — balance base mensual actualizado: %.2f USDT", current_balance)

    # ------------------------------------------------------------------ #
    # Límites operativos diarios + cooldown tras pérdidas consecutivas    #
    # ------------------------------------------------------------------ #

    async def _refresh_day_balance(self, current_balance: float) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._current_day:
            self._current_day = today
            self.day_start_balance = current_balance
            self.trades_today = 0
            log.info("Nuevo día UTC — balance base diario reiniciado: %.2f USDT", current_balance)

    def _record_trade_result(self, pnl: float) -> None:
        """Actualiza la racha de pérdidas consecutivas y activa el cooldown global si
        se alcanza COOLDOWN_AFTER_LOSSES. El cooldown bloquea TODAS las entradas nuevas
        (en todos los símbolos) hasta que pase COOLDOWN_HOURS."""
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= config.COOLDOWN_AFTER_LOSSES:
                self.cooldown_until = datetime.now(timezone.utc) + timedelta(hours=config.COOLDOWN_HOURS)
                log.warning("COOLDOWN activado tras %d pérdidas consecutivas — sin nuevas entradas hasta %s",
                            self.consecutive_losses, self.cooldown_until.strftime("%Y-%m-%d %H:%M UTC"))
                asyncio.create_task(notify(msg_cooldown(self.consecutive_losses, config.COOLDOWN_HOURS)))
        else:
            self.consecutive_losses = 0

    def _in_cooldown(self) -> bool:
        return self.cooldown_until is not None and datetime.now(timezone.utc) < self.cooldown_until

    def _count_open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.in_position)

    # ------------------------------------------------------------------ #
    # Apertura de posición (largo o corto)                                #
    # ------------------------------------------------------------------ #

    async def _open_position(self, state: SymbolState, price: float, side: str) -> None:
        is_long = side == "LONG"

        if self._in_cooldown():
            log.info("[%s] %s omitido — cooldown activo hasta %s (tras %d pérdidas consecutivas)",
                      state.symbol, side, self.cooldown_until.strftime("%Y-%m-%d %H:%M UTC"), self.consecutive_losses)
            return

        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            log.info("[%s] %s omitido — tope de %d operaciones/día alcanzado",
                      state.symbol, side, config.MAX_TRADES_PER_DAY)
            return

        if is_long:
            if not state.htf_strategy.is_bullish and not config.ALLOW_BUY_IN_BEARISH_HTF:
                log.info("[%s] HTF bajista — LONG omitido (contratendencia)", state.symbol)
                return
            if not state.strategy.can_enter_long:
                log.info("[%s] Filtros no superados — LONG omitido | RSI: %.1f | ADX: %.1f",
                          state.symbol, state.strategy.current_rsi or 0, state.strategy.current_adx or 0)
                return
        else:
            if state.symbol not in config.SHORT_ENABLED_SYMBOLS:
                log.info("[%s] Shorts deshabilitados para este símbolo (ver SHORT_ENABLED_SYMBOLS) — omitido",
                          state.symbol)
                return
            if not state.htf_strategy.is_bearish:
                log.info("[%s] HTF no bajista — SHORT omitido (contratendencia)", state.symbol)
                return
            if not state.strategy.can_enter_short:
                log.info("[%s] Filtros no superados — SHORT omitido | RSI: %.1f | ADX: %.1f",
                          state.symbol, state.strategy.current_rsi or 0, state.strategy.current_adx or 0)
                return

        if self._count_open_positions() >= config.MAX_CONCURRENT_POSITIONS:
            log.info("[%s] Cap de posiciones concurrentes alcanzado (%d) — entrada omitida",
                      state.symbol, config.MAX_CONCURRENT_POSITIONS)
            return

        funding_rate = await self._get_funding_rate(state.symbol)
        if self._funding_blocks_entry(side, funding_rate):
            log.info("[%s] %s omitido — funding rate desfavorable (%.4f%%/8h)",
                      state.symbol, side, (funding_rate or 0) * 100)
            return

        balance = await self._get_usdt_balance()
        await self._refresh_month_balance(balance)
        await self._refresh_day_balance(balance)

        if self.risk_manager.is_monthly_drawdown_exceeded(balance, self.month_start_balance):
            log.warning("[%s] CIRCUIT BREAKER: drawdown mensual >= límite — operaciones suspendidas",
                        state.symbol)
            await notify(msg_circuit_breaker(balance, self.month_start_balance, "mensual"))
            return

        if self.risk_manager.is_daily_drawdown_exceeded(balance, self.day_start_balance):
            log.warning("[%s] CIRCUIT BREAKER: drawdown diario >= límite — pausado hasta el próximo día UTC",
                        state.symbol)
            await notify(msg_circuit_breaker(balance, self.day_start_balance, "diario"))
            return

        atr = state.strategy.current_atr
        adx = state.strategy.current_adx
        vol_mult = state.strategy.vol_multiplier
        qty = self.risk_manager.calculate_position_size(balance, price, atr, vol_mult, adx)

        if qty <= 0:
            log.warning("[%s] Tamaño de posición insuficiente (balance: %.2f USDT). Omitido.",
                       state.symbol, balance)
            return

        risk_side = "BUY" if is_long else "SELL"
        sl = self.risk_manager.get_stop_loss(price, risk_side, atr, adx)
        tp = self.risk_manager.get_take_profit(price, risk_side, atr)

        if not self.risk_manager.is_sl_safe_from_liquidation(price, sl, side):
            liq = self.risk_manager.get_liquidation_price(price, side)
            log.error(
                "[%s] %s ABORTADO — SL (%.2f) demasiado cerca del precio de liquidación "
                "estimado (%.2f) con leverage %dx. Entrada omitida por seguridad.",
                state.symbol, side, sl, liq, config.LEVERAGE,
            )
            return

        order_side = SIDE_BUY if is_long else SIDE_SELL
        try:
            order = await self.client.futures_create_order(
                symbol=state.symbol, side=order_side,
                type=FUTURE_ORDER_TYPE_MARKET, quantity=qty,
            )
            self.trades_today += 1

            state.in_position    = True
            state.side           = side
            state.entry_price    = price
            state.entry_atr      = atr or 0.0
            state.scale_out_done = False
            state.current_qty    = qty
            state.current_sl     = sl

            self.trade_logger.log_trade({
                "action": "OPEN", "side": side, "symbol": state.symbol,
                "price": price, "quantity": qty, "stop_loss": sl, "take_profit": tp,
                "order_id": order["orderId"], "atr": round(atr, 2) if atr else None,
            })
            log.info("[%s] %s ejecutado | Qty: %s | Precio: %.2f | SL: %.2f | TP: %.2f | ATR: %.2f | VolMult: %.2f",
                     state.symbol, side, qty, price, sl, tp, atr or 0, vol_mult)
            risk_usdt = balance * config.RISK_PER_TRADE * vol_mult
            if is_long:
                await notify(msg_trade_open(state.symbol, price, qty, sl, tp, atr or 0, risk_usdt, vol_mult))
            else:
                await notify(msg_trade_open_short(state.symbol, price, qty, sl, tp, atr or 0, risk_usdt, vol_mult))

            await self._place_protective_orders(state, qty, sl, tp, is_long)

        except Exception as exc:
            log.error("[%s] Error al abrir %s: %s", state.symbol, side, exc)

    # ------------------------------------------------------------------ #
    # SL/TP en Futuros: dos órdenes reduceOnly+closePosition independientes
    # (Futures no tiene OCO nativo combinando STOP_MARKET + TAKE_PROFIT_MARKET,
    # así que se gestionan a mano igual que la OCO de Spot: si una llena, se
    # cancela la otra).                                                     #
    # ------------------------------------------------------------------ #

    async def _place_protective_orders(self, state: SymbolState, qty: float, sl: float, tp: float, is_long: bool) -> None:
        close_side = SIDE_SELL if is_long else SIDE_BUY
        try:
            sl_order = await self.client.futures_create_order(
                symbol=state.symbol, side=close_side,
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=str(sl), closePosition=True,
            )
            state.sl_order_id = str(sl_order["orderId"])
        except Exception as exc:
            log.error("[%s] Error al colocar SL — posición sin protección: %s", state.symbol, exc)

        try:
            tp_order = await self.client.futures_create_order(
                symbol=state.symbol, side=close_side,
                type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=str(tp), closePosition=True,
            )
            state.tp_order_id = str(tp_order["orderId"])
        except Exception as exc:
            log.warning("[%s] Error al colocar TP (techo de seguridad no activo): %s", state.symbol, exc)

    async def _cancel_protective_orders(self, state: SymbolState) -> None:
        for attr in ("sl_order_id", "tp_order_id"):
            order_id = getattr(state, attr)
            if not order_id:
                continue
            try:
                await self.client.futures_cancel_order(symbol=state.symbol, orderId=int(order_id))
            except Exception as exc:
                log.warning("[%s] Error al cancelar orden %s (¿ya ejecutada?): %s", state.symbol, order_id, exc)
            setattr(state, attr, None)

    async def _sync_orders(self, state: SymbolState) -> None:
        """Detecta si el SL o el TP ejecutaron en Binance y cancela el otro lado."""
        if not state.in_position:
            return
        for attr, other_attr, label, msg_fn in (
            ("sl_order_id", "tp_order_id", "SL", msg_trade_close_sl),
            ("tp_order_id", "sl_order_id", "TP", msg_trade_close_tp),
        ):
            order_id = getattr(state, attr)
            if not order_id:
                continue
            try:
                order = await self.client.futures_get_order(symbol=state.symbol, orderId=int(order_id))
            except Exception as exc:
                log.warning("[%s] Error al verificar orden %s: %s", state.symbol, order_id, exc)
                continue
            if order["status"] == "FILLED":
                fill_price = float(order.get("avgPrice") or order.get("stopPrice", 0))
                log.info("[%s] %s ejecutado @ %.2f — posición cerrada.", state.symbol, label, fill_price)
                await notify(msg_fn(state.symbol, state.entry_price, fill_price,
                                     float(order["executedQty"]), state.side or "LONG"))
                self.trade_logger.log_trade({
                    "action": f"CLOSED_BY_{label}", "side": state.side, "symbol": state.symbol,
                    "price": fill_price, "quantity": float(order["executedQty"]), "order_id": order_id,
                })
                other_id = getattr(state, other_attr)
                if other_id:
                    try:
                        await self.client.futures_cancel_order(symbol=state.symbol, orderId=int(other_id))
                    except Exception as exc:
                        log.warning("[%s] Error cancelando orden remanente %s: %s", state.symbol, other_id, exc)
                is_long = state.side == "LONG"
                pnl = ((fill_price - state.entry_price) if is_long else (state.entry_price - fill_price)) * float(order["executedQty"])
                self._record_trade_result(pnl)
                self._reset_position(state)
                return

    def _reset_position(self, state: SymbolState) -> None:
        state.in_position    = False
        state.side           = None
        state.sl_order_id    = None
        state.tp_order_id    = None
        state.scale_out_done = False
        state.current_qty    = 0.0
        state.entry_price    = 0.0
        state.entry_atr      = 0.0
        state.current_sl     = 0.0

    # ------------------------------------------------------------------ #
    # Trailing stop adaptativo (ADX)                                       #
    # ------------------------------------------------------------------ #

    async def _update_trailing_stop(self, state: SymbolState, close: float) -> None:
        if not state.sl_order_id or not state.strategy.current_atr:
            return
        is_long = state.side == "LONG"
        trail_mult = self.risk_manager.get_trailing_multiplier(state.strategy.current_adx)
        if is_long:
            new_sl = round(close - state.strategy.current_atr * trail_mult, 2)
            improved = new_sl > state.current_sl * (1 + config.TRAILING_STOP_MIN_MOVE)
        else:
            new_sl = round(close + state.strategy.current_atr * trail_mult, 2)
            improved = new_sl < state.current_sl * (1 - config.TRAILING_STOP_MIN_MOVE)
        if not improved:
            return

        try:
            await self.client.futures_cancel_order(symbol=state.symbol, orderId=int(state.sl_order_id))
        except Exception as exc:
            log.warning("[%s] Error al cancelar SL para trailing: %s", state.symbol, exc)
            return
        log.info("[%s] Trailing stop movido: %.2f → %.2f", state.symbol, state.current_sl, new_sl)
        state.current_sl = new_sl
        close_side = SIDE_SELL if is_long else SIDE_BUY
        try:
            sl_order = await self.client.futures_create_order(
                symbol=state.symbol, side=close_side,
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=str(new_sl), closePosition=True,
            )
            state.sl_order_id = str(sl_order["orderId"])
        except Exception as exc:
            log.error("[%s] Error al recolocar SL tras trailing — posición sin SL: %s", state.symbol, exc)
            state.sl_order_id = None

    # ------------------------------------------------------------------ #
    # Scale-out: toma parcial de beneficios                               #
    # ------------------------------------------------------------------ #

    async def _check_scale_out(self, state: SymbolState, close: float) -> None:
        if (not state.in_position or state.scale_out_done
                or config.SCALE_OUT_R <= 0 or not state.entry_atr):
            return
        is_long = state.side == "LONG"
        trail_mult = self.risk_manager.get_trailing_multiplier(state.strategy.current_adx)
        offset = config.SCALE_OUT_R * trail_mult * state.entry_atr
        so_trigger = state.entry_price + offset if is_long else state.entry_price - offset
        triggered = (close >= so_trigger) if is_long else (close <= so_trigger)
        if not triggered:
            return

        partial_qty = round(state.current_qty * config.SCALE_OUT_RATIO, 5)
        if partial_qty < config.MIN_QUANTITY:
            state.scale_out_done = True
            return

        close_side = SIDE_SELL if is_long else SIDE_BUY
        try:
            order = await self.client.futures_create_order(
                symbol=state.symbol, side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET, quantity=partial_qty, reduceOnly=True,
            )
            state.scale_out_done = True
            remaining_qty = round(state.current_qty - partial_qty, 5)
            state.current_qty = remaining_qty

            log.info("[%s] SCALE-OUT %.0f%% | %s: %s | Precio: %.2f | Trigger: %.2f | Restante: %s",
                      state.symbol, config.SCALE_OUT_RATIO * 100, close_side, partial_qty, close, so_trigger, remaining_qty)
            pnl_so = (close - state.entry_price) * partial_qty if is_long else (state.entry_price - close) * partial_qty
            await notify(msg_scale_out(state.symbol, close, partial_qty, pnl_so))
            self.trade_logger.log_trade({
                "action": "SCALE_OUT", "side": state.side, "symbol": state.symbol,
                "price": close, "quantity": partial_qty, "order_id": order["orderId"],
            })

            # Mover el SL a break-even — el TP con closePosition=True sigue válido sin cambios.
            if state.sl_order_id:
                try:
                    await self.client.futures_cancel_order(symbol=state.symbol, orderId=int(state.sl_order_id))
                except Exception as exc:
                    log.warning("[%s] Error cancelando SL para mover a break-even: %s", state.symbol, exc)
                try:
                    sl_order = await self.client.futures_create_order(
                        symbol=state.symbol, side=close_side,
                        type=FUTURE_ORDER_TYPE_STOP_MARKET,
                        stopPrice=str(state.entry_price), closePosition=True,
                    )
                    state.sl_order_id = str(sl_order["orderId"])
                    state.current_sl  = state.entry_price
                    log.info("[%s] SL movido a break-even: %.2f", state.symbol, state.entry_price)
                except Exception as exc:
                    log.error("[%s] Error recolocando SL en break-even: %s", state.symbol, exc)
                    state.sl_order_id = None

        except Exception as exc:
            log.error("[%s] Error en scale-out: %s", state.symbol, exc)

    # ------------------------------------------------------------------ #
    # Cierre de posición por señal contraria                              #
    # ------------------------------------------------------------------ #

    async def _close_position(self, state: SymbolState, price: float) -> None:
        if not state.in_position:
            return
        await self._cancel_protective_orders(state)

        is_long = state.side == "LONG"
        close_side = SIDE_SELL if is_long else SIDE_BUY
        qty = state.current_qty

        try:
            order = await self.client.futures_create_order(
                symbol=state.symbol, side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET, quantity=qty, reduceOnly=True,
            )
            side_label = state.side
            entry_price = state.entry_price
            self.trade_logger.log_trade({
                "action": "CLOSE", "side": side_label, "symbol": state.symbol,
                "price": price, "quantity": qty, "order_id": order["orderId"],
            })
            log.info("[%s] Cierre %s ejecutado | Qty: %s | Precio: %.2f", state.symbol, side_label, qty, price)
            await notify(msg_trade_close_signal(state.symbol, entry_price, price, qty, side_label))
            pnl = ((price - entry_price) if is_long else (entry_price - price)) * qty
            self._record_trade_result(pnl)
            self._reset_position(state)

        except Exception as exc:
            log.error("[%s] Error al cerrar posición: %s", state.symbol, exc)

    # ------------------------------------------------------------------ #
    # Balance de la cuenta de Futuros                                      #
    # ------------------------------------------------------------------ #

    async def _get_funding_rate(self, symbol: str) -> float | None:
        """Funding rate actual (premiumIndex) que se aplicará en el próximo periodo de 8h.
        Positivo → LONG paga a SHORT; negativo → SHORT paga a LONG. None si falla la consulta
        (no bloquea la entrada: más vale operar sin el filtro que no operar por un error de red)."""
        try:
            mark = await self.client.futures_mark_price(symbol=symbol)
            return float(mark["lastFundingRate"])
        except Exception as exc:
            log.warning("[%s] No se pudo obtener funding rate: %s", symbol, exc)
            return None

    def _funding_blocks_entry(self, side: str, funding_rate: float | None) -> bool:
        """True si el funding actual es desfavorable para el lado que se quiere abrir."""
        if not config.FUNDING_FILTER_ENABLED or funding_rate is None:
            return False
        if side == "LONG":
            return funding_rate > config.FUNDING_RATE_MAX_FOR_LONG
        return funding_rate < config.FUNDING_RATE_MIN_FOR_SHORT

    async def _get_usdt_balance(self) -> float:
        balances = await self.client.futures_account_balance()
        for asset in balances:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0


# ------------------------------------------------------------------ #
# Punto de entrada                                                    #
# ------------------------------------------------------------------ #

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


async def main() -> None:
    setup_logging()
    bot = TradingBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("Bot detenido por el usuario.")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
