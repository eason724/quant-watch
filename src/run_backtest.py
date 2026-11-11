# -*- coding: utf-8 -*-
"""一键回测：跑 config.json 里全部策略，输出 Excel 报表 + PDF 研报 + 仪表盘用 JSON。

用法（在 WSL 中）:
    .venv/bin/python run_backtest.py                # 按 config.json 默认参数
    .venv/bin/python run_backtest.py --start 2020-01-01
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from quant.backtest import Fees, run_backtest
from quant.data import fetch_daily
from quant.paths import app_root
from quant.report_pdf import export_pdf
from quant.report_xlsx import export_xlsx
from quant.strategies import make_strategy

ROOT = app_root()


def build_fees_note(cfg) -> str:
    cn = cfg["fees"]["cn"]
    us = cfg["fees"]["us"]
    return (f"费用：A股佣金万{cn['buy_commission_bps']}/滑点万{cn['slippage_bps']}/免印花税 · "
            f"美股佣金{'0' if us['buy_commission_bps'] == 0 else us['buy_commission_bps']}/滑点万{us['slippage_bps']} · "
            f"权重变动<0.5%不交易")


def main():
    ap = argparse.ArgumentParser(description="量化策略一键回测")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="output")
    ap.add_argument("--start", default=None, help="回测起始日，如 2020-01-01")
    args = ap.parse_args()

    cfg = json.loads((ROOT / args.config).read_text(encoding="utf-8"))
    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    (out / "charts").mkdir(exist_ok=True)
    start = args.start or cfg["backtest"]["start"]
    capital = float(cfg["backtest"]["capital"])
    fees_note = build_fees_note(cfg)

    cache: dict = {}
    failed: list[str] = []
    results = []
    print(f"回测区间 {start} ~ 今 · 初始资金 ¥{capital:,.0f}\n" + "-" * 78)
    for sc in cfg["backtest"]["strategies"]:
        try:
            mkt = sc.get("market", "cn")
            fees = Fees(**cfg["fees"][mkt])
            data = {}
            for s in sc["universe"]:
                if s not in cache:
                    cache[s] = fetch_daily(s, start=start)
                data[s] = cache[s]
            bench_sym = cfg["backtest"]["benchmarks"][mkt]
            if bench_sym not in cache:
                cache[bench_sym] = fetch_daily(bench_sym, start=start)
            res = run_backtest(make_strategy(sc), data, fees, capital, cache[bench_sym], market=mkt)
            results.append(res)
            m = res.metrics
            bc = cache[bench_sym]["close"]
            bench_ret = bc.iloc[-1] / bc.iloc[0] - 1
            print(f"{res.name:<18} 总收益 {m['总收益率']:+7.1%} | 年化 {m['年化收益率']:+7.1%} | "
                  f"回撤 {m['最大回撤']:6.1%} | 夏普 {m['夏普比率']:5.2f} | {m['交易次数']:>3} 笔 | "
                  f"基准 {bench_ret:+7.1%}")
        except Exception as e:   # 单个策略失败不拖死整批（云端定时任务尤其需要）
            failed.append(sc.get("name", sc["type"]))
            print(f"{sc.get('name', sc['type']):<18} ⚠ 跳过：{type(e).__name__}: {str(e)[:80]}")
    if not results:
        # 全部失败（如接口临时封禁云端 IP 段）：不更新报告，优雅退出保旧数据，下轮再试
        print("⚠ 所有策略本轮均失败——保留上一轮报告数据，等待下一轮自动重试")
        print(f"✅ 保留旧数据继续发布（失败清单: {'、'.join(failed)}）")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    xlsx_path = out / f"回测报告_{stamp}.xlsx"
    pdf_path = out / f"量化策略研报_{stamp}.pdf"
    export_xlsx(results, xlsx_path, capital, start, fees_note)
    export_pdf(results, pdf_path, capital, start, fees_note, out / "charts")

    # 给在线仪表盘用的 JSON（净值归一到期初=1）
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "start": start,
        "capital": capital,
        "fees_note": fees_note,
        "strategies": [],
    }
    for res in results:
        eq = res.equity
        bench = res.benchmark
        payload["strategies"].append({
            "name": res.name,
            "market": res.market,
            "metrics": {k: (None if v is None else float(v)) for k, v in res.metrics.items()},
            "dates": [str(d.date()) for d in eq.index],
            "equity_norm": [round(float(v) / float(eq.iloc[0]), 4) for v in eq.values],
            "benchmark_norm": ([round(float(v) / float(bench.iloc[0]), 4) for v in bench.values]
                               if bench is not None else None),
            "bench_dates": ([str(d.date()) for d in bench.index] if bench is not None else []),
            "trades": res.trades[-30:],
            "n_trades": len(res.trades),
        })
    (out / "backtest_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("-" * 78)
    print(f"✅ Excel 报表: {xlsx_path.name}")
    print(f"✅ PDF 研报  : {pdf_path.name}")
    print(f"✅ 仪表盘数据: output/backtest_results.json")


if __name__ == "__main__":
    main()
