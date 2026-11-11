# -*- coding: utf-8 -*-
"""Windows exe 交互菜单入口（也可直接 python cli_main.py 运行）。"""
import os
import sys

if os.name == "nt":
    os.system("chcp 65001 >NUL")  # 控制台切 UTF-8，避免中文乱码
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))

import run_backtest
import run_monitor


def call_backtest():
    sys.argv = ["run_backtest.py"]
    run_backtest.main()


def call_monitor(once=False, reset=False):
    argv = ["run_monitor.py"]
    if once:
        argv.append("--once")
    if reset:
        argv.append("--reset")
    sys.argv = argv
    run_monitor.main()


MENU = """
========================================================
  观澜量化 · A股/美股 策略回测与实时监控
========================================================
  应用目录: {root}
  [1] 一键回测 → 生成 Excel 报表 + PDF 研报
  [2] 实时监控（循环刷新，Ctrl+C 停止）
  [3] 监控一轮 + 模拟盘记账（跑一次就退出）
  [4] 重置模拟盘（清空持仓，资金回到初始）
  [0] 退出
""".format(root=run_monitor.ROOT)


def main():
    while True:
        print(MENU)
        choice = input("请选择: ").strip()
        try:
            if choice == "1":
                call_backtest()
            elif choice == "2":
                call_monitor()
            elif choice == "3":
                call_monitor(once=True)
            elif choice == "4":
                if input("确认重置模拟盘? (y/N): ").strip().lower() == "y":
                    call_monitor(once=True, reset=True)
            elif choice == "0":
                print("再见。")
                return
            else:
                print("无效选项。")
        except KeyboardInterrupt:
            print("\n(已中断)")
        except Exception as e:
            print(f"\n[出错] {e}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n再见。")
