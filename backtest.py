"""
Backtest profesional — EMA Crossover 9/21 + filtro HTF 4H + gestión ATR.

Reutiliza exactamente las mismas clases del bot (EMAStrategy, RiskManager)
para que los resultados sean idénticos al comportamiento real.
Incluye slippage, comisiones y simulación intracandle de SL/TP.

Uso:
    python backtest.py                   # 24 meses, 1000 USDT
    python backtest.py --months 36       # 3 años
    python backtest.py --balance 500
    python backtest.py --months 12 --balance 2000 --csv trades.csv
"""

import asyncio
import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

# Forzar UTF-8 en la salida para que los caracteres del informe funcionen en Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Windows fix: aiodns falla con c-ares en Windows; forzar resolver nativo de Python
import aiohttp as _aiohttp
from aiohttp import ThreadedResolver as _ThreadedResolver
_orig_connector_init = _aiohttp.TCPConnector.__init__
def _patched_connector_init(self, *, resolver=None, **kwargs):
    if resolver is None:
        resolver = _ThreadedResolver()
    _orig_connector_init(self, resolver=resolver, **kwargs)
_aiohttp.TCPConnector.__init__ = _patched_connector_init

from binance import AsyncClient

from config import config
from strategy import EMAStrategy
from risk_manager import RiskManager

SLIPPAGE = 0.001   # 0.1 % base por orden de mercado (deslizamiento en volatilidad normal)
SLIPPAGE_CAP = 0.005  # techo de slippage en eventos de alta volatilidad (0.5 %)
FEE      = 0.001   # 0.1 % comisión Binance por operación (maker/taker)


def dynamic_slippage(current_atr: float | None, avg_atr: float | None) -> float:
    """Slippage proporcional a la volatilidad relativa: en ATR alto el libro de órdenes
    suele tener menos profundidad y el deslizamiento real es mayor que en mercado calmo."""
    if not current_atr or not avg_atr or avg_atr <= 0:
        return SLIPPAGE
    return min(SLIPPAGE_CAP, SLIPPAGE * max(1.0, current_atr / avg_atr))


# ──────────────────────────────────────────────────────────────────── #
# Estructuras de datos                                                  #
# ──────────────────────────────────────────────────────────────────── #

@dataclass
class Trade:
    entry_time:     str
    entry_price:    float
    qty:            float
    sl:             float
    tp:             float
    atr:            float
    exit_time:      str   = ""
    exit_price:     float = 0.0
    exit_reason:    str   = ""   # TP | SL | SIGNAL | FIN
    pnl_usdt:       float = 0.0
    pnl_pct:        float = 0.0
    initial_qty:    float = 0.0   # qty original al entrar (para pnl_pct correcto tras scale-out)
    scale_out_pnl:  float = 0.0   # PnL cobrado en la salida parcial
    scale_out_done: bool  = False  # True tras ejecutar el scale-out (evita doble ejecución)
    initial_risk:   float = 0.0   # |entry - sl inicial|, referencia fija para el break-even (1R)
    breakeven_done: bool  = False  # True tras mover el SL a break-even (evita recalcularlo cada vela)
    side:           str   = "LONG"  # LONG | SHORT (usado por backtest_futures.py)
    funding_cost:   float = 0.0   # costo acumulado de funding (solo backtest_futures.py)
    engine:         str   = "TREND"  # TREND | MR (usado por backtest_hybrid.py)


# ──────────────────────────────────────────────────────────────────── #
# Descarga de datos                                                     #
# ──────────────────────────────────────────────────────────────────── #

async def fetch_klines(interval: str, months: int) -> list:
    """Descarga klines históricos de Binance real (datos auténticos)."""
    # testnet=False → datos históricos reales, independientemente del modo del bot
    client = await AsyncClient.create(
        api_key=config.API_KEY or "",
        api_secret=config.API_SECRET or "",
        testnet=False,
    )
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000)
    all_klines, start = [], since_ms

    while True:
        batch = await client.get_klines(
            symbol=config.SYMBOL, interval=interval,
            startTime=start, limit=1000,
        )
        if not batch:
            break
        all_klines.extend(batch)
        if len(batch) < 1000:
            break
        start = int(batch[-1][0]) + 1

    await client.close_connection()
    return all_klines


# ──────────────────────────────────────────────────────────────────── #
# Motor de simulación                                                   #
# ──────────────────────────────────────────────────────────────────── #

def run_backtest(
    klines_1h: list,
    klines_4h: list,
    initial_balance: float,
) -> tuple[List[Trade], List[float], float]:

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
    htf_strategy = EMAStrategy(config.EMA_FAST, config.EMA_SLOW)  # HTF solo necesita is_bullish/is_bearish
    risk_manager = RiskManager()

    balance          = initial_balance
    balance_history  = [balance]
    trades: List[Trade]        = []
    current: Optional[Trade]   = None
    in_position                = False
    htf_ptr                    = 0
    candles_in_position        = 0
    candles_ready               = 0

    for k in klines_1h:
        open_time = int(k[0])
        close     = float(k[4])
        high      = float(k[2])
        low       = float(k[3])
        volume    = float(k[5])

        # ── Actualizar HTF con velas 4H que cerraron antes de esta vela 1H ──
        while htf_ptr < len(klines_4h):
            close_time_4h = int(klines_4h[htf_ptr][6])
            if close_time_4h < open_time:
                htf_strategy.update(float(klines_4h[htf_ptr][4]), closed=True)
                htf_ptr += 1
            else:
                break

        # ── Actualizar estrategia principal ──
        strategy.update(close, high=high, low=low, volume=volume, closed=True)

        if not strategy.is_ready:
            balance_history.append(balance)
            continue

        candles_ready += 1
        if in_position:
            candles_in_position += 1

        # ── Gestión intracandle: comprobar SL / TP ──
        if in_position and current:
            # ── Scale-out: tomar beneficios parciales al llegar a SCALE_OUT_R ──
            # Se comprueba ANTES del SL para capturar subidas antes de posibles retrocesos.
            if (config.SCALE_OUT_R > 0 and not current.scale_out_done
                    and current.atr > 0 and current.qty > 0):
                so_mult = risk_manager.get_trailing_multiplier(strategy.current_adx)
                so_price = (current.entry_price
                            + config.SCALE_OUT_R * so_mult * current.atr)
                if high >= so_price:
                    partial_qty = round(current.qty * config.SCALE_OUT_RATIO, 5)
                    if partial_qty >= config.MIN_QUANTITY:
                        fee_so = so_price * partial_qty * FEE
                        pnl_so = (so_price - current.entry_price) * partial_qty - fee_so
                        balance += pnl_so
                        current.scale_out_pnl = round(pnl_so, 4)
                        current.qty           = round(current.qty - partial_qty, 5)
                        current.sl            = current.entry_price  # stop a break-even
                        current.scale_out_done = True

            hit_sl = low <= current.sl
            # El TP fijo actúa como techo de seguridad incluso con trailing activo:
            # antes, con TRAILING_STOP=True, nunca se evaluaba y ATR_TP_MULTIPLIER era código muerto.
            hit_tp = high >= current.tp

            if hit_sl or hit_tp:
                if hit_sl and hit_tp:
                    exit_price, reason = current.sl, "SL"
                elif hit_tp:
                    exit_price, reason = current.tp, "TP"
                else:
                    exit_price, reason = current.sl, "SL"

                fee = exit_price * current.qty * FEE
                pnl = (exit_price - current.entry_price) * current.qty - fee
                balance += pnl
                _fill_trade(current, open_time, exit_price, reason, pnl)
                trades.append(current)
                in_position, current = False, None
                balance_history.append(balance)
                continue

            # ── Break-even: tras alcanzar BREAKEVEN_R × riesgo inicial, mover el SL a
            # entrada + buffer (cubre fees/slippage) para garantizar que el trade ya no
            # puede cerrar en pérdida. Se compara con el high de la vela, no el close,
            # para no perderse el toque intracandle. Solo sube el SL, nunca lo baja. ──
            if (config.BREAKEVEN_R > 0 and not current.breakeven_done
                    and current.initial_risk > 0):
                be_trigger = current.entry_price + config.BREAKEVEN_R * current.initial_risk
                if high >= be_trigger:
                    be_price = current.entry_price * (1 + config.BREAKEVEN_BUFFER)
                    if be_price > current.sl:
                        current.sl = round(be_price, 2)
                    current.breakeven_done = True

            # ── Trailing stop: elevar SL siguiendo el precio de cierre, con multiplicador
            # adaptado a la fuerza de tendencia actual (ADX) ──
            if config.TRAILING_STOP and strategy.current_atr:
                trail_mult = risk_manager.get_trailing_multiplier(strategy.current_adx)
                new_sl = round(close - strategy.current_atr * trail_mult, 2)
                if new_sl > current.sl:
                    current.sl = new_sl

        # ── Señales de cruce ──
        signal = strategy.get_signal()
        slip = dynamic_slippage(strategy.current_atr, strategy.avg_atr)

        if signal == "SELL" and in_position and current:
            exit_price = close * (1 - slip)
            fee = exit_price * current.qty * FEE
            pnl = (exit_price - current.entry_price) * current.qty - fee
            balance += pnl
            _fill_trade(current, open_time, exit_price, "SIGNAL", pnl)
            trades.append(current)
            in_position, current = False, None

        htf_ok = htf_strategy.is_bullish or config.ALLOW_BUY_IN_BEARISH_HTF
        if signal == "BUY" and not in_position and htf_ok and strategy.can_enter_long:
            atr   = strategy.current_atr
            entry = close * (1 + slip)
            qty   = risk_manager.calculate_position_size(
                balance, entry, atr, strategy.vol_multiplier, strategy.current_adx
            )

            if qty > 0:
                balance -= entry * qty * FEE   # comisión de entrada
                initial_sl = risk_manager.get_stop_loss(entry, "BUY", atr, strategy.current_adx)
                current = Trade(
                    entry_time  = _fmt(open_time),
                    entry_price = round(entry, 2),
                    qty         = qty,
                    initial_qty = qty,
                    sl          = initial_sl,
                    tp          = risk_manager.get_take_profit(entry, "BUY", atr),
                    atr         = round(atr, 2) if atr else 0,
                    initial_risk = round(entry - initial_sl, 2),
                )
                in_position = True

        balance_history.append(balance)

    # ── Cerrar posición abierta al final del periodo ──
    if in_position and current:
        last_close = float(klines_1h[-1][4]) * (1 - dynamic_slippage(strategy.current_atr, strategy.avg_atr))
        fee = last_close * current.qty * FEE
        pnl = (last_close - current.entry_price) * current.qty - fee
        balance += pnl
        _fill_trade(current, int(klines_1h[-1][0]), last_close, "FIN", pnl)
        trades.append(current)

    time_in_market_pct = (candles_in_position / candles_ready * 100) if candles_ready else 0.0
    return trades, balance_history, time_in_market_pct


def _fill_trade(t: Trade, open_time: int, exit_price: float, reason: str, pnl: float):
    t.exit_time   = _fmt(open_time)
    t.exit_price  = round(exit_price, 2)
    t.exit_reason = reason
    total_pnl     = t.scale_out_pnl + pnl   # incluye PnL parcial del scale-out si ocurrió
    t.pnl_usdt    = round(total_pnl, 4)
    ref_qty       = t.initial_qty if t.initial_qty > 0 else t.qty
    initial_val   = t.entry_price * ref_qty
    t.pnl_pct     = round(total_pnl / initial_val * 100, 2) if initial_val else 0.0


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ──────────────────────────────────────────────────────────────────── #
# Cálculo de métricas                                                   #
# ──────────────────────────────────────────────────────────────────── #

def compute_metrics(
    trades: List[Trade], balance_history: list, initial_balance: float, months: int,
    time_in_market_pct: float = 0.0,
) -> dict:
    if not trades:
        return {}

    winners = [t for t in trades if t.pnl_usdt > 0]
    losers  = [t for t in trades if t.pnl_usdt <= 0]

    gross_profit = sum(t.pnl_usdt for t in winners)
    gross_loss   = abs(sum(t.pnl_usdt for t in losers))

    # Drawdown
    peak, max_dd = balance_history[0], 0.0
    for b in balance_history:
        peak  = max(peak, b)
        max_dd = max(max_dd, (peak - b) / peak * 100)

    final_balance = balance_history[-1]
    total_return  = (final_balance - initial_balance) / initial_balance * 100
    annual_return = total_return / months * 12

    # Sharpe anualizado (sobre retornos por operación)
    ret_series = [t.pnl_usdt / initial_balance for t in trades]
    mean_r     = sum(ret_series) / len(ret_series)
    variance   = sum((r - mean_r) ** 2 for r in ret_series) / max(len(ret_series) - 1, 1)
    std_r      = math.sqrt(variance) if variance > 0 else 0
    tpy        = len(trades) / months * 12
    sharpe     = (mean_r / std_r) * math.sqrt(tpy) if std_r > 0 else 0.0

    # Sortino: como Sharpe pero solo penaliza la volatilidad de retornos negativos
    # (Sharpe castiga igual las subidas grandes que las bajadas, lo que no tiene sentido
    # para una estrategia trend-following donde los ganadores grandes son el objetivo).
    downside = [min(r, 0.0) for r in ret_series]
    down_var = sum(r ** 2 for r in downside) / max(len(downside) - 1, 1)
    down_std = math.sqrt(down_var) if down_var > 0 else 0
    sortino  = (mean_r / down_std) * math.sqrt(tpy) if down_std > 0 else 0.0

    # Calmar
    calmar = annual_return / max_dd if max_dd > 0 else float("inf")

    # Racha de pérdidas consecutivas
    max_losing, curr_losing = 0, 0
    for t in trades:
        if t.pnl_usdt <= 0:
            curr_losing += 1
            max_losing   = max(max_losing, curr_losing)
        else:
            curr_losing = 0

    by_reason = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    return dict(
        total          = len(trades),
        winners        = len(winners),
        losers         = len(losers),
        win_rate       = len(winners) / len(trades) * 100,
        gross_profit   = gross_profit,
        gross_loss     = gross_loss,
        profit_factor  = gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        expectancy     = sum(t.pnl_usdt for t in trades) / len(trades),
        max_drawdown   = max_dd,
        total_return   = total_return,
        annual_return  = annual_return,
        sharpe         = sharpe,
        sortino        = sortino,
        calmar         = calmar,
        time_in_market = time_in_market_pct,
        max_losing     = max_losing,
        final_balance  = final_balance,
        by_reason      = by_reason,
        worst5         = sorted(trades, key=lambda x: x.pnl_usdt)[:5],
        best5          = sorted(trades, key=lambda x: x.pnl_usdt, reverse=True)[:5],
    )


# ──────────────────────────────────────────────────────────────────── #
# Informe                                                               #
# ──────────────────────────────────────────────────────────────────── #

def _grade(val: float, good: float, ok: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        if val >= good: return "BUENO"
        if val >= ok:   return "ACEPTABLE"
        return "MEJORABLE"
    else:
        if val <= good: return "BUENO"
        if val <= ok:   return "ACEPTABLE"
        return "MEJORABLE"


def print_report(m: dict, initial_balance: float, months: int):
    if not m:
        print("\n  Sin operaciones en el periodo seleccionado.\n")
        return

    sep = "─" * 56

    print()
    print("=" * 58)
    print("  BACKTEST — EMA 9/21 + HTF 4H + ATR  |  " + config.SYMBOL)
    print(f"  Periodo: {months} meses  |  Intervalo: {config.INTERVAL}")
    print(f"  Slippage: {SLIPPAGE*100:.1f}%  |  Comisión: {FEE*100:.1f}% por lado")
    print("=" * 58)

    print(f"\n  Balance inicial : {initial_balance:>10,.2f} USDT")
    print(f"  Balance final   : {m['final_balance']:>10,.2f} USDT")
    print(f"  Retorno total   : {m['total_return']:>+9.2f} %")
    print(f"  Retorno anual.  : {m['annual_return']:>+9.2f} %")

    print(f"\n  {sep}")
    print("  OPERACIONES")
    print(f"  {sep}")
    br = m["by_reason"]
    print(f"  Total operaciones   : {m['total']:>6}")
    print(f"  Ganadoras           : {m['winners']:>6}  ({m['win_rate']:.1f} %)  "
          f"[{_grade(m['win_rate'], 50, 40)}]")
    print(f"  Perdedoras          : {m['losers']:>6}")
    print(f"  Cerradas por TP     : {br.get('TP', 0):>6}")
    print(f"  Cerradas por SL     : {br.get('SL', 0):>6}")
    print(f"  Cerradas por señal  : {br.get('SIGNAL', 0):>6}")
    print(f"  Beneficio bruto     : {m['gross_profit']:>+9.2f} USDT")
    print(f"  Pérdida bruta       : {m['gross_loss']:>+9.2f} USDT")

    print(f"\n  {sep}")
    print("  MÉTRICAS DE RIESGO")
    print(f"  {sep}")
    print(f"  Profit Factor       : {m['profit_factor']:>8.2f}      "
          f"[{_grade(m['profit_factor'], 1.5, 1.1)}]  (>1.5 bueno)")
    print(f"  Max Drawdown        : {m['max_drawdown']:>8.2f} %     "
          f"[{_grade(m['max_drawdown'], 20, 40, higher_is_better=False)}]  (<20% bueno)")
    print(f"  Sharpe Ratio        : {m['sharpe']:>8.2f}      "
          f"[{_grade(m['sharpe'], 1.0, 0.5)}]  (>1.0 bueno)")
    print(f"  Sortino Ratio       : {m['sortino']:>8.2f}      "
          f"[{_grade(m['sortino'], 1.5, 0.8)}]  (>1.5 bueno; solo penaliza caídas)")
    print(f"  Calmar Ratio        : {m['calmar']:>8.2f}      "
          f"[{_grade(m['calmar'], 0.5, 0.2)}]  (>0.5 bueno)")
    print(f"  Expectativa/op      : {m['expectancy']:>+8.2f} USDT")
    print(f"  Racha pérd. consec. : {m['max_losing']:>8}  operaciones")
    print(f"  Tiempo en mercado   : {m['time_in_market']:>8.1f} %")

    print(f"\n  {sep}")
    print("  TOP 5 MEJORES OPERACIONES")
    print(f"  {sep}")
    for t in m["best5"]:
        print(f"  {t.entry_time} → {t.exit_time}  {t.exit_reason:<6}  {t.pnl_usdt:>+8.2f} USDT  ({t.pnl_pct:>+.2f}%)")

    print(f"\n  {sep}")
    print("  TOP 5 PEORES OPERACIONES")
    print(f"  {sep}")
    for t in m["worst5"]:
        print(f"  {t.entry_time} → {t.exit_time}  {t.exit_reason:<6}  {t.pnl_usdt:>+8.2f} USDT  ({t.pnl_pct:>+.2f}%)")

    # Veredicto global — criterios para trend-following profesional
    c1 = m["profit_factor"] >= 1.5     # PF ≥ 1.5
    c2 = m["sharpe"]        >= 1.0     # Sharpe ≥ 1.0 (estándar profesional)
    c3 = m["max_drawdown"]  < 20       # DD < 20%
    c4 = m["calmar"]        >= 1.0     # Calmar ≥ 1.0 (retorno/riesgo)
    c5 = m["expectancy"]    > 0        # expectativa positiva
    score = sum([c1, c2, c3, c4, c5])
    veredicto = {5: "EXCELENTE", 4: "BUENA", 3: "ACEPTABLE", 2: "MEJORABLE", 1: "DÉBIL", 0: "NO VIABLE"}

    print()
    print("=" * 58)
    print(f"  CRITERIOS PARA EXCELENTE:")
    print(f"  Profit Factor ≥ 1.5  : {'OK' if c1 else 'FALTA':4}  ({m['profit_factor']:.2f})")
    print(f"  Sharpe ≥ 1.0         : {'OK' if c2 else 'FALTA':4}  ({m['sharpe']:.2f})")
    print(f"  Max DD < 20%         : {'OK' if c3 else 'FALTA':4}  ({m['max_drawdown']:.1f}%)")
    print(f"  Calmar ≥ 1.0         : {'OK' if c4 else 'FALTA':4}  ({m['calmar']:.2f})")
    print(f"  Expectativa > 0      : {'OK' if c5 else 'FALTA':4}  ({m['expectancy']:+.2f} USDT)")
    print(f"  VEREDICTO: {veredicto.get(score, 'N/A')}  ({score}/5 criterios)")
    print("=" * 58)
    print()


# ──────────────────────────────────────────────────────────────────── #
# Exportar CSV                                                          #
# ──────────────────────────────────────────────────────────────────── #

def save_csv(trades: List[Trade], path: str):
    fields = ["engine", "side", "entry_time", "entry_price", "qty", "sl", "tp", "atr",
              "exit_time", "exit_price", "exit_reason", "pnl_usdt", "pnl_pct", "funding_cost"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow({k: getattr(t, k) for k in fields})
    print(f"  Log de operaciones guardado en: {path}")


# ──────────────────────────────────────────────────────────────────── #
# Punto de entrada                                                      #
# ──────────────────────────────────────────────────────────────────── #

async def main(months: int, initial_balance: float, csv_path: Optional[str]):
    print(f"\nDescargando velas 1H ({config.SYMBOL}, {months} meses)...")
    klines_1h = await fetch_klines(config.INTERVAL, months)
    print(f"  -> {len(klines_1h):,} velas")

    print(f"Descargando velas 4H (filtro HTF)...")
    klines_4h = await fetch_klines(config.HTF_INTERVAL, months)
    print(f"  -> {len(klines_4h):,} velas")

    print("Ejecutando simulación...\n")
    trades, balance_history, time_in_market_pct = run_backtest(klines_1h, klines_4h, initial_balance)

    metrics = compute_metrics(trades, balance_history, initial_balance, months, time_in_market_pct)
    print_report(metrics, initial_balance, months)

    if csv_path and trades:
        save_csv(trades, csv_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest EMA Crossover")
    parser.add_argument("--months",  type=int,   default=24,     help="Meses de historial (default: 24)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--csv",     type=str,   default=None,   help="Exportar operaciones a CSV")
    args = parser.parse_args()

    asyncio.run(main(args.months, args.balance, args.csv))
