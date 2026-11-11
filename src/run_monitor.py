# -*- coding: utf-8 -*-
"""实时监控入口：见 quant/monitor.py 模块说明。"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from quant.monitor import print_panel, run_once
from quant.paths import app_root

ROOT = app_root()


def main():
    ap = argparse.ArgumentParser(description="量化实时监控 + 模拟盘")
    ap.add_argument("--once", action="store_true", help="只跑一次退出")
    ap.add_argument("--loop", type=int, default=None, help="循环间隔秒数（默认取配置）")
    ap.add_argument("--reset", action="store_true", help="重置模拟盘后运行")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    interval = args.loop or int(cfg["monitor"].get("loop_seconds", 60))

    while True:
        try:
            state = run_once(cfg, reset=args.reset)
            print_panel(state)
        except Exception as e:
            print(f"[监控出错] {e}  （{interval} 秒后重试）", file=sys.stderr)
        args.reset = False  # 只在首轮生效
        if args.once:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已停止监控。")
            break


if __name__ == "__main__":
    main()
