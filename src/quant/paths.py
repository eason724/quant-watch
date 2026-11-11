# -*- coding: utf-8 -*-
"""统一的应用根目录：源码运行 = 项目目录；PyInstaller 打包后 = exe 所在目录。
config.json、data_cache/、output/ 都定位在应用根下。"""
import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
