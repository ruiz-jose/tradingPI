"""
Walk-forward validation — valida out-of-sample si los parámetros que ganan
en backtest.py siguen funcionando en datos que NO vieron durante la calibración.

Por cada fold: descarga ya hecha una sola vez, se prueba una grilla pequeña de
ADX_MIN / SCALE_OUT_R sobre el tramo de TRAIN, se aplica la mejor combinación
(por score(), igual que optimize.py) al tramo de TEST inmediatamente posterior,
y se registran solo las métricas de TEST (out-of-sample) — la única medida
honesta de si la estrategia generaliza.

Uso:
    python walkforward.py                                  # 48 meses totales, train 12 / test 3 / step 3
    python walkforward.py --months-total 60 --train-months 18 --test-months 3 --step-months 3
"""

import asyncio
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import config
from backtest import fetch_klines, run_backtest, compute_metrics
from optimize import score


@dataclass
class WFParams:
    adx_min: float
    scale_out_r: float

    def label(self) -> str:
        return f"ADX>{self.adx_min:>2.0f}  SCALE_OUT_R={self.scale_out_r}"


def build_wf_grid() -> list:
    """Grilla reducida — solo calibra los puntos 2 (ADX_MIN) y 8 (SCALE_OUT_R),
    que es lo que dejamos pendiente de validar out-of-sample en config.py."""
    grid = []
    for adx_min in [0, 15, 20, 25]:
        for scale_out_r in [0.0, 1.5, 2.0, 3.0]:
            grid.append(WFParams(adx_min, scale_out_r))
    return grid


def _slice(klines: list, start_ms: int, end_ms: int) -> list:
    return [k for k in klines if start_ms <= int(k[0]) < end_ms]


def build_folds(total_start_ms: int, now_ms: int, train_months: int, test_months: int, step_months: int):
    day_ms = 24 * 3600 * 1000
    train_ms = train_months * 30 * day_ms
    test_ms  = test_months * 30 * day_ms
    step_ms  = step_months * 30 * day_ms

    folds = []
    train_start = total_start_ms
    while True:
        train_end = train_start + train_ms
        test_end  = train_end + test_ms
        if test_end > now_ms:
            break
        folds.append((train_start, train_end, test_end))
        train_start += step_ms
    return folds


async def main(months_total: int, train_months: int, test_months: int, step_months: int, balance: float):
    print(f"\nWALK-FORWARD | {config.SYMBOL} | {months_total} meses totales")
    print(f"Train: {train_months}m  |  Test: {test_months}m  |  Step: {step_months}m")
    print("=" * 70)

    print(f"\nDescargando velas {config.INTERVAL}...")
    klines_main = await fetch_klines(config.INTERVAL, months_total)
    print(f"  -> {len(klines_main):,} velas")
    print(f"Descargando velas {config.HTF_INTERVAL} (HTF)...")
    klines_htf = await fetch_klines(config.HTF_INTERVAL, months_total)
    print(f"  -> {len(klines_htf):,} velas")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    total_start_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=30 * months_total)).timestamp() * 1000
    )
    folds = build_folds(total_start_ms, now_ms, train_months, test_months, step_months)
    if not folds:
        print("\n  No hay suficientes datos para ni un solo fold con esos parámetros.\n")
        return

    grid = build_wf_grid()
    orig_adx_min, orig_scale_out_r = config.ADX_MIN, config.SCALE_OUT_R

    results = []
    for i, (train_start, train_end, test_end) in enumerate(folds, 1):
        train_main = _slice(klines_main, train_start, train_end)
        train_htf  = _slice(klines_htf, train_start, train_end)
        test_main  = _slice(klines_main, train_end, test_end)
        test_htf   = _slice(klines_htf, train_end, test_end)

        best_score, best_params = -1.0, grid[0]
        for p in grid:
            config.ADX_MIN, config.SCALE_OUT_R = p.adx_min, p.scale_out_r
            trades, bal_hist, tim = run_backtest(train_main, train_htf, balance)
            m = compute_metrics(trades, bal_hist, balance, train_months, tim)
            s = score(m)
            if s > best_score:
                best_score, best_params = s, p

        config.ADX_MIN, config.SCALE_OUT_R = best_params.adx_min, best_params.scale_out_r
        trades, bal_hist, tim = run_backtest(test_main, test_htf, balance)
        m_test = compute_metrics(trades, bal_hist, balance, test_months, tim)

        fold_label = (
            f"{datetime.fromtimestamp(train_end/1000, tz=timezone.utc):%Y-%m}"
            f" → {datetime.fromtimestamp(test_end/1000, tz=timezone.utc):%Y-%m}"
        )
        results.append((fold_label, best_params, m_test))

    config.ADX_MIN, config.SCALE_OUT_R = orig_adx_min, orig_scale_out_r

    print(f"\n{'Fold (test)':<18} {'Mejor params (train)':<26} {'Trades':>7} {'PF':>6} {'Sharpe':>7} {'Sortino':>8} {'Retorno':>9}")
    print("-" * 90)
    pf_list, sharpe_list, sortino_list, ret_list = [], [], [], []
    for label, params, m in results:
        if not m:
            print(f"{label:<18} {params.label():<26} {'sin operaciones':>7}")
            continue
        print(f"{label:<18} {params.label():<26} {m['total']:>7} {m['profit_factor']:>6.2f} "
              f"{m['sharpe']:>7.2f} {m['sortino']:>8.2f} {m['total_return']:>+8.2f}%")
        pf_list.append(m["profit_factor"] if m["profit_factor"] != float("inf") else 5.0)
        sharpe_list.append(m["sharpe"])
        sortino_list.append(m["sortino"])
        ret_list.append(m["total_return"])

    print("-" * 90)
    if pf_list:
        n = len(pf_list)
        print(f"\nPromedio OUT-OF-SAMPLE sobre {n} folds con operaciones:")
        print(f"  Profit Factor : {sum(pf_list)/n:.2f}")
        print(f"  Sharpe        : {sum(sharpe_list)/n:.2f}")
        print(f"  Sortino       : {sum(sortino_list)/n:.2f}")
        print(f"  Retorno medio : {sum(ret_list)/n:+.2f}% por fold de {test_months} meses")
        print("\n  Esto es lo que de verdad valida (o tumba) los resultados in-sample de backtest.py.")
    else:
        print("\n  Ningún fold generó operaciones suficientes para evaluar.")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward validation")
    parser.add_argument("--months-total", type=int, default=48, help="Historial total a usar (default: 48)")
    parser.add_argument("--train-months", type=int, default=12, help="Meses de entrenamiento por fold (default: 12)")
    parser.add_argument("--test-months",  type=int, default=3,  help="Meses de test por fold (default: 3)")
    parser.add_argument("--step-months",  type=int, default=3,  help="Avance entre folds (default: 3)")
    parser.add_argument("--balance",      type=float, default=1000.0, help="Balance inicial USDT (default: 1000)")
    args = parser.parse_args()

    asyncio.run(main(args.months_total, args.train_months, args.test_months, args.step_months, args.balance))
