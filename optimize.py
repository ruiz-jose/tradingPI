"""
Optimizador de estrategia — busca la configuracion mas rentable.
Descarga klines una sola vez y prueba todas las combinaciones de parametros.

Por defecto NO modifica config.py: esta búsqueda es in-sample sobre todo el periodo,
con riesgo real de curve-fitting. Antes de aplicar cualquier resultado, valídalo con
walkforward.py (out-of-sample) y solo entonces usa --apply.

Uso:
    python optimize.py                     # 36 meses, 1000 USDT, top 15 (no modifica config.py)
    python optimize.py --months 48 --top 10
    python optimize.py --apply             # aplica la config ganadora a config.py (in-sample)
"""
import asyncio
import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from binance import AsyncClient

from config import config
from backtest import run_backtest, compute_metrics


# ------------------------------------------------------------------ #
# Parametros a explorar                                               #
# ------------------------------------------------------------------ #

@dataclass
class Params:
    interval:     str
    htf_interval: str
    ema_fast:     int
    ema_slow:     int
    adx_min:      float
    atr_sl_mult:  float
    rsi_min:      float = 40.0
    rsi_max:      float = 80.0   # RSI 70-80 = momentum válido en tendencia fuerte

    def label(self) -> str:
        return (
            f"{self.interval:<2}  EMA{self.ema_fast:>2}/{self.ema_slow:<2}  "
            f"ADX>{self.adx_min:>2.0f}  SL={self.atr_sl_mult}xATR"
        )


def build_grid() -> list:
    grid = []
    # 1H como TF principal, filtro HTF en 4H
    for fast, slow in [(9, 21), (12, 26)]:
        for adx in [0, 15, 20, 25]:
            for sl_mult in [1.5, 2.0, 2.5]:
                grid.append(Params("1h", "4h", fast, slow, adx, sl_mult))
    # 4H como TF principal, filtro HTF en 1D
    for fast, slow in [(9, 21), (21, 55)]:
        for adx in [0, 15, 20, 25]:
            for sl_mult in [1.5, 2.0, 2.5]:
                grid.append(Params("4h", "1d", fast, slow, adx, sl_mult))
    return grid


# ------------------------------------------------------------------ #
# Puntuacion compuesta                                                #
# ------------------------------------------------------------------ #

def score(m: dict) -> float:
    """
    Combina PF, Sharpe y cantidad de trades.
    Penaliza configuraciones con < 15 operaciones (estadisticamente invalidas).
    """
    if not m or m["total"] < 15 or m["expectancy"] <= 0 or m["profit_factor"] <= 1.0:
        return 0.0
    trades_factor = min(m["total"], 60) / 60   # bono maximo a 60 trades
    pf_factor     = min(m["profit_factor"], 5.0)
    sharpe_factor = max(m["sharpe"], 0.05)
    return pf_factor * trades_factor * sharpe_factor


# ------------------------------------------------------------------ #
# Descarga de datos                                                   #
# ------------------------------------------------------------------ #

async def fetch_raw(symbol: str, interval: str, months: int) -> list:
    client = await AsyncClient.create(
        api_key=config.API_KEY or "", api_secret=config.API_SECRET or "", testnet=False
    )
    since_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000
    )
    all_klines, start = [], since_ms
    while True:
        batch = await client.get_klines(
            symbol=symbol, interval=interval, startTime=start, limit=1000
        )
        if not batch:
            break
        all_klines.extend(batch)
        if len(batch) < 1000:
            break
        start = int(batch[-1][0]) + 1
    await client.close_connection()
    return all_klines


# ------------------------------------------------------------------ #
# Aplicar la config ganadora a config.py                             #
# ------------------------------------------------------------------ #

def apply_to_config(p: Params) -> None:
    path = "config.py"
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # INTERVAL sin tocar HTF_INTERVAL — lookbehind negativo
    text = re.sub(r'(?<!HTF_)INTERVAL: str = "[^"]*"',
                  f'INTERVAL: str = "{p.interval}"', text)
    text = re.sub(r'HTF_INTERVAL: str = "[^"]*"',
                  f'HTF_INTERVAL: str = "{p.htf_interval}"', text)
    text = re.sub(r'EMA_FAST: int = \d+',     f'EMA_FAST: int = {p.ema_fast}',         text)
    text = re.sub(r'EMA_SLOW: int = \d+',     f'EMA_SLOW: int = {p.ema_slow}',         text)
    text = re.sub(r'ADX_MIN: float = [\d.]+', f'ADX_MIN: float = {p.adx_min}',         text)
    text = re.sub(r'ATR_SL_MULTIPLIER: float = [\d.]+',
                  f'ATR_SL_MULTIPLIER: float = {p.atr_sl_mult}', text)
    text = re.sub(r'RSI_BUY_MIN: float = [\d.]+', f'RSI_BUY_MIN: float = {p.rsi_min}', text)
    text = re.sub(r'RSI_BUY_MAX: float = [\d.]+', f'RSI_BUY_MAX: float = {p.rsi_max}', text)

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------------ #
# Main                                                                #
# ------------------------------------------------------------------ #

async def main(months: int, initial_balance: float, top_n: int, apply: bool) -> None:
    symbol = config.SYMBOL
    print(f"\nOptimizador | {symbol} | {months} meses | {initial_balance:.0f} USDT")
    print("=" * 70)

    # Descargar los 3 intervalos necesarios una sola vez
    klines: dict = {}
    for interval in ["1h", "4h", "1d"]:
        print(f"  Descargando {interval}...", end=" ", flush=True)
        klines[interval] = await fetch_raw(symbol, interval, months)
        print(f"{len(klines[interval]):,} velas")

    grid = build_grid()
    print(f"\nProbando {len(grid)} combinaciones...\n")

    results = []
    for i, p in enumerate(grid, 1):
        # Parchear config temporalmente para esta combinacion
        config.INTERVAL          = p.interval
        config.HTF_INTERVAL      = p.htf_interval
        config.EMA_FAST          = p.ema_fast
        config.EMA_SLOW          = p.ema_slow
        config.ADX_MIN           = p.adx_min
        config.ATR_SL_MULTIPLIER = p.atr_sl_mult
        config.RSI_BUY_MIN       = p.rsi_min
        config.RSI_BUY_MAX       = p.rsi_max

        trades, bal_hist, time_in_market_pct = run_backtest(
            klines[p.interval], klines[p.htf_interval], initial_balance
        )
        m = compute_metrics(trades, bal_hist, initial_balance, months, time_in_market_pct)
        s = score(m)
        results.append((s, p, m))

        # Barra de progreso
        if i % 6 == 0 or i == len(grid):
            done = int(i / len(grid) * 40)
            bar  = "#" * done + "-" * (40 - done)
            print(f"  [{bar}] {i}/{len(grid)}", end="\r", flush=True)

    print()

    results.sort(key=lambda x: x[0], reverse=True)
    validas = [(s, p, m) for s, p, m in results if s > 0]

    # ---- Tabla de resultados ----
    print()
    print("=" * 82)
    print(f"  TOP {min(top_n, len(validas))} CONFIGURACIONES RENTABLES  |  {symbol}  |  {months} meses")
    print("=" * 82)
    print(f"  {'#':>2}  {'Configuracion':42}  {'Op':>4}  {'PF':>5}  {'Sharpe':>6}  "
          f"{'DD%':>5}  {'Win%':>5}  {'Ret%':>7}")
    print("  " + "-" * 80)

    for rank, (s, p, m) in enumerate(validas[:top_n], 1):
        marker = "  <<" if rank == 1 else ""
        print(
            f"  {rank:>2}  {p.label():42}  "
            f"{m['total']:>4}  "
            f"{m['profit_factor']:>5.2f}  "
            f"{m['sharpe']:>6.2f}  "
            f"{m['max_drawdown']:>5.1f}  "
            f"{m['win_rate']:>5.1f}  "
            f"{m['total_return']:>+7.2f}%"
            f"{marker}"
        )

    if not validas:
        print("  Ninguna configuracion supero los criterios minimos.")
        print()
        return

    # ---- Detalle de la ganadora ----
    best_s, best_p, best_m = validas[0]
    print()
    print("=" * 82)
    print("  GANADORA DETALLADA")
    print("=" * 82)
    print(f"  Intervalo principal : {best_p.interval}  (HTF: {best_p.htf_interval})")
    print(f"  EMA                 : {best_p.ema_fast}/{best_p.ema_slow}")
    print(f"  ADX minimo          : {best_p.adx_min}")
    print(f"  ATR SL multiplier   : {best_p.atr_sl_mult}x")
    print(f"  RSI rango           : {best_p.rsi_min} - {best_p.rsi_max}")
    print()
    print(f"  Operaciones         : {best_m['total']} en {months} meses")
    print(f"  Win rate            : {best_m['win_rate']:.1f}%")
    print(f"  Profit Factor       : {best_m['profit_factor']:.2f}")
    print(f"  Sharpe Ratio        : {best_m['sharpe']:.2f}")
    print(f"  Max Drawdown        : {best_m['max_drawdown']:.2f}%")
    print(f"  Expectativa/op      : {best_m['expectancy']:+.2f} USDT")
    print(f"  Retorno total       : {best_m['total_return']:+.2f}%")
    print(f"  Retorno anual       : {best_m['annual_return']:+.2f}%")
    print(f"  Racha perdedora max : {best_m['max_losing']} consecutivas")
    print()

    if apply:
        apply_to_config(best_p)
        print("  config.py actualizado con la configuracion ganadora.")
    else:
        print("  (config.py NO fue modificado — esto es una búsqueda IN-SAMPLE sobre todo el periodo,")
        print("   con alto riesgo de curve-fitting. Antes de aplicarla, valídala con:")
        print(f"     python walkforward.py --months-total {months}")
        print("   y aplica manualmente solo si se sostiene out-of-sample. Usa --apply para forzar.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Optimizador de estrategia EMA")
    parser.add_argument("--months",  type=int,   default=36,     help="Meses de historial (default: 36)")
    parser.add_argument("--balance", type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    parser.add_argument("--top",     type=int,   default=15,     help="Configs a mostrar (default: 15)")
    parser.add_argument("--apply",   action="store_true",
                         help="Aplicar la config ganadora a config.py (in-sample — valida con walkforward.py primero)")
    args = parser.parse_args()

    asyncio.run(main(args.months, args.balance, args.top, args.apply))
