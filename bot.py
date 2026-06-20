import asyncio
import logging
import sys
from datetime import datetime, timezone
from binance import AsyncClient, BinanceSocketManager
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_STOP_LOSS_LIMIT,
    TIME_IN_FORCE_GTC,
)

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager
from logger import TradeLogger
from notifier import (
    notify,
    msg_bot_started, msg_trade_open, msg_trade_close_signal,
    msg_trade_close_sl, msg_trade_close_oco, msg_scale_out,
    msg_circuit_breaker,
)

log = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.client: AsyncClient | None = None
        self.strategy = EMAStrategy(
            config.EMA_FAST, config.EMA_SLOW, config.ATR_PERIOD,
            rsi_min=config.RSI_BUY_MIN, rsi_max=config.RSI_BUY_MAX,
            adx_period=config.ADX_PERIOD, adx_min=config.ADX_MIN,
            atr_vol_period=config.ATR_VOL_PERIOD,
            atr_vol_min_ratio=config.ATR_VOL_MIN_RATIO,
            er_period=config.REGIME_ER_PERIOD,
            er_min=config.REGIME_ER_MIN,
        )
        self.htf_strategy = EMAStrategy(config.EMA_FAST, config.EMA_SLOW)
        self.risk_manager = RiskManager()
        self.trade_logger = TradeLogger()
        self.in_position: bool = False
        self.open_order: dict | None = None
        # Modo fijo (TRAILING_STOP=False): OCO con SL+TP simultáneos
        self.oco_list_id: int | None = None
        # Modo trailing (TRAILING_STOP=True): stop-limit que se mueve con el precio
        self.sl_order_id: str | None = None
        self.current_sl: float = 0.0
        self._last_htf_open_time: int = 0
        self.month_start_balance: float = 0.0
        self._current_month: int = 0
        # Scale-out: toma parcial al alcanzar SCALE_OUT_R × riesgo inicial
        self.entry_price_open: float = 0.0   # precio de entrada para calcular trigger
        self.entry_atr_open:   float = 0.0   # ATR al entrar para calcular trigger
        self.scale_out_done:   bool  = False  # True tras ejecutar la salida parcial
        self.current_qty:      float = 0.0   # cantidad restante (ajustada tras scale-out)

    # ------------------------------------------------------------------ #
    # Ciclo principal                                                      #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self.client = await AsyncClient.create(
            config.API_KEY,
            config.API_SECRET,
            testnet=config.TESTNET,
        )
        mode = "TESTNET" if config.TESTNET else "LIVE"
        log.info("Conectado a Binance [%s] | Par: %s | Intervalo: %s", mode, config.SYMBOL, config.INTERVAL)

        await self._warmup()
        # Fijar balance base del mes al arranque
        initial_balance = await self._get_usdt_balance()
        self.month_start_balance = initial_balance
        self._current_month = datetime.now(timezone.utc).month
        log.info("Balance base mensual: %.2f USDT (mes %d)", self.month_start_balance, self._current_month)
        await asyncio.gather(
            self._run_socket(),
            self._refresh_htf_loop(),
        )

    async def stop(self) -> None:
        if self.client:
            await self.client.close_connection()
            log.info("Conexión cerrada.")

    # ------------------------------------------------------------------ #
    # Calentamiento: carga velas históricas                               #
    # ------------------------------------------------------------------ #

    async def _warmup(self) -> None:
        klines = await self.client.get_klines(
            symbol=config.SYMBOL,
            interval=config.INTERVAL,
            limit=config.LOOKBACK_CANDLES,
        )
        for k in klines[:-1]:
            self.strategy.update(float(k[4]), high=float(k[2]), low=float(k[3]), volume=float(k[5]), closed=True)

        log.info(
            "Warmup 1H: %d velas | EMA%d=%.2f | EMA%d=%.2f | ATR=%.2f",
            len(klines) - 1,
            config.EMA_FAST, self.strategy.ema_fast or 0,
            config.EMA_SLOW, self.strategy.ema_slow or 0,
            self.strategy.current_atr or 0,
        )

        htf_klines = await self.client.get_klines(
            symbol=config.SYMBOL,
            interval=config.HTF_INTERVAL,
            limit=config.HTF_LOOKBACK_CANDLES,
        )
        for k in htf_klines[:-1]:
            self.htf_strategy.update(float(k[4]), closed=True)

        # Guardar timestamp para que _refresh_htf_loop no reprocese estas velas
        if len(htf_klines) >= 2:
            self._last_htf_open_time = int(htf_klines[-2][0])

        htf_trend = "alcista" if self.htf_strategy.is_bullish else "bajista"
        log.info(
            "Warmup HTF [%s]: %d velas | Tendencia: %s | EMA%d=%.2f | EMA%d=%.2f",
            config.HTF_INTERVAL, len(htf_klines) - 1, htf_trend,
            config.EMA_FAST, self.htf_strategy.ema_fast or 0,
            config.EMA_SLOW, self.htf_strategy.ema_slow or 0,
        )
        mode = "TESTNET" if config.TESTNET else "LIVE"
        balance = await self._get_usdt_balance()
        await notify(msg_bot_started(config.SYMBOL, config.INTERVAL, balance, mode))

    # ------------------------------------------------------------------ #
    # Refresco periódico del filtro HTF (cada intervalo principal)        #
    # ------------------------------------------------------------------ #

    async def _refresh_htf_loop(self) -> None:
        _INTERVAL_SECONDS = {"1h": 3600, "4h": 14400, "1d": 86400}
        sleep_secs = _INTERVAL_SECONDS.get(config.INTERVAL, 3600)
        while True:
            await asyncio.sleep(sleep_secs)
            try:
                klines = await self.client.get_klines(
                    symbol=config.SYMBOL,
                    interval=config.HTF_INTERVAL,
                    limit=10,
                )
                new_count = 0
                for k in klines[:-1]:  # excluir vela actual no cerrada
                    if int(k[0]) > self._last_htf_open_time:
                        self.htf_strategy.update(float(k[4]), closed=True)
                        new_count += 1

                if new_count and len(klines) >= 2:
                    self._last_htf_open_time = int(klines[-2][0])

                htf_trend = "alcista" if self.htf_strategy.is_bullish else "bajista"
                log.info("HTF [%s] actualizado (%d nuevas velas): %s", config.HTF_INTERVAL, new_count, htf_trend)
            except Exception as exc:
                log.warning("Error al refrescar HTF: %s", exc)

    # ------------------------------------------------------------------ #
    # WebSocket con reconexión automática                                 #
    # ------------------------------------------------------------------ #

    async def _run_socket(self) -> None:
        backoff = 5
        while True:
            try:
                bm = BinanceSocketManager(self.client)
                async with bm.kline_socket(symbol=config.SYMBOL, interval=config.INTERVAL) as stream:
                    log.info("WebSocket activo. Esperando señales...")
                    backoff = 5
                    while True:
                        msg = await stream.recv()
                        await self._process_message(msg)
            except Exception as exc:
                log.warning("WebSocket desconectado: %s — reconectando en %ds...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _process_message(self, msg: dict) -> None:
        if msg.get("e") == "error":
            log.error("Error WebSocket: %s", msg)
            return

        kline = msg["k"]
        close = float(kline["c"])
        high = float(kline["h"])
        low = float(kline["l"])
        volume = float(kline["v"])
        is_closed = kline["x"]

        self.strategy.update(close, high=high, low=low, volume=volume, closed=is_closed)

        if not is_closed:
            return

        # Verificar estado real en Binance y actualizar trailing stop si aplica
        if config.TRAILING_STOP:
            await self._sync_stop_order()
            if self.in_position:
                await self._check_scale_out(close)   # toma parcial antes de trailing
                await self._update_trailing_stop(close)
        else:
            await self._sync_position_with_oco()

        signal = self.strategy.get_signal()
        htf_trend = "alcista" if self.htf_strategy.is_bullish else "bajista"
        log.info(
            "Vela cerrada | Precio: %.2f | EMA%d: %.2f | EMA%d: %.2f | ATR: %.2f | RSI: %.1f | ADX: %.1f | ER: %.2f | HTF: %s | Señal: %s",
            close,
            config.EMA_FAST, self.strategy.ema_fast or 0,
            config.EMA_SLOW, self.strategy.ema_slow or 0,
            self.strategy.current_atr or 0,
            self.strategy.current_rsi or 0,
            self.strategy.current_adx or 0,
            self.strategy.current_er or 0,
            htf_trend,
            signal,
        )

        if signal == "BUY" and not self.in_position:
            await self._open_position(close)
        elif signal == "SELL" and self.in_position:
            await self._close_position(close)

    # ------------------------------------------------------------------ #
    # Sincronización con estado real de Binance                          #
    # ------------------------------------------------------------------ #

    async def _sync_position_with_oco(self) -> None:
        """Detecta si el SL o TP ejecutaron en Binance y actualiza el estado local."""
        if not self.in_position or self.oco_list_id is None:
            return
        try:
            oco = await self.client.get_order_list(orderListId=self.oco_list_id)
            if oco["listOrderStatus"] in ("ALL_DONE", "REJECTED"):
                log.info(
                    "OCO %d finalizado (%s) — posición cerrada por SL/TP.",
                    self.oco_list_id, oco["listOrderStatus"],
                )
                await notify(msg_trade_close_oco(config.SYMBOL, self.entry_price_open))
                self.trade_logger.log_trade({
                    "action": "CLOSED_BY_OCO",
                    "symbol": config.SYMBOL,
                    "oco_list_id": oco["orderListId"],
                    "status": oco["listOrderStatus"],
                })
                self.in_position = False
                self.open_order = None
                self.oco_list_id = None
        except Exception as exc:
            log.warning("Error al verificar OCO %d: %s", self.oco_list_id, exc)

    # ------------------------------------------------------------------ #
    # Gestión mensual                                                     #
    # ------------------------------------------------------------------ #

    async def _refresh_month_balance(self, current_balance: float) -> None:
        """Resetea el balance base al inicio de cada mes nuevo."""
        now_month = datetime.now(timezone.utc).month
        if now_month != self._current_month:
            self._current_month = now_month
            self.month_start_balance = current_balance
            log.info("Nuevo mes — balance base mensual actualizado: %.2f USDT", current_balance)

    # ------------------------------------------------------------------ #
    # Gestión de órdenes                                                  #
    # ------------------------------------------------------------------ #

    async def _open_position(self, price: float) -> None:
        if not self.htf_strategy.is_bullish and not config.ALLOW_BUY_IN_BEARISH_HTF:
            log.info("HTF [%s] bajista — BUY omitido (señal en contratendencia)", config.HTF_INTERVAL)
            return
        if not self.htf_strategy.is_bullish and config.ALLOW_BUY_IN_BEARISH_HTF:
            log.info("HTF [%s] bajista — BUY en contratendencia permitido", config.HTF_INTERVAL)

        if not self.strategy.can_enter_long:
            log.info(
                "Filtros no superados — BUY omitido | RSI: %.1f | ADX: %.1f",
                self.strategy.current_rsi or 0,
                self.strategy.current_adx or 0,
            )
            return

        balance = await self._get_usdt_balance()
        await self._refresh_month_balance(balance)

        if self.risk_manager.is_monthly_drawdown_exceeded(balance, self.month_start_balance):
            log.warning(
                "CIRCUIT BREAKER: drawdown mensual ≥8%% (balance %.2f / inicio mes %.2f) — operaciones suspendidas hasta el próximo mes",
                balance, self.month_start_balance,
            )
            await notify(msg_circuit_breaker(balance, self.month_start_balance))
            return

        atr = self.strategy.current_atr
        vol_mult = self.strategy.vol_multiplier
        qty = self.risk_manager.calculate_position_size(balance, price, atr, vol_mult)

        if qty <= 0:
            log.warning("Tamaño de posición insuficiente (balance: %.2f USDT). Operación omitida.", balance)
            return

        try:
            order = await self.client.create_order(
                symbol=config.SYMBOL,
                side=SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            self.in_position      = True
            self.open_order       = order
            self.entry_price_open = price
            self.entry_atr_open   = atr or 0.0
            self.scale_out_done   = False
            self.current_qty      = qty

            sl = self.risk_manager.get_stop_loss(price, "BUY", atr)
            tp = self.risk_manager.get_take_profit(price, "BUY", atr)

            self.trade_logger.log_trade({
                "action": "OPEN",
                "side": "BUY",
                "symbol": config.SYMBOL,
                "price": price,
                "quantity": qty,
                "stop_loss": sl,
                "take_profit": tp,
                "order_id": order["orderId"],
                "atr": round(atr, 2) if atr else None,
            })
            log.info("COMPRA ejecutada | Qty: %s | Precio: %.2f | SL: %.2f | TP: %.2f | ATR: %.2f | VolMult: %.2f",
                     qty, price, sl, tp, atr or 0, vol_mult)
            risk_usdt = balance * config.RISK_PER_TRADE * vol_mult
            await notify(msg_trade_open(config.SYMBOL, price, qty, sl, tp, atr or 0, risk_usdt, vol_mult))

            if config.TRAILING_STOP:
                await self._place_stop_limit(qty, sl)
            else:
                await self._place_oco_order(qty, sl, tp)

        except Exception as exc:
            log.error("Error al abrir posición: %s", exc)

    # ------------------------------------------------------------------ #
    # Trailing stop: stop-limit que se actualiza con el precio           #
    # ------------------------------------------------------------------ #

    async def _place_stop_limit(self, qty: float, sl: float) -> None:
        """Coloca una orden STOP-LOSS-LIMIT en Binance para el trailing stop."""
        limit_price = round(sl * 0.998, 2)
        try:
            order = await self.client.create_order(
                symbol=config.SYMBOL,
                side=SIDE_SELL,
                type=ORDER_TYPE_STOP_LOSS_LIMIT,
                timeInForce=TIME_IN_FORCE_GTC,
                quantity=qty,
                stopPrice=str(sl),
                price=str(limit_price),
            )
            self.sl_order_id = str(order["orderId"])
            self.current_sl = sl
            log.info("Stop-limit | OrderId: %s | SL: %.2f | Limit: %.2f",
                     self.sl_order_id, sl, limit_price)
        except Exception as exc:
            log.error("Error al colocar stop-limit — posición sin protección: %s", exc)

    async def _update_trailing_stop(self, close: float) -> None:
        """Sube el stop-limit si el precio subió suficiente (≥ TRAILING_STOP_MIN_MOVE)."""
        if not self.sl_order_id or not self.strategy.current_atr:
            return
        new_sl = round(close - self.strategy.current_atr * config.ATR_SL_MULTIPLIER, 2)
        if new_sl <= self.current_sl * (1 + config.TRAILING_STOP_MIN_MOVE):
            return
        try:
            await self.client.cancel_order(
                symbol=config.SYMBOL, orderId=int(self.sl_order_id)
            )
            log.info("Trailing stop subido: %.2f → %.2f", self.current_sl, new_sl)
            self.sl_order_id = None
        except Exception as exc:
            log.warning("Error al cancelar stop para trailing: %s", exc)
            return
        qty = self.current_qty if self.current_qty > 0 else float(self.open_order["executedQty"])
        await self._place_stop_limit(qty, new_sl)

    async def _check_scale_out(self, close: float) -> None:
        """Vende SCALE_OUT_RATIO de la posición cuando el precio alcanza SCALE_OUT_R × riesgo inicial
        y mueve el stop-limit a break-even (precio de entrada)."""
        if (not self.in_position or self.scale_out_done
                or config.SCALE_OUT_R <= 0 or not self.entry_atr_open):
            return
        so_trigger = (self.entry_price_open
                      + config.SCALE_OUT_R * config.ATR_SL_MULTIPLIER * self.entry_atr_open)
        if close < so_trigger:
            return

        partial_qty = round(self.current_qty * config.SCALE_OUT_RATIO, 5)
        if partial_qty < config.MIN_QUANTITY:
            self.scale_out_done = True   # posición demasiado pequeña, omitir
            return

        try:
            order = await self.client.create_order(
                symbol=config.SYMBOL,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=partial_qty,
            )
            self.scale_out_done = True
            remaining_qty = round(self.current_qty - partial_qty, 5)
            self.current_qty = remaining_qty

            log.info(
                "SCALE-OUT %.0f%% | Vendida: %s BTC | Precio: %.2f | Trigger: %.2f | Restante: %s BTC",
                config.SCALE_OUT_RATIO * 100, partial_qty, close, so_trigger, remaining_qty,
            )
            pnl_so = (close - self.entry_price_open) * partial_qty
            await notify(msg_scale_out(config.SYMBOL, close, partial_qty, pnl_so))
            self.trade_logger.log_trade({
                "action": "SCALE_OUT",
                "side": "SELL",
                "symbol": config.SYMBOL,
                "price": close,
                "quantity": partial_qty,
                "order_id": order["orderId"],
            })

            # Mover stop-limit a break-even para proteger el 50% restante
            if config.TRAILING_STOP and self.sl_order_id:
                try:
                    await self.client.cancel_order(
                        symbol=config.SYMBOL, orderId=int(self.sl_order_id)
                    )
                    self.sl_order_id = None
                except Exception as exc:
                    log.warning("Error cancelando stop para mover a break-even: %s", exc)
                if remaining_qty >= config.MIN_QUANTITY:
                    await self._place_stop_limit(remaining_qty, self.entry_price_open)
                    log.info("Stop movido a break-even: %.2f", self.entry_price_open)

        except Exception as exc:
            log.error("Error en scale-out: %s", exc)

    async def _sync_stop_order(self) -> None:
        """Detecta si el stop-limit ejecutó en Binance y actualiza el estado local."""
        if not self.in_position or not self.sl_order_id:
            return
        try:
            order = await self.client.get_order(
                symbol=config.SYMBOL, orderId=int(self.sl_order_id)
            )
            if order["status"] == "FILLED":
                fill_price = float(order.get("avgPrice") or order.get("price", 0))
                log.info("Stop-limit ejecutado @ %.2f — posición cerrada por SL.", fill_price)
                await notify(msg_trade_close_sl(config.SYMBOL, self.entry_price_open, fill_price,
                                               float(order["executedQty"])))
                self.trade_logger.log_trade({
                    "action": "CLOSED_BY_STOP",
                    "side": "SELL",
                    "symbol": config.SYMBOL,
                    "price": fill_price,
                    "quantity": float(order["executedQty"]),
                    "order_id": self.sl_order_id,
                })
                self.in_position      = False
                self.open_order       = None
                self.sl_order_id      = None
                self.scale_out_done   = False
                self.current_qty      = 0.0
                self.entry_price_open = 0.0
                self.entry_atr_open   = 0.0
        except Exception as exc:
            log.warning("Error al verificar stop-limit: %s", exc)

    async def _cancel_stop_limit(self) -> None:
        """Cancela el stop-limit activo antes de un cierre manual por señal."""
        if not self.sl_order_id:
            return
        try:
            await self.client.cancel_order(
                symbol=config.SYMBOL, orderId=int(self.sl_order_id)
            )
            log.info("Stop-limit %s cancelado (cierre por señal EMA).", self.sl_order_id)
        except Exception as exc:
            log.warning("Error al cancelar stop-limit %s: %s", self.sl_order_id, exc)
        finally:
            self.sl_order_id = None

    # ------------------------------------------------------------------ #
    # OCO fijo (TRAILING_STOP=False)                                     #
    # ------------------------------------------------------------------ #

    async def _place_oco_order(self, qty: float, sl: float, tp: float) -> None:
        """Coloca una orden OCO (TP limit + SL stop-limit) en Binance."""
        # stopLimitPrice ligeramente inferior al trigger para asegurar ejecución en caídas rápidas
        stop_limit_price = round(sl * 0.998, 2)
        try:
            oco = await self.client.create_oco_order(
                symbol=config.SYMBOL,
                side=SIDE_SELL,
                quantity=qty,
                price=str(tp),
                stopPrice=str(sl),
                stopLimitPrice=str(stop_limit_price),
                stopLimitTimeInForce="GTC",
            )
            self.oco_list_id = oco["orderListId"]
            log.info(
                "OCO colocada | ListId: %d | TP: %.2f | SL: %.2f | SL-Limit: %.2f",
                self.oco_list_id, tp, sl, stop_limit_price,
            )
        except Exception as exc:
            log.error("Error al colocar OCO — SL/TP NO activos en Binance: %s", exc)

    async def _cancel_oco(self) -> bool:
        """Cancela la OCO activa. Devuelve False si falla (posible ejecución previa)."""
        if self.oco_list_id is None:
            return True
        try:
            await self.client.cancel_order_list(
                symbol=config.SYMBOL,
                orderListId=self.oco_list_id,
            )
            log.info("OCO %d cancelada (cierre manual por señal EMA).", self.oco_list_id)
            self.oco_list_id = None
            return True
        except Exception as exc:
            log.warning("Error al cancelar OCO %d (¿ya ejecutada?): %s", self.oco_list_id, exc)
            self.oco_list_id = None
            return False

    async def _close_position(self, price: float) -> None:
        if not self.open_order:
            return

        if config.TRAILING_STOP:
            await self._cancel_stop_limit()
        else:
            cancel_ok = await self._cancel_oco()
            if not cancel_ok:
                await self._sync_position_with_oco()
                if not self.in_position:
                    return

        qty = self.current_qty if self.current_qty > 0 else float(self.open_order["executedQty"])

        try:
            order = await self.client.create_order(
                symbol=config.SYMBOL,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            self.in_position      = False
            self.open_order       = None
            self.scale_out_done   = False
            self.current_qty      = 0.0
            self.entry_price_open = 0.0
            self.entry_atr_open   = 0.0

            self.trade_logger.log_trade({
                "action": "CLOSE",
                "side": "SELL",
                "symbol": config.SYMBOL,
                "price": price,
                "quantity": qty,
                "order_id": order["orderId"],
            })
            log.info("VENTA ejecutada | Qty: %s | Precio: %.2f", qty, price)
            await notify(msg_trade_close_signal(config.SYMBOL, self.entry_price_open, price, qty))

        except Exception as exc:
            log.error("Error al cerrar posición: %s", exc)

    async def _get_usdt_balance(self) -> float:
        account = await self.client.get_account()
        for asset in account["balances"]:
            if asset["asset"] == "USDT":
                return float(asset["free"])
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
