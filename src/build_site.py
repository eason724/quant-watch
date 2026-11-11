# -*- coding: utf-8 -*-
"""构建在线服务站点：把监控快照、回测数据、最新报告文件组装进 site/ 目录。

用法: .venv/bin/python build_site.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
SITE = ROOT / "site"


def latest(pattern: str) -> Path | None:
    files = sorted(OUT.glob(pattern))
    return files[-1] if files else None


def main():
    state_file = OUT / "state.json"
    bt_file = OUT / "backtest_results.json"
    missing = [f.name for f in (state_file, bt_file) if not f.exists()]
    if missing:
        raise SystemExit(f"缺少 {missing}，请先运行 run_monitor.py --once 和 run_backtest.py")

    SITE.mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    (SITE / "files").mkdir(exist_ok=True)

    shutil.copyfile(ROOT / "site_template.html", SITE / "index.html")
    shutil.copyfile(ROOT / "echarts.min.js", SITE / "echarts.min.js")
    shutil.copyfile(ROOT / "app.js", SITE / "app.js")
    shutil.copyfile(state_file, SITE / "data" / "state.json")
    shutil.copyfile(bt_file, SITE / "data" / "backtest.json")

    xlsx = latest("回测报告_*.xlsx")
    pdf = latest("量化策略研报_*.pdf")
    if xlsx:
        shutil.copyfile(xlsx, SITE / "files" / "report.xlsx")
    if pdf:
        shutil.copyfile(pdf, SITE / "files" / "report.pdf")

    size = sum(f.stat().st_size for f in SITE.rglob("*") if f.is_file())
    print(f"✅ 站点构建完成: {SITE}（{size / 1e6:.1f} MB）")
    for f in sorted(SITE.rglob("*")):
        if f.is_file():
            print(f"   {f.relative_to(SITE)}")


if __name__ == "__main__":
    main()
