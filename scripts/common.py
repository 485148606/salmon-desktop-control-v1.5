# -*- coding: utf-8 -*-
"""公共模块：DPI 处理、JSON 输出、region/显示器工具。

坐标约定（关键）:
- 本模块在 import 阶段就把进程设为 Per-Monitor DPI Aware，
  此后 GetSystemMetrics / pyautogui / mss 全部使用【物理像素】，
  三者坐标系一致，可直接互换。所谓"物理像素"= 屏幕真实分辨率。
- 若系统缩放为 125%/150% 且本模块未生效，会出现"点不准/找图偏移"，
  此时请先确认本模块在 pyautogui、mss 之前 import。
"""

import ctypes
import json
import os
import sys

# ---------- DPI awareness（必须在任何 GUI 相关库 import 之前） ----------
def _make_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

_make_dpi_aware()

# ---------- 输出 ----------
def emit(data: dict) -> None:
    """标准结果以单行 JSON 写 stdout（UTF-8 无 BOM），便于主代理解析。"""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(msg, code: int = 1):
    sys.stderr.write(json.dumps({"ok": False, "error": str(msg)}, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def ok(**kw) -> None:
    d = {"ok": True}
    d.update(kw)
    emit(d)


# ---------- region / 坐标工具 ----------
def parse_region(s: str):
    """'x,y,w,h' -> (left, top, width, height)，做基本合法性检查。"""
    if not s:
        return None
    try:
        parts = [int(v.strip()) for v in s.split(",")]
        if len(parts) != 4:
            raise ValueError
        x, y, w, h = parts
        if w < 0 or h < 0:
            raise ValueError
        return (x, y, w, h)
    except Exception:
        fail("region 格式应为 x,y,w,h 的整数，例如 0,0,1920,1080，收到: %r" % s)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def system_dpi_scale() -> float:
    """读取当前主显示器逻辑缩放比例（1.0 / 1.25 / 1.5 ...），仅供信息展示。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return dpi / 96.0 if dpi else 1.0
    except Exception:
        return 1.0
