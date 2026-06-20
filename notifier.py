"""
Notificaciones Telegram para el bot de trading.

Usa la API HTTP de Telegram directamente (sin dependencias extra).
Si el token no está configurado, las llamadas se ignoran silenciosamente
para que el bot nunca falle por un problema de conectividad con Telegram.
"""

import logging
import aiohttp
from config import config

log = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def notify(text: str) -> None:
    """Envía un mensaje HTML al chat de Telegram configurado."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    url = _BASE_URL.format(token=config.TELEGRAM_TOKEN)
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                url,
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as exc:
        log.warning("Telegram notify error (ignorado): %s", exc)


# ── Mensajes predefinidos ─────────────────────────────────────────────

def msg_bot_started(symbols, interval: str, balance: float, mode: str) -> str:
    pares = ", ".join(symbols) if isinstance(symbols, (list, tuple)) else symbols
    return (
        f"🤖 <b>Bot iniciado [Futures]</b>\n"
        f"Pares: <code>{pares}</code>  |  TF: <code>{interval}</code>\n"
        f"Balance: <code>{balance:,.2f} USDT</code>\n"
        f"Modo: <b>{mode}</b>"
    )


def msg_trade_open(
    symbol: str, price: float, qty: float,
    sl: float, tp: float, atr: float,
    risk_usdt: float, vol_mult: float,
) -> str:
    tp_line = f"TP: <code>{tp:,.2f}</code>\n" if tp > 0 else ""
    return (
        f"🟢 <b>COMPRA</b> — {symbol}\n"
        f"Precio entrada: <code>{price:,.2f} USDT</code>\n"
        f"Cantidad: <code>{qty}</code>\n"
        f"SL: <code>{sl:,.2f}</code>\n"
        f"{tp_line}"
        f"ATR: <code>{atr:.2f}</code>  |  VolMult: <code>{vol_mult:.2f}</code>\n"
        f"Riesgo: <code>~{risk_usdt:.2f} USDT</code>"
    )


def msg_trade_open_short(
    symbol: str, price: float, qty: float,
    sl: float, tp: float, atr: float,
    risk_usdt: float, vol_mult: float,
) -> str:
    return (
        f"🔴 <b>SHORT</b> — {symbol}\n"
        f"Precio entrada: <code>{price:,.2f} USDT</code>\n"
        f"Cantidad: <code>{qty}</code>\n"
        f"SL: <code>{sl:,.2f}</code>\n"
        f"TP: <code>{tp:,.2f}</code>\n"
        f"ATR: <code>{atr:.2f}</code>  |  VolMult: <code>{vol_mult:.2f}</code>\n"
        f"Riesgo: <code>~{risk_usdt:.2f} USDT</code>"
    )


def msg_trade_close_signal(symbol: str, entry: float, exit_price: float, qty: float, side: str = "LONG") -> str:
    pnl = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
    emoji = "✅" if pnl >= 0 else "🔻"
    return (
        f"{emoji} <b>CIERRE {side} (señal EMA)</b> — {symbol}\n"
        f"Entrada: <code>{entry:,.2f}</code>  →  Salida: <code>{exit_price:,.2f}</code>\n"
        f"PnL: <code>{pnl:+.2f} USDT</code>"
    )


def msg_trade_close_sl(symbol: str, entry: float, fill_price: float, qty: float, side: str = "LONG") -> str:
    pnl = (fill_price - entry) * qty if side == "LONG" else (entry - fill_price) * qty
    emoji = "✅" if pnl >= 0 else "🔴"
    return (
        f"{emoji} <b>STOP-LOSS {side} ejecutado</b> — {symbol}\n"
        f"Entrada: <code>{entry:,.2f}</code>  →  SL: <code>{fill_price:,.2f}</code>\n"
        f"PnL: <code>{pnl:+.2f} USDT</code>"
    )


def msg_trade_close_tp(symbol: str, entry: float, fill_price: float, qty: float, side: str = "LONG") -> str:
    pnl = (fill_price - entry) * qty if side == "LONG" else (entry - fill_price) * qty
    return (
        f"✅ <b>TAKE-PROFIT {side} ejecutado</b> — {symbol}\n"
        f"Entrada: <code>{entry:,.2f}</code>  →  TP: <code>{fill_price:,.2f}</code>\n"
        f"PnL: <code>{pnl:+.2f} USDT</code>"
    )


def msg_trade_close_oco(symbol: str, entry: float) -> str:
    return (
        f"📋 <b>OCO ejecutada</b> — {symbol}\n"
        f"Posición cerrada por SL o TP\n"
        f"Entrada: <code>{entry:,.2f}</code>"
    )


def msg_scale_out(symbol: str, price: float, partial_qty: float, pnl: float) -> str:
    return (
        f"🟡 <b>SCALE-OUT 50%</b> — {symbol}\n"
        f"Precio: <code>{price:,.2f}</code>\n"
        f"Vendida: <code>{partial_qty}</code>\n"
        f"PnL parcial: <code>{pnl:+.2f} USDT</code>\n"
        f"Stop movido a break-even"
    )


def msg_circuit_breaker(balance: float, period_start: float, period: str = "mensual") -> str:
    dd = (period_start - balance) / period_start * 100
    limite = "8%" if period == "mensual" else "4%"
    siguiente = "próximo mes" if period == "mensual" else "próximo día UTC"
    return (
        f"⛔ <b>CIRCUIT BREAKER ({period})</b>\n"
        f"Drawdown {period}: <code>{dd:.1f}%</code> (límite {limite})\n"
        f"Balance: <code>{balance:,.2f} USDT</code>\n"
        f"Operaciones suspendidas hasta el {siguiente}"
    )


def msg_cooldown(consecutive_losses: int, hours: float) -> str:
    return (
        f"🧊 <b>COOLDOWN activado</b>\n"
        f"{consecutive_losses} pérdidas consecutivas\n"
        f"Sin nuevas entradas durante <code>{hours:.0f}h</code>"
    )
