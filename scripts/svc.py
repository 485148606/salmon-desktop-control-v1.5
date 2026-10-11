# -*- coding: utf-8 -*-
"""svc.py —— salmon-desktop-control 常驻加速服务（Windows）。

为什么需要它：
  原 skill 每执行一次 screen.py / mouse.py 就要重启一次 Python：
    - 解释器启动 ~0.3s
    - import numpy / cv2 / PIL / mss / pyautogui ~1.5-2.5s（cv2 最重）
    - RapidOCR 首次加载 ONNX 模型 ~1-3s
  一个「截图 → OCR → 点击 → 再截图验证」循环 = 4 次进程启动 ≈ 10-20 秒。

  本服务把这些一次性加载完，之后所有命令走 localhost HTTP，
  单命令往返通常 < 100ms（OCR 本身除外，但引擎已常驻内存）。

额外能力（原 skill 没有）：
  - shot-pw    : 用 PrintWindow 抓窗口 HDC，mss 抓黑屏时（显示器睡眠/窗口被遮挡）的兜底
  - wait-text  : 轮询等文字出现（替代盲 sleep，元素一出现立刻返回）
  - wait-stable: 轮询等画面稳定（替代固定 sleep 等重编译/加载）
  - batch      : 一次调用顺序执行多个动作，省掉 N 次进程/IPC 往返
  - win list/activate/screenshot : 窗口管理封装（含 DPI-aware 正确做法）

用法:
  python svc.py serve [--port 8765]        # 前台常驻（调试用）
  python svc.py start  [--port 8765]       # 后台拉起（mc.py 会自动做，一般不用手敲）
  python svc.py stop   [--port 8765]
  python svc.py ping   [--port 8765]
"""
import os
import sys
import io
import json
import time
import queue
import base64
import hashlib
import threading
import subprocess
import tempfile
import argparse
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402  必须在 GUI 库之前 import（内部设置 DPI awareness）

# ---- 重量级依赖：仅在这里 import 一次，之后所有请求复用 ----
import numpy as np            # noqa: E402
import cv2                    # noqa: E402
from PIL import Image         # noqa: E402
try:
    from mss import MSS as _MSS_CLS
except ImportError:
    from mss import mss as _MSS_CLS
import pyautogui              # noqa: E402

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

_DEFAULT_PORT = 8765
_OCR_LOCK = threading.Lock()
# 全局操作锁：mss / pyautogui 都不是线程安全的，
# ThreadingHTTPServer 并发进来会互相踩（表现为 svc 静默崩溃或截图串味）。
# 所有 op 串行执行，宁可排队也不能崩。
_OP_LOCK = threading.RLock()
_OCR_CACHE = {}          # (img_md5, max_side) -> (ts, items)   items 为图片本地坐标（不含屏幕偏移）
_OCR_CACHE_MAX = 32
# OCR 推理不放在 svc 进程内（rapidocr/onnxruntime 在 socketserver 请求线程里
# 100% 卡死，实测独立进程正常）→ 第 4 轮：交给【常驻 daemon】ocr_worker.py --daemon。
# 旧版每次 OCR 临时 spawn 子进程，模型反复加载 ≈1~2.5s 冷启开销；daemon 只加载一次。
_OCR_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_worker.py")
_OCR_DAEMON = None            # Popen 句柄
_OCR_DAEMON_LOCK = threading.Lock()
_OCR_TIMEOUT_DEFAULT = 90     # 单次 OCR 最长等待（daemon 卡死则 kill 重启）

# ---- watchdog 状态（svc 自愈 / 客户端探活用）----
_LAST_OP_TS = _now0 = time.time()      # 最近一次 op 结束时间
_OP_INFLIGHT = False                   # 当前是否正在执行 op（排他）
_MAX_OP_SECONDS = 300                  # 单 op 超过该时长视为假死 → 自杀等客户端重启
_WATCHDOG_TICK = 5.0


def _pid_file(port):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "svc.%d.pid" % port)


def _write_pid(port):
    try:
        with open(_pid_file(port), "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _rm_pid(port):
    try:
        os.remove(_pid_file(port))
    except OSError:
        pass


# ============================ 基础工具 ============================
def _now():
    return time.time()


def _md5(b):
    return hashlib.md5(b).hexdigest()


# ============================ OCR daemon 管理 ============================
def _daemon_start():
    """拉起常驻 OCR daemon（若已在跑则直接返回）。"""
    global _OCR_DAEMON
    if _OCR_DAEMON is not None and _OCR_DAEMON.poll() is None:
        return _OCR_DAEMON
    try:
        p = subprocess.Popen(
            [sys.executable, _OCR_WORKER, "--daemon"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _OCR_DAEMON = p
        return p
    except Exception as e:
        _OCR_DAEMON = None
        print("[ocr] daemon start fail: %s" % e, file=sys.stderr, flush=True)
        return None


def _daemon_kill():
    """kill 当前 daemon（卡死/异常时重启用）。进程句柄置空，下次请求自动重建。"""
    global _OCR_DAEMON
    p = _OCR_DAEMON
    _OCR_DAEMON = None
    if p is None:
        return
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.wait(timeout=3)
    except Exception:
        pass


def _daemon_request(cmd, timeout=_OCR_TIMEOUT_DEFAULT):
    """向 daemon 发一行 JSON 指令，阻塞读一行 JSON 返回。

    daemon 一次只服务一个请求（本 svc 所有 op 已被 _OP_LOCK 串行），
    但保险起见仍包 _OCR_DAEMON_LOCK，杜绝未来并发时的 stdin 写串。
    读取用独立线程 + queue 实现带超时的 readline：
      - 超时 → kill daemon（下次请求自动重建），返回错误而不是永久卡死；
      - daemon 读到 EOF 说明 svc 退出，daemon 自行结束（无孤儿进程）。
    """
    with _OCR_DAEMON_LOCK:
        proc = _daemon_start()
        if proc is None:
            return {"ok": False, "error": "ocr_daemon_start_failed"}
        try:
            line = json.dumps(cmd, ensure_ascii=False).encode("utf-8") + b"\n"
            proc.stdin.write(line)
            proc.stdin.flush()
        except Exception as e:
            _daemon_kill()
            return {"ok": False, "error": "ocr_daemon_stdin_fail: %s" % e}
        q = queue.Queue()

        def _reader():
            try:
                raw = proc.stdout.readline()
            except Exception:
                raw = b""
            q.put(raw)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            raw = q.get(timeout=timeout)
        except queue.Empty:
            _daemon_kill()
            return {"ok": False, "error": "ocr_daemon_timeout_%ss" % timeout}
        if not raw:
            _daemon_kill()
            return {"ok": False, "error": "ocr_daemon_eof"}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            _daemon_kill()
            return {"ok": False, "error": "ocr_daemon_bad_json"}


def _ascii_tmp_dir():
    """v1.5.0：cv2.imwrite 在非 ASCII 路径上会直接返回 False——中文账户的 TEMP 目录
    正好是这种（用户名是中文），表现为 OCR 报 img_write_fail。
    所以给 OCR 临时图挑一个一定全 ASCII 且可写的目录。"""
    bases = [os.environ.get("ProgramData") or "C:/ProgramData",
             "C:/salmon_tmp", tempfile.gettempdir()]
    for base in bases:
        try:
            d = os.path.join(base, "salmon_tmp")
            if any(ord(c) > 127 for c in d):
                continue
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, "w.tmp")
            with open(probe, "wb") as f:
                f.write(b"x")
            os.remove(probe)
            return d
        except Exception:
            continue
    return None


_OCR_TMP = _ascii_tmp_dir()


def _run_ocr_worker(img, max_side=1280, timeout=_OCR_TIMEOUT_DEFAULT):
    """把图片交给常驻 OCR daemon 做 OCR，返回 {ok,count,items} 或 {ok:False,error}。
    items 坐标为图片本地坐标系（已按缩放还原），屏幕偏移由调用方叠加。
    图片走临时 png 文件（daemon 与 svc 同机，文件 IO ~几 ms，远小于传输 base64 的开销）。
    """
    fd, path = tempfile.mkstemp(suffix=".png", prefix="ocr_", dir=_OCR_TMP)
    os.close(fd)
    try:
        if not cv2.imwrite(path, img):
            return {"ok": False, "error": "img_write_fail",
                    "hint": "cv2.imwrite 失败，当前临时目录 %s" % (_OCR_TMP,)}
        data = _daemon_request({"img": path, "max_side": int(max_side)}, timeout=timeout)
        return data if isinstance(data, dict) else {"ok": False, "error": "bad_daemon_reply"}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _grab(region=None, monitor=None):
    # mss 不是线程安全的：并发 grab 会拿到串味的画面甚至崩进程
    with _OP_LOCK:
        with _MSS_CLS() as sct:
            # mss 返回 BGRA → 只取前 3 通道即得 BGR（cv2 原生顺序）。
            # ⚠️ 修 2026-09-15：这里曾多写一步 [:, :, ::-1]（把 BGR 反成 RGB），
            #    而下游 op_color 按 B 读通道 0、op_shot/op_shot_pw 又统一做 BGR→RGB，
            #    导致「所有截图与取色 R/B 互换」（蓝显示成橙），并且 find 模板匹配
            #    拿 RGB 去和 cv2.imread 的 BGR 模板比对而静默失配。此处去反相即全链自洽。
            if region is not None:
                left, top, w, h = region
                shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
                return np.array(shot)[:, :, :3].copy(), left, top
            mon = sct.monitors[monitor] if monitor is not None else sct.monitors[0]
            shot = sct.grab(mon)
            return np.array(shot)[:, :, :3].copy(), mon["left"], mon["top"]


def _maybe_downsample(img, max_side=1280):
    """OCR 在大图上仍可能慢。强制把长边压到 max_side 以下，坐标按比例还原回原图坐标系。
    实测 3096x2064 全屏 OCR ~3s；1600x1067 ~1.8s；1280x853 ~1.4s。再小对精度有损，默认 1280。"""
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img, 1.0
    s = max_side / float(max(h, w))
    new_w = max(1, int(round(w * s)))
    new_h = max(1, int(round(h * s)))
    return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA), s


def _img_is_black(img, thresh=8):
    """判断截图是否全黑（显示器睡眠 / 被遮挡时 mss 会返回黑图）。"""
    if img is None or img.size == 0:
        return True
    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA)
    return float(small.mean()) < thresh


# ============================ 命令实现 ============================
def op_ping(a):
    return {"ok": True, "pong": True, "pid": os.getpid(),
            "uptime_s": round(_now() - _BOOT_TS, 1),
            "ocr_daemon": (_OCR_DAEMON is not None and _OCR_DAEMON.poll() is None),
            "inflight": _OP_INFLIGHT,
            "last_op_age_s": round(_now() - _LAST_OP_TS, 1),
            # 第 6 轮：让下一轮会话一眼看到"上次有没有窗口忘了还原"
            "stashed_windows": len(_WIN_SNAP),
            "allow_front": ALLOW_FRONT[0], "front_suppressed_times": FRONT_SUPPRESSED[0],
            "stashed": [{"hwnd": h, "reason": s.get("reason", ""),
                         "held_s": round(_now() - s.get("ts", _now()), 1)}
                        for h, s in _WIN_SNAP.items()]}


def op_shot(a):
    region = common.parse_region(a.get("region")) if a.get("region") else None
    img, oleft, otop = _grab(region=region, monitor=a.get("monitor"))
    if a.get("require_content") and _img_is_black(img):
        return {"ok": False, "error": "screenshot_is_black",
                "hint": "mss 拿到全黑（显示器睡眠/窗口被遮挡），改用 shot-pw（PrintWindow）或唤醒屏幕"}
    out = a.get("out")
    if not out:
        shots = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shots")
        os.makedirs(shots, exist_ok=True)
        out = os.path.join(shots, "shot_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    else:
        d = os.path.dirname(os.path.abspath(out))
        if d:
            os.makedirs(d, exist_ok=True)
    Image.fromarray(img[:, :, ::-1].copy()).save(out)
    h, w = img.shape[:2]
    return {"ok": True, "path": out.replace("\\", "/"), "width": w, "height": h,
            "left": oleft, "top": otop, "black": _img_is_black(img)}


def _printwindow(hwnd):
    """PrintWindow + GetDIBits 抓窗口内容，不依赖 z-order / 显示器唤醒状态。"""
    import ctypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    r = RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    hwndDC = user32.GetWindowDC(int(hwnd))
    if not hwndDC:
        return None
    mfcDC = gdi32.CreateCompatibleDC(hwndDC)
    bmp = gdi32.CreateCompatibleBitmap(hwndDC, w, h)
    gdi32.SelectObject(mfcDC, bmp)
    # PW_RENDERFULLCONTENT = 0x2（Win8.1+，能抓到硬件加速/分层窗口内容）
    user32.PrintWindow(int(hwnd), mfcDC, 0x00000002)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = (ctypes.c_uint8 * (w * h * 4))()
    got = gdi32.GetDIBits(mfcDC, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mfcDC)
    user32.ReleaseDC(int(hwnd), hwndDC)
    if not got:
        return None
    img = Image.frombytes("RGBA", (w, h), bytes(buf), "raw", "BGRA").convert("RGB")
    return np.array(img)[:, :, ::-1].copy(), r.left, r.top   # RGB->BGR


def op_shot_pw(a):
    """PrintWindow 抓指定窗口（hwnd 必填）。mss 黑屏时的救命方案。"""
    hwnd = a.get("hwnd")
    if not hwnd:
        return {"ok": False, "error": "hwnd required"}
    res = _printwindow(hwnd)
    if res is None:
        return {"ok": False, "error": "printwindow_failed"}
    img, oleft, otop = res
    out = a.get("out")
    if not out:
        shots = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shots")
        os.makedirs(shots, exist_ok=True)
        out = os.path.join(shots, "pw_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    else:
        d = os.path.dirname(os.path.abspath(out))
        if d:
            os.makedirs(d, exist_ok=True)
    Image.fromarray(img[:, :, ::-1].copy()).save(out)
    h, w = img.shape[:2]
    return {"ok": True, "path": out.replace("\\", "/"), "width": w, "height": h,
            "left": oleft, "top": otop, "black": _img_is_black(img)}


def op_shot_wgc(a):
    """WGC 抓指定窗口（hwnd 必填）：抗遮挡 + 屏幕物理像素 1:1 + 每帧只几 ms。
    比 shot-pw 更便宜更清晰（DPI 无关窗口 pw 只印出左上 1/4，实测 buffer 1072x878/内容 536x431）。"""
    hwnd = a.get("hwnd")
    if not hwnd:
        return {"ok": False, "error": "hwnd required"}
    r, err = _wgc_grab(int(hwnd))
    if r is None:
        return {"ok": False, "error": "wgc_failed", "detail": err,
                "hint": "没装 windows-capture 就 pip install windows-capture；"
                        "窗口最小化要先 win-front；仍失败则退回 shot-pw"}
    img, oleft, otop, age = r
    out = a.get("out")
    if not out:
        shots = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shots")
        os.makedirs(shots, exist_ok=True)
        out = os.path.join(shots, "wgc_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    else:
        d = os.path.dirname(os.path.abspath(out))
        if d:
            os.makedirs(d, exist_ok=True)
    Image.fromarray(img[:, :, ::-1].copy()).save(out)
    h, w = img.shape[:2]
    return {"ok": True, "path": out.replace("\\", "/"), "width": w, "height": h,
            "left": oleft, "top": otop, "frame_age_ms": age, "black": _img_is_black(img)}


def _ocr_items(img, oleft, otop, max_side=1280, ocr_timeout=None):
    """OCR 识别。截图在进程内完成；推理交常驻 ocr_worker daemon（避免 onnxruntime
    在 socketserver 请求线程内卡死）。缓存命中直接复用（同一画面轮询时提速）。

    返回 (items, used_ms, cached, ocr_err)：
      - items:  命中的文字项（含屏幕偏移）
      - ocr_err: None=成功；字符串=OCR 引擎失败（与"没找到字"严格区分，不做静默失败）
    """
    ms = int(max_side or 1280)
    to = float(ocr_timeout) if ocr_timeout else _OCR_TIMEOUT_DEFAULT
    key = (_md5(img.tobytes()), ms)
    with _OCR_LOCK:
        hit = _OCR_CACHE.get(key)
    if hit and _now() - hit[0] < 120:
        items = [_add_offset(it, oleft, otop) for it in hit[1]]
        return items, 0.0, True, None
    t0 = _now()
    data = _run_ocr_worker(img, max_side=ms, timeout=to)
    used = round((_now() - t0) * 1000)
    if not data or not data.get("ok"):
        # 不缓存失败；错误语义透给上层（调用方会报 OCR 失败而非"文字不存在"）
        err = (data or {}).get("error") or "ocr_unknown_fail"
        print("[ocr] worker fail: %s" % err, file=sys.stderr, flush=True)
        return [], used, False, err
    raw_items = data.get("items") or []
    # 淘汰超龄缓存
    now = _now()
    stale = [k for k, (ts, _) in _OCR_CACHE.items() if now - ts > 120]
    for k in stale:
        _OCR_CACHE.pop(k, None)
    if len(_OCR_CACHE) >= _OCR_CACHE_MAX:
        _OCR_CACHE.clear()
    with _OCR_LOCK:
        _OCR_CACHE[key] = (now, raw_items)
    items = [_add_offset(it, oleft, otop) for it in raw_items]
    return items, used, False, None


def _add_offset(it, oleft, otop):
    """把图片本地坐标 + 屏幕偏移，还原成物理屏幕坐标。"""
    return {
        "text": it["text"],
        "confidence": it["confidence"],
        "x": int(it["x"]) + oleft,
        "y": int(it["y"]) + otop,
        "left": int(it["left"]) + oleft,
        "top": int(it["top"]) + otop,
        "width": int(it["width"]),
        "height": int(it["height"]),
    }


def op_ocr(a):
    region = common.parse_region(a.get("region")) if a.get("region") else None
    form_used = "screen"
    if a.get("form") or a.get("hwnd"):
        img, oleft, otop, form_used, _n = _capture(a)
        if img is None:
            return {"ok": False, "error": "capture_failed", "detail": _n}
    else:
        img, oleft, otop = _grab(region=region, monitor=a.get("monitor"))
    items, ms, cached, err = _ocr_items(img, oleft, otop,
                                        max_side=a.get("max_side"),
                                        ocr_timeout=a.get("ocr_timeout"))
    if err:
        return {"ok": False, "error": "ocr_failed", "detail": err, "ocr_ms": ms}
    return {"ok": True, "count": len(items), "ocr_ms": ms, "cached": cached,
            "form": form_used, "items": items}


def _norm(s):
    import re
    return re.sub(r"[\s\u3000]", "", s).casefold()


def _text_hits(items, a):
    """从 OCR 项里按 text 过滤（locate-text / click-text / locate 共用，行为一致）。"""
    target = _norm(a.get("text", ""))
    exact = bool(a.get("exact"))
    hits = []
    for it in items:
        cand = _norm(it["text"])
        if (cand == target) if exact else (target in cand):
            hits.append(dict(it))
    hits.sort(key=lambda h: (-h["confidence"], -len(h["text"])))
    return hits


def _locate(a):
    """找文字。返回 (hits, ocr_ms, cached, ocr_err)。ocr_err 非 None = OCR 引擎失败。

    v1.3.0：--hwnd 时先（默认，--front 0 可关）把窗口拉到最上端，并直接在
    PrintWindow 窗口图上找字——被遮挡时屏幕上是别人的画面。
    v1.3.0 修的两处冲突：
      1) PrintWindow 失败（提权/UIPI/部分 GPU 合成窗口）时**退回屏幕路径**，
         不再直接报 printwindow_failed 把老行为打断；
      2) 只有目标不在前台时才 _force_front——否则 wait-text --hwnd 这种轮询
         会每 0.6 秒抢一次前台，共享桌面上属于骚扰。"""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = a.get("hwnd")
    if hwnd:
        hwnd = int(hwnd)
        if a.get("front", True) and user32.GetForegroundWindow() != hwnd:
            _force_front(hwnd, settle=float(a.get("settle", 0.2)))
        res = _printwindow(hwnd) if a.get("pw", True) else None
        if res:
            img, oleft, otop = res
            items, ms, cached, err = _ocr_items(img, oleft, otop,
                                                max_side=a.get("max_side"),
                                                ocr_timeout=a.get("ocr_timeout"))
            return _text_hits(items, a), ms, cached, err
        # PrintWindow 不可用 → 继续走下面的屏幕路径（老行为兜底）
    region = common.parse_region(a.get("region")) if a.get("region") else None
    t0 = _now()
    img, oleft, otop = _grab(region=region, monitor=a.get("monitor"))
    t_grab = _now() - t0
    t1 = _now()
    items, ms, cached, err = _ocr_items(img, oleft, otop,
                                        max_side=a.get("max_side"),
                                        ocr_timeout=a.get("ocr_timeout"))
    t_ocr = _now() - t1
    # 调试慢的环节
    if (t_grab + t_ocr) > 0.5:
        print("[locate] grab=%.0fms ocr=%.0fms img=%dx%d region=%s"
              % (t_grab*1000, t_ocr*1000, img.shape[1], img.shape[0], region),
              file=sys.stderr, flush=True)
    return (_text_hits(items, a) if not err else []), ms, cached, err
def op_locate_text(a):
    hits, ms, cached, err = _locate(a)
    if err:
        return {"ok": False, "error": "ocr_failed", "detail": err, "text": a.get("text"),
                "hint": "OCR 引擎失败，不是没找到字。先 ping 看 worker 状态，稍后重试"}
    if not hits:
        return {"ok": False, "error": "text_not_found", "text": a.get("text"),
                "ocr_ms": ms, "hint": "缩小 --region 或先 shot 看实际画面"}
    return {"ok": True, "count": len(hits), "ocr_ms": ms, "cached": cached, "matches": hits}


def op_click_text(a):
    """一步到位：找文字 → 点它（省掉 2 次进程往返）。"""
    hits, ms, cached, err = _locate(a)
    if err:
        return {"ok": False, "error": "ocr_failed", "detail": err, "text": a.get("text"),
                "hint": "OCR 引擎失败，不是没找到字。先 ping 看 worker 状态，稍后重试"}
    if not hits:
        return {"ok": False, "error": "text_not_found", "text": a.get("text"), "ocr_ms": ms}
    h = hits[0]
    off_x = int(a.get("offset_x", 0))
    off_y = int(a.get("offset_y", 0))
    x, y = h["x"] + off_x, h["y"] + off_y
    btn = a.get("button", "left")
    cnt = int(a.get("count", 1))
    pyautogui.click(x, y, clicks=cnt, button=btn)
    return {"ok": True, "clicked": {"x": x, "y": y, "text": h["text"],
                                    "confidence": h["confidence"], "button": btn, "count": cnt},
            "ocr_ms": ms, "cached": cached, "candidates": len(hits)}


def op_wait_text(a):
    """轮询等文字出现（替代盲 sleep）。元素一出现立即返回。"""
    timeout = float(a.get("timeout", 15))
    interval = float(a.get("interval", 0.6))
    t0 = _now()
    tries = 0
    while _now() - t0 < timeout:
        hits, ms, _, err = _locate(a)
        tries += 1
        if err:
            # OCR 引擎失败：连错 2 次即放弃，不空等到超时（避免"接而不答"式挂起）
            if tries >= 2:
                return {"ok": False, "error": "ocr_failed_waiting_text",
                        "detail": err, "text": a.get("text"), "tries": tries}
        elif hits:
            return {"ok": True, "found": True, "elapsed_s": round(_now() - t0, 2),
                    "tries": tries, "matches": hits, "ocr_ms": ms}
        time.sleep(interval)
    return {"ok": False, "error": "timeout_waiting_text", "text": a.get("text"),
            "timeout": timeout, "tries": tries}


def op_wait_text_gone(a):
    timeout = float(a.get("timeout", 15))
    interval = float(a.get("interval", 0.6))
    t0 = _now()
    tries = 0
    while _now() - t0 < timeout:
        hits, _, _, err = _locate(a)
        tries += 1
        if err:
            if tries >= 2:
                return {"ok": False, "error": "ocr_failed_waiting_text_gone",
                        "detail": err, "text": a.get("text"), "tries": tries}
        elif not hits:
            return {"ok": True, "gone": True, "elapsed_s": round(_now() - t0, 2),
                    "tries": tries}
        time.sleep(interval)
    return {"ok": False, "error": "timeout_waiting_text_gone", "text": a.get("text")}


def op_wait_stable(a):
    """等画面稳定：连续 N 次采样画面 hash 一致即认为加载完成（等编译/加载的神器）。

    给 --hwnd 时只看那个窗口（pw/wgc），别的地儿重绘不打扰它 ——
    共享桌面上（别的代理在动鼠标、别处有进度条）整屏 region 判稳是误判大户。"""
    region = common.parse_region(a.get("region")) if a.get("region") else None
    hwnd = a.get("hwnd")
    timeout = float(a.get("timeout", 30))
    need = int(a.get("stable_times", 3))
    interval = float(a.get("interval", 0.8))
    last = None
    same = 0
    t0 = _now()
    while _now() - t0 < timeout:
        if hwnd:
            img, _, _, fm, nte = _capture({"hwnd": int(hwnd), "form": a.get("form", "pw")})
            if img is None:
                return {"ok": False, "error": "wait_stable_no_image", "form": fm,
                        "detail": nte, "hint": "先恢复窗口（win-front）或改用 --region"}
        else:
            img, _, _ = _grab(region=region, monitor=a.get("monitor"))
            fm = "screen"
        h = _md5(cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA).tobytes())
        if h == last:
            same += 1
            if same >= need:
                return {"ok": True, "stable": True, "elapsed_s": round(_now() - t0, 2),
                        "form": fm}
        else:
            same = 1
            last = h
        time.sleep(interval)
    return {"ok": False, "error": "timeout_waiting_stable", "timeout": timeout, "form": fm,
           "note": "整个 timeout 内没抓到连续 %d 次同图：%s" % (
               need, "目标确实一直在变（这通常就是答案）" if hwnd else "屏幕上这块区域一直在变")}


# ---------- 模板匹配 find（找图标，不依赖 OCR） ----------
def _load_template(path):
    if not os.path.exists(path):
        return None, "模板文件不存在: %s" % path
    try:
        img = Image.open(path)
    except Exception as e:
        return None, "模板打开失败: %s" % e
    if img.mode == "RGBA":
        # 半透明模板合成到白底，避免透明像素参与匹配
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return np.array(img), None   # HxWx3 RGB


def op_find(a):
    """模板匹配找图（找图标用，不依赖 OCR）。与 locate-text 互补。

    参数：--template 小图路径（必填） [--confidence 0.8] [--region x,y,w,h]
          [--monitor N] [--scales 0.9,1.0,1.1] [--max 1]
    返回 matches: [{x,y,confidence,scale,bbox}]（中心坐标=物理像素，已加屏幕偏移）
    """
    tpl_path = a.get("template")
    if not tpl_path:
        return {"ok": False, "error": "template required"}
    tpl, terr = _load_template(tpl_path)
    if terr:
        return {"ok": False, "error": terr}
    th, tw = tpl.shape[:2]
    if tw <= 0 or th <= 0:
        return {"ok": False, "error": "template size invalid"}

    scales = []
    raw_scales = a.get("scales")
    if raw_scales:
        for s in str(raw_scales).split(","):
            s = s.strip()
            try:
                scales.append(float(s))
            except ValueError:
                return {"ok": False, "error": "--scales 需为逗号分隔的数字，如 0.9,1.0,1.1"}
    if not scales:
        scales = [1.0]

    region = common.parse_region(a.get("region")) if a.get("region") else None
    img, oleft, otop = _grab(region=region, monitor=a.get("monitor"))
    if _img_is_black(img) and a.get("hwnd"):
        res = _printwindow(a["hwnd"])
        if res:
            img, oleft, otop = res
    sh, sw = img.shape[:2]
    confidence = float(a.get("confidence", 0.8))
    maxn = int(a.get("max", 1))

    t0 = _now()
    all_hits = []
    for scale in scales:
        if abs(scale - 1.0) < 1e-6:
            tpl2 = tpl
        else:
            tpl2 = cv2.resize(tpl, (max(1, int(tw * scale)), max(1, int(th * scale))),
                              interpolation=cv2.INTER_AREA)
        th2, tw2 = tpl2.shape[:2]
        if th2 > sh or tw2 > sw:
            continue
        res = cv2.matchTemplate(img, tpl2, cv2.TM_CCOEFF_NORMED)
        for _ in range(maxn):
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            if maxv < confidence or not np.isfinite(maxv):
                break
            mx, my = maxloc[0], maxloc[1]
            all_hits.append({
                "x": int(mx + tw2 / 2.0) + oleft,
                "y": int(my + th2 / 2.0) + otop,
                "confidence": round(float(maxv), 4),
                "scale": scale,
                "bbox": [oleft + mx, otop + my, tw2, th2],
            })
            res[my:my + th2, mx:mx + tw2] = -np.inf
        if maxn <= 1 and all_hits:
            break

    # 去重：中心距离 < 12px 视为同一目标，保留高分
    all_hits.sort(key=lambda h: -h["confidence"])
    dedup = []
    for h in all_hits:
        dup = any(abs(h["x"] - d["x"]) < 12 and abs(h["y"] - d["y"]) < 12 for d in dedup)
        if not dup:
            dedup.append(h)
        if len(dedup) >= maxn:
            break
    used = round((_now() - t0) * 1000)
    if not dedup:
        return {"ok": False, "error": "template_not_found",
                "template_size": [tw, th], "find_ms": used,
                "hint": "降 --confidence 或换更小的不透明模板；先 shot 看目标是否在区域内"}
    return {"ok": True, "found": len(dedup), "matches": dedup,
            "template_size": [tw, th], "find_ms": used}


# ---------- 鼠标 / 键盘 ----------
def op_move(a):
    x, y = int(a["x"]), int(a["y"])
    pyautogui.moveTo(x, y, duration=float(a.get("duration", 0)))
    return {"ok": True, "x": x, "y": y}


# ---- v1.5.0：相对鼠标移动 ----
# 为什么不能用 pyautogui.moveTo：它发的是 MOUSEEVENTF_ABSOLUTE|VIRTUALDESK，
# 游戏（DirectInput / Raw Input）只读"相对位移"，于是绝对定位对视角完全无效。
# 这里直接 SendInput 一条不带 ABSOLUTE 标志的 MOUSEEVENTF_MOVE。
class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("extra", ctypes.c_void_p)]


class _II(ctypes.Union):
    _fields_ = [("mi", _MI), ("pad", ctypes.c_byte * 32)]


class _SI(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", _II)]


_MI_MOVE = 0x0001


def _send_rel(dx, dy):
    inp = _SI()
    inp.type = 0                      # INPUT_MOUSE
    inp.ii.mi.dx = int(dx)
    inp.ii.mi.dy = int(dy)
    inp.ii.mi.dwFlags = _MI_MOVE      # 只给 MOVE，不给 ABSOLUTE
    return int(ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_SI)))


def op_move_rel(a):
    """相对移动鼠标 = 转视角。--dx 120 --dy -40 [--steps 6 --interval 0.02]"""
    dx, dy = int(a.get("dx", 0)), int(a.get("dy", 0))
    steps = max(1, int(a.get("steps", 1)))
    gap = float(a.get("interval", 0.0))
    sent = 0
    for _ in range(steps):
        sent += _send_rel(float(dx) / steps, float(dy) / steps)
        if gap:
            time.sleep(gap)
    cur = pyautogui.position()
    return {"ok": True, "dx": dx, "dy": dy, "steps": steps, "sent": sent,
            "cursor": [cur.x, cur.y], "sizeof_INPUT": ctypes.sizeof(_SI)}


def op_mouse_hold(a):
    """按住鼠标键若干秒再松（挖矿/蓄力）。--button left --hold 1.5"""
    btn = a.get("button", "left")
    hold = max(0.0, float(a.get("hold", 0.5)))
    pyautogui.mouseDown(button=btn)
    time.sleep(hold)
    pyautogui.mouseUp(button=btn)
    return {"ok": True, "button": btn, "hold_s": hold}


def op_click(a):
    x, y = a.get("x"), a.get("y")
    btn = a.get("button", "left")
    cnt = int(a.get("count", 1))
    interval = float(a.get("interval", 0.0))
    if x is None or y is None:
        pyautogui.click(clicks=cnt, button=btn, interval=interval)
        cur = pyautogui.position()
        x, y = cur.x, cur.y
    else:
        if str(a.get("via", "send")).lower() == "post":
            if not a.get("hwnd"):
                return {"ok": False, "error": "via=post 需要 --hwnd"}
            _post_click(int(a["hwnd"]), int(x), int(y), button=btn, count=cnt)
            return {"ok": True, "x": int(x), "y": int(y), "button": btn, "count": cnt,
                    "via": "post", "hwnd": int(a["hwnd"])}
        pyautogui.click(int(x), int(y), clicks=cnt, button=btn, interval=interval)
    return {"ok": True, "x": int(x), "y": int(y), "button": btn, "count": cnt, "via": "send"}


def op_drag(a):
    pyautogui.moveTo(int(a["x1"]), int(a["y1"]))
    pyautogui.drag(int(a["x2"]) - int(a["x1"]), int(a["y2"]) - int(a["y1"]),
                   duration=float(a.get("duration", 0.3)), button=a.get("button", "left"))
    return {"ok": True, "from": [a["x1"], a["y1"]], "to": [a["x2"], a["y2"]]}


def op_scroll(a):
    x, y = a.get("x"), a.get("y")
    if x is not None and y is not None:
        pyautogui.scroll(int(a["amount"]), x=int(x), y=int(y))
    else:
        pyautogui.scroll(int(a["amount"]))
    return {"ok": True, "amount": int(a["amount"])}


def op_type(a):
    # 兜底转字符串：调用方若把纯数字文本当 int 传进来（老版 mc.py 的 _coerce 会这样），
    # pyperclip.copy(int) 会抛异常、回退的 pyautogui.write(int) 再抛 TypeError。
    text = a.get("text", "")
    text = "" if text is None else str(text)
    if str(a.get("via", "send")).lower() == "post":
        if not a.get("hwnd"):
            return {"ok": False, "error": "via=post 需要 --hwnd"}
        h = int(a["hwnd"])
        for ch in text:
            _post_char(h, ch)
            time.sleep(0.004)
        return {"ok": True, "typed": len(text), "method": "wm_char", "via": "post"}
    try:
        import pyperclip
        old = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        pyperclip.copy(old)
        return {"ok": True, "typed": len(text), "method": "clipboard"}
    except Exception:
        pyautogui.write(text, interval=0.01)
        return {"ok": True, "typed": len(text), "method": "write"}


def op_key(a):
    # v1.3.1：--via post --hwnd N → 后台投递，不抢前台；默认仍是 SendInput（行为不变）
    if str(a.get("via", "send")).lower() == "post":
        if not a.get("hwnd"):
            return {"ok": False, "error": "via=post 需要 --hwnd"}
        ok, note = _post_key(int(a["hwnd"]), a["key"], down=True)
        if not ok:
            return {"ok": False, "error": note}
        _post_key(int(a["hwnd"]), a["key"], down=False)
        return {"ok": True, "key": a["key"], "via": "post", "hwnd": int(a["hwnd"])}
    pyautogui.press(a["key"])
    return {"ok": True, "key": a["key"], "via": "send"}


def op_hotkey(a):
    keys = [k.strip() for k in a["keys"].split(",") if k.strip()]
    pyautogui.hotkey(*keys)
    return {"ok": True, "keys": keys}


def op_pos(a):
    p = pyautogui.position()
    return {"ok": True, "x": p.x, "y": p.y, "size": list(pyautogui.size())}


def op_color(a):
    img, oleft, otop = _grab(region=(int(a["x"]), int(a["y"]), 1, 1))
    b, g, r = int(img[0, 0, 0]), int(img[0, 0, 1]), int(img[0, 0, 2])
    return {"ok": True, "hex": "#%02X%02X%02X" % (r, g, b), "rgb": [r, g, b]}


# ---------- 窗口管理（封装了这次踩的所有坑） ----------
def op_win_list(a):
    """列出窗口。注意：内部已 SetProcessDpiAwareness(2)，坐标是物理像素。"""
    import ctypes
    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    LP = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    GetWindowTextW = user32.GetWindowTextW
    GetWindowTextLengthW = user32.GetWindowTextLengthW

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    keyword = a.get("keyword", "")
    pid_filter = a.get("pid")
    out = []

    def cb(h, _):
        n = GetWindowTextLengthW(h)
        if n <= 0 or n > 400:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(h, buf, n + 1)
        title = buf.value
        if keyword and keyword not in title:
            return True
        if pid_filter:
            p = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value != int(pid_filter):
                return True
        r = RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        out.append({
            "hwnd": h, "pid": int(p.value), "title": title,
            "left": r.left, "top": r.top,
            "width": r.right - r.left, "height": r.bottom - r.top,
            "visible": bool(user32.IsWindowVisible(h)),
            "iconic": bool(user32.IsIconic(h)),
        })
        return True

    EnumWindows(LP(cb), 0)
    return {"ok": True, "count": len(out), "windows": out}


def op_win_activate(a):
    """安全前置窗口。

    关键（血泪教训）：
      - 绝不用 SetWindowPos(HWND_TOPMOST=-1) 再切回 HWND_TOP=0：
        这会把窗口压到 z-order 底层，之后 mss 永远抓不到，看起来像"黑屏"。
      - 正确做法：ShowWindow(SW_RESTORE) + SetForegroundWindow + BringWindowToTop。
      - Windows 前台锁定：后台进程调 SetForegroundWindow 偶尔被系统拒
        （is_foreground=false），紧接重试一次通常即成 —— 已内置自动重试。
    """
    # v1.3.0：改走 _force_front（AttachThreadInput 强拉，前台锁也能过）
    hwnd = int(a["hwnd"])
    if not ALLOW_FRONT[0]:
        return {"ok": False, "error": "front_policy_denied", "allow_front": False,
                "hint": "已禁止抢前台。看内容改 --form pw / --form wgc，出手改 --via post（都不需要前台）；"
                        "确实要前台就 mc.py front-policy --allow 1（或先征得用户同意）"}
    settle = float(a.get("settle", 0.25))
    retries = int(a.get("retry", 2))  # 含首次
    last_fg = False
    for _ in range(max(1, retries)):
        last_fg, _ = _force_front(hwnd, settle=settle)
        if last_fg:
            break
    import ctypes
    r = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, r, 256)
    return {"ok": True, "hwnd": hwnd, "title": r.value,
            "is_foreground": last_fg}


# ---------- 窗口「快照 / 还原」（第 6 轮：解决"操控完窗口找不回来"） ----------
# 血泪背景：为了让 OCR 拍到目标窗口，需要把 WorkBuddy 挪开/隐藏。但此前
# win-move / win-hide 是"单向"的——做完活没还原，用户回到电脑前发现
# WorkBuddy 不见了（挪到屏幕外）或打不开（被压成全屏大小盖住一切）。
#
# 设计：
#   * _WIN_SNAP：进程内快照表 {hwnd: {x,y,w,h,zoomed,iconic,hidden}}
#     —— 放在 svc 进程内存里，svc 一重启就清空，不写磁盘。
#   * win-stash  ：快照 + 隐藏（隐藏比挪到屏幕外安全：负坐标在高 DPI 屏上
#                  仍可能露出一角，而且用户根本猜不到窗口在哪）
#   * win-restore：按快照还原几何 + 恢复显示 + 若是最大化则恢复最大化
#   * win-restore-all：还原所有还在快照表里的窗口（收尾兜底）
#   * 不做定时自动还原：会打断长流程。改为「收尾必调 restore-all」的约定，
#     并把清单挂在 ping 上（_stashed 字段），让下一轮会话能发现上次没还原。

_WIN_SNAP = {}


def _win_snapshot(hwnd):
    """读取窗口当前几何与状态，返回 dict（同时用于打快照和回报状态）。"""
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32 = ctypes.windll.user32
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return {
        "x": rc.left, "y": rc.top,
        "w": rc.right - rc.left, "h": rc.bottom - rc.top,
        "zoomed": bool(user32.IsZoomed(hwnd)),
        "iconic": bool(user32.IsIconic(hwnd)),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "maximized": bool(user32.IsZoomed(hwnd)),
    }


def op_win_stash(a):
    """把窗口「寄存在一边」：先记录它原本的几何/状态，再隐藏。

    这是操控期间的**推荐做法**（优于 win-move 挪到屏幕外）：
      - 隐藏后窗口完全不参与绘制，OCR/截图绝不会拍到它抢焦点
      - 还原只需 win-restore，不依赖"记住我挪到哪个负坐标了"
      - 即使用户中途重启 svc，窗口仍由系统保持隐藏，任务栏点一下就能回来

    参数：
      --hwnd <h>          （必填）要隐藏的窗口
      --reason "..."      （可选）备注，便于 ping 时看清是谁藏的

    返回快照内容，便于调用方核对。
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = int(a["hwnd"])
    if not user32.IsWindow(hwnd):
        return {"ok": False, "error": "invalid_hwnd", "hwnd": hwnd}
    # 已经藏过就别覆盖快照（否则会把"隐藏状态"当成原始状态存进去，还原不回来）
    if hwnd not in _WIN_SNAP:
        _WIN_SNAP[hwnd] = _win_snapshot(hwnd)
        _WIN_SNAP[hwnd]["reason"] = a.get("reason", "")
        _WIN_SNAP[hwnd]["ts"] = _now()
    user32.ShowWindow(hwnd, 0)            # SW_HIDE
    time.sleep(0.05)
    return {"ok": True, "hwnd": hwnd, "stashed": True,
            "snapshot": _WIN_SNAP[hwnd],
            "note": "用 win-restore / win-restore-all 还原"}


def op_win_restore(a):
    """按快照把单个窗口还原回原样（几何 + 显示 + 最大化状态）。

    不传 --hwnd 时还原**全部**已寄存窗口（等价 win-restore-all）。
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnds = []
    if "hwnd" in a and a["hwnd"]:
        hwnds = [int(a["hwnd"])]
    else:
        hwnds = list(_WIN_SNAP.keys())
    if not hwnds:
        return {"ok": True, "restored": 0, "note": "没有寄存中的窗口"}

    out = []
    for hwnd in hwnds:
        snap = _WIN_SNAP.get(hwnd)
        if not snap:
            # 没有快照：至少保证窗口可见（不能凭空猜原尺寸）
            if user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, 9)     # SW_RESTORE
                out.append({"hwnd": hwnd, "ok": True, "note": "无快照，仅恢复显示"})
            else:
                out.append({"hwnd": hwnd, "ok": False, "error": "window_gone"})
            continue
        if not user32.IsWindow(hwnd):
            out.append({"hwnd": hwnd, "ok": False, "error": "window_gone"})
            _WIN_SNAP.pop(hwnd, None)
            continue
        # 1) 先显示 + 还原（最大化窗口必须先 SW_RESTORE 才挪得动）
        user32.ShowWindow(hwnd, 9)             # SW_RESTORE
        time.sleep(0.08)
        # 2) 恢复几何（除非原本就是最大化，那就不动几何、最后再最大化）
        if not snap.get("maximized"):
            flags = 0x0004 | 0x0010            # SWP_NOZORDER | SWP_NOACTIVATE
            user32.MoveWindow(hwnd, int(snap["x"]), int(snap["y"]),
                              max(1, int(snap["w"])), max(1, int(snap["h"])), True)
            time.sleep(0.05)
        # 3) 原本最大化 / 最小化的再还原回去
        if snap.get("maximized"):
            user32.ShowWindow(hwnd, 3)         # SW_MAXIMIZE
        elif snap.get("iconic"):
            user32.ShowWindow(hwnd, 6)         # SW_MINIMIZE
        # 4) 原本就没显示的（窗口本来就被隐藏）→ 还原后仍隐藏
        if not snap.get("visible"):
            user32.ShowWindow(hwnd, 0)         # SW_HIDE
            time.sleep(0.05)
        after = _win_snapshot(hwnd)
        out.append({"hwnd": hwnd, "ok": True, "restored_to": after,
                    "was": {k: snap[k] for k in ("x", "y", "w", "h", "maximized", "visible")}})
        _WIN_SNAP.pop(hwnd, None)

    return {"ok": all(o["ok"] for o in out), "restored": len(out), "windows": out}


def op_win_restore_all(a):
    """还原全部已寄存窗口（收尾兜底，自动化流程最后一步必调）。"""
    return op_win_restore({})


def op_win_stash_list(a):
    """列出当前还"寄存着没还原"的窗口（排查用，也会挂在 ping 里）。"""
    now = _now()
    items = []
    for hwnd, s in _WIN_SNAP.items():
        items.append({"hwnd": hwnd, "reason": s.get("reason", ""),
                      "held_s": round(now - s.get("ts", now), 1),
                      "orig": {k: s[k] for k in ("x", "y", "w", "h")}})
    return {"ok": True, "count": len(items), "stashed": items}


def op_win_hide(a):
    """直接隐藏窗口（不打快照）。

    ⚠️ 优先用 win-stash：win-hide 没有快照，win-restore 只能做到"恢复显示"，
    无法还原原始位置和尺寸。保留它是为了兼容旧调用。
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = int(a["hwnd"])
    user32.ShowWindow(hwnd, 0)            # SW_HIDE
    return {"ok": True, "hwnd": hwnd, "hidden": True,
            "note": "未打快照；还原请用 win-show（仅恢复显示）"}


def op_win_show(a):
    """把 win-hide 藏起来的窗口重新显示（SW_SHOW + SW_RESTORE + BringWindowToTop）。

    配套 win-hide 使用：藏了记得放回来，否则窗口只能靠任务栏手动恢复。
    """
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = int(a["hwnd"])
    user32.ShowWindow(hwnd, 5)            # SW_SHOW
    user32.ShowWindow(hwnd, 9)            # SW_RESTORE（曾被最小化的也能拉回）
    user32.BringWindowToTop(hwnd)
    return {"ok": True, "hwnd": hwnd, "shown": True}


def op_win_move(a):
    """把窗口整体挪走 / 挪回（MoveWindow）。

    ★ 存在意义（§0.5.4 教训 1）：操控会话期间 WorkBuddy 会抢前台，导致 OCR 读到的
      "按钮"全是聊天记录文字。文档要求"把 WorkBuddy MoveWindow 移出屏幕（x=-2400）"，
      但此前没有任何 op 能做这件事 —— 这里补上。

    用法：
      win-move --hwnd <h> --x -2400 --y 0        # 挪到屏幕外（不占前台、用户看不见）
      win-move --hwnd <h> --x 0 --y 0            # 挪回左屏
      win-move --hwnd <h> --x 100 --y 100 --w 1600 --h 1200   # 顺便改尺寸

    不传 --w/--h 时保持原尺寸（读 GetWindowRect 补全），不会把窗口压成 0x0。
    挪到负坐标是允许的：窗口仍在 z-order 里、不抢焦点，只是画在屏幕外。

    ★ 坑 1：**最大化的窗口直接 MoveWindow 会被系统立刻弹回原位**。
    ★ 坑 2（第 6 轮实测，更要命）：**对最大化窗口先 SW_RESTORE(9) 再 MoveWindow
      也不行** —— WorkBuddy 这类 Electron 应用收到尺寸变化后会**自己重新最大化**，
      实测坐标纹丝不动、IsZoomed 仍为 True。
      **正确解法：SW_NORMAL(1) 取消最大化 → 等 300ms → SetWindowPos(带
      SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED) 移动**。SW_NORMAL 不改
      z-order 也不激活，应用不会触发重新最大化；SetWindowPos 比 MoveWindow
      对"移动+改尺寸"的原子性更好。实测 WorkBuddy 3118x1966(最大化) →
      1600x1500@(60,60) 一次成功。
    """
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32 = ctypes.windll.user32
    hwnd = int(a["hwnd"])
    # 首次移动前打快照，这样 win-restore 能把窗口摆回原位
    if hwnd not in _WIN_SNAP:
        _WIN_SNAP[hwnd] = _win_snapshot(hwnd)
        _WIN_SNAP[hwnd]["reason"] = a.get("reason", "win-move")
        _WIN_SNAP[hwnd]["ts"] = _now()
    # 取消最大化 / 还原最小化：用 SW_NORMAL(1)，**不是** SW_RESTORE(9)
    was_zoomed = bool(user32.IsZoomed(hwnd)) or bool(user32.IsIconic(hwnd))
    if was_zoomed:
        user32.ShowWindow(hwnd, 1)        # SW_NORMAL：取消最大化且不触发重新最大化
        time.sleep(0.3)                   # 给应用一点时间处理 WM_SIZE
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    cur_w = rc.right - rc.left
    cur_h = rc.bottom - rc.top
    x = int(a["x"])
    y = int(a["y"])
    w = int(a.get("w", cur_w))
    h = int(a.get("h", cur_h))
    if w <= 0:
        w = cur_w
    if h <= 0:
        h = cur_h
    flags = 0x0004 | 0x0010        # SWP_NOZORDER | SWP_NOACTIVATE（绝不抢焦点、绝不动 z-order）
    ok = bool(user32.MoveWindow(hwnd, x, y, w, h, True))
    time.sleep(0.05)
    rc2 = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc2))
    return {"ok": ok, "hwnd": hwnd, "x": rc2.left, "y": rc2.top,
            "width": rc2.right - rc2.left, "height": rc2.bottom - rc2.top,
            "offscreen": rc2.right <= 0 or rc2.left >= user32.GetSystemMetrics(78)}


def op_monitors(a):
    """列出所有显示器几何信息（多屏用）。mss.monitors[0]=全部屏幕联合；主显示器 index=1。"""
    with _MSS_CLS() as sct:
        items = []
        for idx, m in enumerate(sct.monitors):
            items.append({
                "index": idx,
                "left": m["left"], "top": m["top"],
                "width": m["width"], "height": m["height"],
                "is_primary": (idx == 1),
            })
    return {"ok": True, "monitors": items,
            "scale": round(common.system_dpi_scale(), 3) if hasattr(common, "system_dpi_scale") else None}


def op_wake(a):
    """唤醒可能睡眠的显示器。

    v1.2.1 实测结论（2026-09-07 晚 1h23m 马拉松实测）：
      - pyautogui 移鼠标 / keybd_event / mouse_event / SC_MONITORPOWER 广播 对 DPMS 睡眠全部无效；
      - 唯一有效 = SendInput 硬件级键盘事件（F15 + Space + A）。
    这里按"SendInput 优先，pyautogui 兜底"顺序执行，且调用后可继续往下走
    （SetThreadExecutionState 只是防止再睡，醒屏由 SendInput 完成）。"""
    import ctypes
    user32 = ctypes.windll.user32
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _I(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("i",)
        _fields_ = [("type", ctypes.c_ulong), ("i", _I)]

    def send_key(vk):
        inp = INPUT(); inp.type = INPUT_KEYBOARD; inp.ki.wVk = vk
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        inp.ki.dwFlags = KEYEVENTF_KEYUP
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    sent = []
    for name, vk in (("F15", 0x7E), ("space", 0x20), ("A", 0x41)):
        try:
            send_key(vk)
            sent.append(name)
        except Exception as e:
            sent.append("%s:%s" % (name, type(e).__name__))
        time.sleep(0.15)
    # 防再睡（仅当前进程生命周期内生效，退出即失效，无副作用）
    try:
        ES_CONTINUOUS = 0x80000000
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
    except Exception:
        pass
    # 兜底：pyautogui 轻移鼠标
    try:
        sx, sy = pyautogui.size()
        cx, cy = sx // 2, sy // 2
        pyautogui.moveTo(cx, cy, duration=0.05)
        pyautogui.moveRel(2, 0, duration=0.02)
    except Exception:
        pass
    return {"ok": True, "waked": True, "method": "SendInput", "keys": sent,
            "note": "DPMS 睡眠须 SendInput 唤醒；若仍黑需人工碰一下鼠标/键盘"}


def op_batch(a):
    """一次调用顺序执行多个动作，省掉 N 次往返。

    steps 支持两种写法：
      - 真数组: [{"op":"shot","out":"..."}, ...]
      - JSON 字符串: '[{"op":"shot","out":"..."}]'   （命令行 --json 传参时会自动是字符串）
    stop_on_error: 默认 true。
    """
    # 命令行 --json '[...]' 传进来键名是 json，这里一并接受
    steps = a.get("steps") or a.get("json") or []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception as e:
            return {"ok": False, "error": "steps_json_parse_failed: %s" % e}
    if not isinstance(steps, list):
        return {"ok": False, "error": "steps must be list or json string"}
    stop = a.get("stop_on_error", True)
    results = []
    for i, st in enumerate(steps):
        st = dict(st)
        op = st.pop("op", None)
        if not op:
            results.append({"i": i, "ok": False, "error": "missing op"})
            if stop:
                break
            continue
        fn = HANDLERS.get(op)
        if not fn:
            results.append({"i": i, "ok": False, "error": "unknown op", "op": op})
            if stop:
                break
            continue
        try:
            r = fn(st)
            r["i"] = i
            r["op"] = op
            results.append(r)
            if stop and not r.get("ok"):
                break
        except Exception as e:
            results.append({"i": i, "op": op, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e)})
            if stop:
                break
    return {"ok": all(r.get("ok") for r in results), "steps": len(steps), "results": results}


# ===================== v1.3.1：双形态（捕获形态 × 出手形态）自适应 =====================
# 动机（2026-10-02 网页小游戏实测）：两种捕获形态各有硬优势，没有一种通吃——
#   screen（mss 抓屏）：最快（21–43ms），且能抓到 GPU 合成后的真实画面；但被遮挡时
#            拿到的是遮挡物的像素，且要求目标可见。
#   pw（PrintWindow）：抗遮挡、可后台（40ms 左右）；但提权/UIPI 窗口会失败，
#            DirectX/部分 GPU 合成窗口可能黑帧或拿到旧帧。
# 出手同理：send（SendInput）要前台；post（PostMessage）不要前台、不抢焦点，
#            但个别应用（尤其自带输入栈的）会忽略合成消息。
# 所以做成"形态可显式指定，默认 auto 自动挑"，任何一形态的优势都不会因改造而丢失。

_VK = {"backspace": 8, "tab": 9, "enter": 13, "return": 13, "shift": 16, "control": 17,
       "ctrl": 17, "alt": 18, "menu": 18, "pause": 19, "capslock": 20, "escape": 27,
       "esc": 27, "space": 32, "pageup": 33, "prior": 33, "pagedown": 34, "next": 34,
       "end": 35, "home": 36, "left": 37, "up": 38, "right": 39, "down": 40,
       "printscreen": 44, "insert": 45, "delete": 46, "del": 46, "win": 91, "meta": 91,
       "contextmenu": 93, "apps": 93, "numlock": 144, "scrolllock": 145}
for _i in range(1, 13):
    _VK["f%d" % _i] = 111 + _i


def _vk_of(key):
    """键名 → (虚拟键码, 是否需要 shift)。单字符走 VkKeyScanW，覆盖字母数字符号。"""
    import ctypes
    k = str(key).lower()
    if k in _VK:
        return _VK[k], 0
    if len(k) == 1:
        r = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(k))
        if r == -1 or r == 32767:
            return None, None
        return (r & 0xFF), ((r >> 8) & 0xFF)
    return None, None


def _key_lparam(vk, down):
    import ctypes
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    lp = 1 | (scan << 16)
    if not down:
        lp |= 0xC0000000
    return lp


def _post_key(hwnd, key, down=True):
    """PostMessage 投递按键：不需要前台、不抢焦点。"""
    import ctypes
    u = ctypes.windll.user32
    vk, shl = _vk_of(key)
    if vk is None:
        return False, "unknown_key:%s" % key
    lp = _key_lparam(vk, down)
    if shl & 1:
        svk = 0x10  # VK_SHIFT
        u.PostMessageW(hwnd, 0x0100 if down else 0x0101, svk, _key_lparam(svk, down))
    u.PostMessageW(hwnd, 0x0100 if down else 0x0101, vk, lp)
    if down:
        # 让需要字符输入的应用也拿到 WM_CHAR
        u.PostMessageW(hwnd, 0x0102, vk, lp)
    return True, "posted"


def _post_char(hwnd, ch):
    import ctypes
    ctypes.windll.user32.PostMessageW(hwnd, 0x0102, ord(ch), 0)


def _post_click(hwnd, x, y, button="left", count=1):
    """PostMessage 点击：屏幕坐标自动转客户区坐标。"""
    import ctypes
    u = ctypes.windll.user32
    p = ctypes.wintypes.POINT(int(x), int(y))
    u.ScreenToClient(hwnd, ctypes.byref(p))
    lp = ((p.y & 0xFFFF) << 16) | (p.x & 0xFFFF)
    msg = {"left": (0x0201, 0x0202, 0x0001), "right": (0x0204, 0x0205, 0x0002),
           "middle": (0x0207, 0x0208, 0x0010)}.get(button, (0x0201, 0x0202, 0x0001))
    down, up, flag = msg
    for _ in range(max(1, int(count))):
        u.PostMessageW(hwnd, down, flag, lp)
        u.PostMessageW(hwnd, up, 0, lp)
        time.sleep(0.012)
    return True, {"client": [p.x, p.y], "button": button, "count": int(count)}


def _occluded(hwnd):
    """z-order 真遮挡判定（v1.3.5 新增）。

    ⚠️ 为什么要有这条：`_visible_unoccluded` 名字里写着 unoccluded，实现却只比较了
    `GetForegroundWindow() == hwnd`，实测一块布明明盖在目标上它仍然报 True。
    auto 规则以前一直把"是否前台"当"是否被遮挡"用 —— 名字骗了人，判断也跟着错。

    做法：从目标往 z-order 上方逐层走（GW_HWNDPREV=3），只要有"别的、可见的、
    矩形和目标相交"的窗口，就判被压。两类跳过：目标自己拥有的子窗（GW_OWNER==hwnd，
    含工具窗）、以及 layered 窗口（salmon 自己的 banner 就是分层置顶窗，不算遮挡源）。
    """
    import ctypes
    u = ctypes.windll.user32
    if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
        return True
    R = ctypes.wintypes.RECT
    me = R(); u.GetWindowRect(hwnd, ctypes.byref(me))
    if me.right - me.left < 8 or me.bottom - me.top < 8:
        return True
    above = u.GetWindow(hwnd, 3)
    hops = 0
    while above and hops < 300:
        hops += 1
        if above != hwnd and u.IsWindowVisible(above):
            ex = u.GetWindowLongW(above, -20)                  # GWL_EXSTYLE
            layered = bool(ex & 0x00080000)                    # WS_EX_LAYERED
            owner = u.GetWindow(above, 4)                      # GW_OWNER
            if not layered and owner != hwnd:
                ar = R(); u.GetWindowRect(above, ctypes.byref(ar))
                if (ar.left < me.right and ar.right > me.left
                        and ar.top < me.bottom and ar.bottom > me.top):
                    return True
        above = u.GetWindow(above, 3)
    return False


def _visible_unoccluded(hwnd):
    """目标是否真的在屏幕上可见（前台且未被别窗口压）。用矩形面积+前台判断，够快。"""
    import ctypes
    u = ctypes.windll.user32
    if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
        return False
    r = ctypes.wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    if (r.right - r.left) < 8 or (r.bottom - r.top) < 8:
        return False
    return u.GetForegroundWindow() == hwnd


# ---------------------------------------------------------------------------
# 第三捕获形态 WGC（Windows Graphics Capture，Win10 1903+ 系统接口）
# 从 DWM 合成层拿「这一个窗口」的画面：
#   vs 抓屏 —— 被遮挡时看到的仍是目标本身（抓屏看到的是遮挡物，这是 v1.3.1 之前
#              「遮挡了点不了」的根因）；
#   vs PrintWindow —— 不让窗口重绘所以更便宜，并且给的是屏幕物理像素。
#              实测 DPI 无关窗口在 200% 缩放下 PrintWindow 只填满缓冲区的左上 1/4
#              （buffer 1072x878 / 内容 536x431，挡板宽 120px 而屏幕上是 240px），
#              WGC 是 1:1 满帧。
# WGC 是推送式（画面更新才回调），所以按 hwnd 常驻会话 + 单槽最新帧，取帧近乎零成本；
# 代价是「帧龄」—— 所以返回值里带上 age，别让上层把"读到旧帧"当成低延迟。
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# WGC（Windows Graphics Capture）第三捕获形态 —— v1.3.6 起走独立 worker
#
# 为什么要 worker：windows-capture 2.0.1 在 start_free_threaded() 里会 access violation
# 把宿主进程整个打死（实测无 Python 堆栈，只有 PYTHONFAULTHANDLER 看得见）。挪进子进程后
# 崩的只是 worker，svc 检测 EOF/退出码就重建，任务不断。这和 ocr_worker.py 当年为
# onnxruntime 做的隔离是同一招。
#
# 为什么值这个复杂度：WGC 是三通路里唯一同时做到「抗遮挡 + 屏幕物理像素 1:1 + 取帧 ~0.4ms」的
# （实测 mss 遮挡时红球 0 px；pw 抗遮挡但 DPI 无关窗口上只印左上 1/4 且 17-30ms；
#  wgc 帧尺寸与 DWMWA_EXTENDED_FRAME_BOUNDS 逐像素相等，与 screen 通路给回同一个坐标 dist=0.0）。
# ---------------------------------------------------------------------------
import struct as _struct
from multiprocessing import shared_memory as _shm_mod

_WGC_HDR = _struct.Struct("<IQIIQQ")     # magic, seq, w, h, t_ns, frames
_WGC_MAGIC = 0x53574743
_WGC_DATAOFF = 64
_WGC_AUTO_MAX_AGE_MS = 120.0             # auto 只接受比这更新的 wgc 帧，否则退回 pw
_WGW = {"proc": None, "shm": None, "name": None, "hwnd": None,
        "cap_frame": 0, "dead": 0}


def _wgc_available():
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wgc_worker.py"))


def _wgw_close_shm():
    if _WGW["shm"] is not None:
        try:
            _WGW["shm"].close()
        except Exception:
            pass
    _WGW["shm"] = None
    _WGW["name"] = None


def _wgw_kill():
    p = _WGW["proc"]
    if p is not None:
        try:
            p.kill()
        except Exception:
            pass
    _WGW["proc"] = None
    _WGW["hwnd"] = None
    _wgw_close_shm()


def _wgw_line_timeout(p, timeout=8.0):
    """worker 回复一行；带超时（用线程取，避免 svc 请求线程被永久挂住）。"""
    import queue
    import threading
    q = queue.Queue()

    def _rd():
        try:
            q.put(p.stdout.readline())
        except Exception as e:
            q.put(None)
    t = threading.Thread(target=_rd, daemon=True)
    t.start()
    t.join(timeout)
    if not q.qsize():
        return None
    return q.get()


def _wgw_cmd(obj, timeout=8.0, respawn=True):
    """发一条命令给 worker 并取回一行 JSON。返回 (dict|None, err)。worker 不在就拉起。"""
    import json as _json
    import subprocess
    import sys
    p = _WGW["proc"]
    if p is None or p.poll() is not None:
        if not respawn:
            return None, "worker_not_running"
        if p is not None:
            _WGW["dead"] += 1              # 上一任是被 access violation 打死的，这里记一笔
        _wgw_close_shm()
        _WGW["hwnd"] = None
        try:
            p = subprocess.Popen(
                [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "wgc_worker.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # worker 的 stderr 落到 TEMP 日志：windows-capture 是原生崩溃，
                # 不给个落点就永远查不到死因（本轮踩过一次"静默死亡"）
                stderr=open(os.path.join(tempfile.gettempdir(), "salmon_wgc_worker.log"), "ab"),
                text=True, bufsize=1, creationflags=0x08000000)   # CREATE_NO_WINDOW
        except Exception as e:
            return None, "worker_spawn_failed: %s" % str(e)[:100]
        _WGW["proc"] = p
    try:
        p.stdin.write(_json.dumps(obj) + "\n")
        p.stdin.flush()
    except Exception as e:
        _wgw_kill()
        return None, "worker_write_failed: %s" % str(e)[:100]
    line = _wgw_line_timeout(p, timeout)
    if line is None:
        _wgw_kill()
        return None, "worker_timeout_or_died"
    try:
        return _json.loads(line), ""
    except Exception as e:
        _wgw_kill()
        return None, "worker_bad_reply: %s" % str(e)[:80]


def _wgc_release():
    """换窗口/窗口没了时调用：只停会话，进程留着复用（拉起一次 ~0.3s）。"""
    if _WGW["proc"] is not None and _WGW["proc"].poll() is None:
        # 必须把 stop 的回复读掉：不读的话它堵在管道里，下一条命令会把它当自己的答复
        # （实测错位成 shm_open_failed: 'name'，看着像共享内存问题，其实是协议串了）
        _wgw_cmd({"cmd": "stop"}, timeout=4.0, respawn=False)
    _WGW["hwnd"] = None
    _wgw_close_shm()


def _ext_bounds(hwnd):
    """DWMWA_EXTENDED_FRAME_BOUNDS —— WGC 抓的正是这一块，坐标对齐靠它。"""
    import ctypes

    class _R(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    r = _R()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(int(hwnd)), 9,
                                                    ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0 or r.r <= r.l or r.b <= r.t:
        return None
    return r.l, r.t, r.r - r.l, r.b - r.t


def _wgc_grab(hwnd):
    """返回 ((img, oleft, otop, frame_age_ms), "") 或 (None, 原因)。
    读侧用 seqlock：seq 奇数=worker 正在写；读到偶数且读完再验一次没变，才算拿到完整一帧。"""
    import ctypes
    import time as _t
    import numpy as _np
    hwnd = int(hwnd)
    if not ctypes.windll.user32.IsWindow(hwnd):
        _wgc_release()
        return None, "window_gone"
    if not _wgc_available():
        return None, "wgc_worker_missing"
    eb = _ext_bounds(hwnd)
    if not eb:
        return None, "no_extended_bounds"
    if _WGW["hwnd"] != hwnd:
        r, err = _wgw_cmd({"cmd": "start", "hwnd": hwnd}, timeout=12.0)
        if err or not (r or {}).get("ok"):
            _wgc_release()
            return None, ("worker: " + (err or (r or {}).get("error", "?")))
        try:
            shm = _shm_mod.SharedMemory(name=r["name"])
        except Exception as e:
            return None, "shm_open_failed: %s" % str(e)[:80]
        _wgw_close_shm()
        _WGW["shm"] = shm
        _WGW["name"] = r["name"]
        _WGW["hwnd"] = hwnd
        _WGW["cap_frame"] = (shm.size - _WGC_DATAOFF) // 2
    buf = _WGW["shm"].buf
    _dbg_last = None
    for _ in range(4):
        try:
            head = _WGC_HDR.unpack(bytes(buf[:_WGC_HDR.size]))
        except Exception:
            return None, "shm_read_failed"
        magic, seq, w, h, t_ns, frames = head
        _dbg_last = "magic=%x seq=%d w=%d h=%d frames=%d cap=%d" % (
            magic, seq, w, h, frames, _WGW["cap_frame"])
        if magic != _WGC_MAGIC or w == 0 or frames == 0:
            _t.sleep(0.02)
            continue
        if w * h * 4 > _WGW["cap_frame"]:          # 窗口被拉大过，缓冲区不够 → 重建一次
            _wgc_release()
            return None, "wgc_resized_rebuild"
        if not (seq & 1):
            slot = ((seq // 2 - 1) % 2) * w * h * 4   # 与 worker 同一公式：slot 由帧号 fnew=seq//2 决定
            try:
                arr = _np.frombuffer(buf, dtype=_np.uint8, count=w * h * 4,
                                     offset=_WGC_DATAOFF + slot).reshape(h, w, 4)
                img = _np.ascontiguousarray(arr[:, :, :3])
            except Exception as e:
                return None, "shm_copy_failed: %s" % str(e)[:80]
            seq2 = _WGC_HDR.unpack(bytes(buf[:_WGC_HDR.size]))[1]
            if seq2 == seq:
                age = round((_t.monotonic_ns() - t_ns) / 1e6, 1)
                return (img, eb[0], eb[1], max(age, 0.0)), ""
        _t.sleep(0.002)
    # 拿不到稳定新帧，最常见是窗口被放大导致共享内存不够：释放会话，下一次按新矩形重建
    _wgc_release()
    return None, "wgc_no_fresh_frame(%s)" % (_dbg_last or "no header read")


def _wgc_frames(hwnd):
    """当前会话已交付的帧数（guard 用它区分「轮询次数」和「新画面次数」）。"""
    try:
        if _WGW["shm"] is None or _WGW["hwnd"] != int(hwnd):
            return -1
        head = _WGC_HDR.unpack(bytes(_WGW["shm"].buf[:_WGC_HDR.size]))
        return int(head[5])
    except Exception:
        return -1


def _capture_impl(a, default="auto"):
    """统一捕获入口。form: auto|screen|pw|wgc。返回 (img, oleft, otop, form_used, note)。"""
    import ctypes as _ct
    _u32 = _ct.windll.user32
    form = str(a.get("form", default) or default).lower()
    hwnd = a.get("hwnd")
    region = common.parse_region(a.get("region")) if a.get("region") else None
    mon = a.get("monitor")
    note = ""
    # 最小化的窗口不重绘，PrintWindow 也拿不到画面 —— 这不是"pw 失败"，
    # 而是必须先恢复。给可执行的错误，别让上层误判成形态问题（实测踩到）。
    if hwnd and _u32.IsIconic(int(hwnd)):
        return None, 0, 0, form, ("target_minimized: 先 win-front --hwnd %s 恢复再抓"
                                  "（最小化窗口不重绘，screen/pw/wgc 都拿不到有效画面）" % int(hwnd))
    if form == "pw":
        if not hwnd:
            return None, 0, 0, form, "hwnd required for form=pw"
        res = _printwindow(int(hwnd))
        if res is None:
            return None, 0, 0, form, "printwindow_failed"
        return res[0], res[1], res[2], "pw", ""
    if form == "wgc":
        if not hwnd:
            return None, 0, 0, form, "hwnd required for form=wgc"
        r, err = _wgc_grab(int(hwnd))
        if r is None:
            return None, 0, 0, form, "wgc_failed: " + err
        return r[0], r[1], r[2], "wgc", "frame_age=%.1fms" % r[3]
    if form == "screen":
        img, l, tp = _grab(region=region, monitor=mon)
        return img, l, tp, "screen", ""
    # auto 规则（v1.3.5：遮挡判据换成 z-order 实测，见 _occluded）：
    #   真被遮挡 / 窗口不可见     → pw（screen 此时拿到的是遮挡物）
    #   没给 region              → pw（抓整屏既慢又容易命中屏幕上别处的同名目标）
    #   未被遮挡且给了 region     → screen（最快，且是 GPU 合成后的真实画面；
    #                              以前"不是前台"就被赶去 pw，白白多花 7-20ms）
    #   pw 失败或黑帧            → 退回 screen
    # 注意：auto 只在"这个窗口已经开着 wgc 会话、且帧够新"时才用 wgc，不会主动为它开会话
    # （建会话要 0.3s，且换窗口就得拆掉重建 —— 一个 svc 进程只能持有一个会话）。
    if hwnd:
        occ = _occluded(int(hwnd))            # 真遮挡，不是"是否前台"
        if occ or (region is None):
            # 已有热会话时 wgc 严格优于 pw（0.4ms vs 17-30ms，且满分辨率、坐标 1:1）；
            # 冷会话不自动开（要 0.3s 建会话，且 windows-capture 崩溃史就摆在那），
            # 需要 wgc 就显式 --form wgc。
            if occ and _WGW["hwnd"] == int(hwnd):
                g, _e = _wgc_grab(int(hwnd))
                # 只在该帧确实是"刚刚"的时候用：wgc 是推送式，会话一旦停更（窗口没重绘、
                # worker 卡住、hwnd 被回收），头部 seq/frames 就冻住，此时拿到的是一张
                # 看起来正常的旧图。循环场景（guard）里这比慢一点严重得多。
                if g is not None and g[3] <= _WGC_AUTO_MAX_AGE_MS:
                    return g[0], g[1], g[2], "wgc", ("auto:目标被遮挡且 wgc 会话已热，"
                                                     "取帧 %.1fms（pw 要 17-30ms）" % g[3])
                if g is not None:
                    note = ("auto:wgc 会话停在 %.0fms 前的旧帧（超过 %.0fms 阈值），改走 pw"
                            % (g[3], _WGC_AUTO_MAX_AGE_MS))
            res = _printwindow(int(hwnd))
            if res is not None and not _img_is_black(res[0]):
                return res[0], res[1], res[2], "pw", (note or ("auto:目标被遮挡" if occ
                                                               else "auto:未给 region，按窗口图更准"))
            note = "pw 不可用/黑帧，退回抓屏"
        else:
            img, l, tp = _grab(region=region, monitor=mon)
            return img, l, tp, "screen", "auto:z-order 实测未被遮挡，抓屏最快"
    img, l, tp = _grab(region=region, monitor=mon)
    return img, l, tp, "screen", note


def _capture(a, default="auto"):
    """薄封装：记下这次实际用了哪个形态，供 _advise 判断（它要多问一句「为什么没找到」）。"""
    out = _capture_impl(a, default)
    _LAST_CAP.clear()
    _LAST_CAP.update({"form": out[3], "hwnd": a.get("hwnd"), "region": bool(a.get("region")),
                      "req": _REQ_SEQ[0]})
    return out


def op_probe(a):
    """形态诊断：三种捕获各测一次，告诉你现在该用哪个形态、为什么。
    参数：--hwnd N [--region x,y,w,h]"""
    import ctypes
    u = ctypes.windll.user32
    hwnd = a.get("hwnd")
    out = {"ok": True, "form_screen_ms": None, "form_pw_ms": None, "form_wgc_ms": None,
           "visible_unoccluded": None, "is_foreground": None, "recommended": "screen"}
    region = common.parse_region(a.get("region")) if a.get("region") else None
    t0 = _now()
    img, l, tp = _grab(region=region, monitor=a.get("monitor"))
    out["form_screen_ms"] = round((_now() - t0) * 1000, 1)
    out["screen_black"] = _img_is_black(img)
    if hwnd:
        hwnd = int(hwnd)
        out["is_foreground"] = (u.GetForegroundWindow() == hwnd)
        out["occluded"] = _occluded(hwnd)                      # z-order 实测
        out["visible_unoccluded"] = _visible_unoccluded(hwnd)  # 保留字段；实为"是否前台"，见 compat_note
        # 诊断默认不新建 WGC 会话：windows-capture 的 start_free_threaded 是那个
        # access violation 的发生地，probe 不该替模型承担这个风险。
        # 已有会话（说明这次任务真的在用 wgc）才报耗时/帧龄；要探路请显式 --wgc_probe 1。
        was_open = (_WGW.get("hwnd") == hwnd)
        probe_it = bool(a.get("wgc_probe"))
        if was_open or probe_it:
            t0 = _now()
            r, werr = _wgc_grab(hwnd)
            out["form_wgc_ms"] = round((_now() - t0) * 1000, 1)
            out["wgc_ok"] = bool(r)
            if r is None:
                out["wgc_error"] = werr
            else:
                out["wgc_cold"] = not was_open
                out["wgc_frame_age_ms"] = r[3]
                out["wgc_frames"] = _wgc_frames(hwnd)
        else:
            other = _WGW["hwnd"]
            out["wgc_ok"] = False
            out["wgc_state"] = ("busy_by_%d" % other) if other else ("worker_missing" if not _wgc_available() else "not_probed")
            out["form_wgc_ms"] = None
        t0 = _now()
        res = _printwindow(hwnd)
        out["form_pw_ms"] = round((_now() - t0) * 1000, 1)
        out["pw_ok"] = bool(res)
        out["pw_black"] = bool(res and _img_is_black(res[0]))
        if res:
            # DPI 无关窗口在 >100% 缩放下，PrintWindow 只把内容画在缓冲区左上角，
            # 其余全黑 —— 图尺寸是对的，内容却是半分辨率，坐标必须乘 scale 才不点歪。
            # 实测 buffer 1072x878 / 内容 536x431（挡板宽 120，屏幕上实为 240）。
            pa = np.asarray(res[0])[..., :3]
            pys, pxs = np.nonzero(pa.sum(axis=2) > 6)
            if len(pxs):
                cw_, ch_ = int(pxs.max() + 1), int(pys.max() + 1)
                sc_ = round(cw_ / max(int(pa.shape[1]), 1), 3)
                out["pw_content"] = [cw_, ch_, int(pa.shape[1]), int(pa.shape[0])]
                out["pw_scale_est"] = sc_
                out["pw_half_resolution"] = sc_ < 0.75
                if out["pw_half_resolution"]:
                    out["pw_fix"] = "pw 图上的坐标要乘 1/%.2f 才是屏幕坐标；要免换算就用 --form wgc（物理像素 1:1）" % sc_
        if not out["occluded"]:
            out["recommended"] = "screen"
        else:
            out["recommended"] = ("wgc" if out["wgc_ok"] and (out.get("wgc_frame_age_ms") or 9e9) <= _WGC_AUTO_MAX_AGE_MS
                                  else ("pw" if (out["pw_ok"] and not out["pw_black"]) else "screen"))
        out["compat_note"] = ("visible_unoccluded 这个字段名有误导性：它实际判断的是"
                              "\"是否前台\"(GetForegroundWindow==hwnd)，一块布盖着它也报 True。"
                              "真正的遮挡判断看新字段 occluded（z-order 实测）。auto 规则 v1.3.5 起用 occluded。")
        if out["wgc_ok"]:
            wgc_desc = "已建会话，取帧 %.1fms，帧龄 %.1fms" % (out["form_wgc_ms"], out.get("wgc_frame_age_ms", -1))
        else:
            wgc_desc = "%s%s" % (out.get("wgc_state", ""),
                                 ("(" + str(out.get("wgc_error", ""))[:60] + ")") if out.get("wgc_error") else "")
        out["wgc_note"] = ("wgc：%s。它比 pw 便宜一个数量级且给屏幕物理像素 1:1；"
                           "v1.3.6 起它跑在独立 worker 进程里（windows-capture 2.0.1 新建会话时会 "
                           "access violation，打死的是 worker，svc 自愈重建，实测 10ms）。"
                           "一个 svc 只持一个会话，所以 auto 只在\"会话已经热着且帧龄<%.0fms\"时用它，"
                           "否则仍走 pw；要给一个新窗口开会话就显式 --form wgc 或 --wgc_probe 1"
                           % (wgc_desc, _WGC_AUTO_MAX_AGE_MS))
    out["note"] = ("目标在前台 → 用 screen（更快且是真实合成画面）；目标不可见/被遮挡 → 用 pw（抗遮挡，"
                   "auto 就走这条）。pw 在 DPI 无关窗口上只印出左上 1/4（实测 buffer 1072x878/内容 536x431），"
                   "要完整清晰画面就显式 --form wgc 单窗口盯着。"
                   "出手形态同理：要后台静默操作用 --via post，应用忽略合成消息时退回 --via send。")
    return out


# ===================== v1.3.0：连续动作 / 遮挡自愈置顶 / 颜色定位 / 反射回路 =====================
# 设计背景（2026-10-02 贪吃蛇+场景矩阵实测驱动）：
#   1) 音游/动作游戏要"按住"和"高频"，原 op_key 只有 press（按下即松开）；
#   2) 被遮挡时 click-text 点不中（屏幕上是遮挡物），需要先把窗口拉到最上端，
#      含从任务栏恢复；前台锁拒绝时用 AttachThreadInput 强拉；
#   3) find 模板匹配在纯色小块上会给 confidence=1.0 的假命中（实测踩到），
#      游戏定位必须用颜色掩码（find-color）；
#   4) "自我操控游戏"要求感知-决策-出手在本地闭环，模型不参与逐帧 → guard。

def op_key_down(a):
    """按住某个键（直到 key-up）。蓄力/持续移动/音游长按。"""
    pyautogui.keyDown(a["key"])
    return {"ok": True, "key": a["key"], "state": "down"}


def op_key_up(a):
    """松开某个键。"""
    pyautogui.keyUp(a["key"])
    return {"ok": True, "key": a["key"], "state": "up"}


def op_mouse_down(a):
    """按住鼠标键（拖拽蓄力/连点前的按住）。默认左键。"""
    btn = a.get("button", "left")
    pyautogui.mouseDown(button=btn)
    return {"ok": True, "button": btn, "state": "down"}


def op_mouse_up(a):
    """松开鼠标键。"""
    btn = a.get("button", "left")
    pyautogui.mouseUp(button=btn)
    return {"ok": True, "button": btn, "state": "up"}


def op_key_hold(a):
    """按住 key 若干秒再松开（一次调用完成，不用拆 key-down + sleep + key-up）。
    参数：--key w --hold 0.5"""
    key = a["key"]
    hold = float(a.get("hold", 0.5))
    pyautogui.keyDown(key)
    time.sleep(max(0.0, hold))
    pyautogui.keyUp(key)
    return {"ok": True, "key": key, "hold_s": hold}


def op_sleep(a):
    """在 batch 里占位等待（配合连点间隔、动画等待）。
    参数：--seconds 0.2 或 --ms 200"""
    sec = float(a.get("seconds", 0)) or float(a.get("ms", 0)) / 1000.0
    sec = min(sec, 30.0)
    time.sleep(sec)
    return {"ok": True, "slept_s": round(sec, 3)}


# ---- 抢前台策略（v1.3.6）----
# 这台机器上常同时有别的模型在用前台。用户说"别抢前台"时，不该靠代理记住"哪个 op 会抢"
# （实测 wait-text --hwnd 这种看起来人畜无害的轮询也会自动前置），所以做成服务级硬开关：
#   启动前 SALMON_ALLOW_FRONT=0 ，或运行时 mc.py front-policy --allow 0
# 关掉后：自动前置一律跳过（改用 pw/wgc 看 + post 出手），显式的 win-front 直接拒绝并说明怎么办。
ALLOW_FRONT = [os.environ.get("SALMON_ALLOW_FRONT", "1").strip().lower()
               not in ("0", "false", "no", "off")]
FRONT_SUPPRESSED = [0]      # 本进程内被策略挡下来的自动前置次数


def op_front_policy(a):
    """查/改「能不能抢前台」策略。--allow 1|0（默认 1，即历史行为）。
    关掉后：任何 op 的自动前置一律跳过，win-front/win-activate 直接拒绝并给出后台替代路径。"""
    v = a.get("allow")
    if v is not None:
        ALLOW_FRONT[0] = str(v).strip().lower() not in ("0", "false", "no", "off")
    return {"ok": True, "allow_front": ALLOW_FRONT[0], "suppressed_times": FRONT_SUPPRESSED[0],
            "persist": "想开机就这样：启动 svc 前设环境变量 SALMON_ALLOW_FRONT=0"}


def _force_front(hwnd, settle=0.25):
    """把窗口拉到最上端（被遮挡 / 从任务栏最小化都吃）。

    前台锁说明：后台进程直接 SetForegroundWindow 会被 Windows 前台锁忽略
    （2026-10-02 实测），正解是 AttachThreadInput 把当前线程与前台线程、
    目标窗口线程临时绑定，再激活。最小化窗口先 SW_RESTORE。
    返回 (is_foreground, was_iconic)。
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = int(hwnd)
    if not ALLOW_FRONT[0]:
        FRONT_SUPPRESSED[0] += 1
        return (user32.GetForegroundWindow() == hwnd), bool(user32.IsIconic(hwnd))
    was_iconic = bool(user32.IsIconic(hwnd))
    if was_iconic:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE：从任务栏拉回
        time.sleep(0.05)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_cur = kernel32.GetCurrentThreadId()
    att_fg = att_tgt = False
    try:
        if tid_fg and tid_fg != tid_cur:
            att_fg = bool(user32.AttachThreadInput(tid_cur, tid_fg, True))
        if tid_tgt and tid_tgt != tid_cur:
            att_tgt = bool(user32.AttachThreadInput(tid_cur, tid_tgt, True))
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if att_tgt:
            user32.AttachThreadInput(tid_cur, tid_tgt, False)
        if att_fg:
            user32.AttachThreadInput(tid_cur, tid_fg, False)
    time.sleep(settle)
    return (user32.GetForegroundWindow() == hwnd), was_iconic


def op_win_front(a):
    """把窗口拉到最上端（遮挡自愈 + 任务栏恢复）。带前台锁强拉与验证重试。
    参数：--hwnd N [--settle 0.25] [--retry 2]
    返回 is_foreground（三次仍失败会如实报 false）、was_iconic、title"""
    hwnd = int(a["hwnd"])
    if not ALLOW_FRONT[0]:
        return {"ok": False, "error": "front_policy_denied", "allow_front": False,
                "hint": "已禁止抢前台。看内容改 --form pw / --form wgc，出手改 --via post（都不需要前台）；"
                        "确实要前台就 mc.py front-policy --allow 1（或先征得用户同意）"}
    settle = float(a.get("settle", 0.25))
    retries = int(a.get("retry", 2))
    ok = False
    was_iconic = False
    for _ in range(max(1, retries)):
        ok, was_iconic = _force_front(hwnd, settle=settle)
        if ok:
            break
    import ctypes
    r = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(hwnd, r, 256)
    return {"ok": True, "hwnd": hwnd, "title": r.value,
            "is_foreground": ok, "was_iconic": was_iconic}


def op_find_color(a):
    """找指定颜色的实心色块——游戏/自绘 UI 的正确定位方式。
    实测（2026-10-02 贪吃蛇）：find 模板匹配在纯色小块上会 confidence=1.0 假命中，
    颜色掩码稳得多。参数：--rgb 255,45,45 [--tolerance 25] [--region x,y,w,h]
    [--monitor N] [--min_area 12] [--max 5] [--hwnd N（被遮挡时走 PrintWindow）]
    返回 matches: [{x,y,w,h,area}]（中心 = 屏幕物理坐标，已加偏移）。"""
    rgb = a.get("rgb")
    if not rgb:
        return {"ok": False, "error": "--rgb 必填，如 255,45,45"}
    try:
        parts = [int(p) for p in str(rgb).split(",")]
        if len(parts) != 3:
            raise ValueError
    except ValueError:
        return {"ok": False, "error": "--rgb 需为 r,g,b 三个数字"}
    r_, g_, b_ = parts
    tol = float(a.get("tolerance", 25))
    region = common.parse_region(a.get("region")) if a.get("region") else None
    t0 = _now()
    img, oleft, otop, used_form, _n = _capture(a)
    if img is None:
        return {"ok": False, "error": "capture_failed", "detail": _n}
    used_pw = (used_form in ("pw", "wgc"))   # 两者都是「窗口原点图」，坐标要加 oleft/otop
    # 边界必须显式 float64：max(0, 负数) 返回的是 int 0，会让 lo 退化成 int64 而 hi 仍是
    # float64，cv2.inRange 直接断言失败（lb.type()==ub.type()）。
    # 触发条件是"目标色接近黑色 + tolerance 略宽"——深色 UI/暗色主题正好中招。
    lo = np.array([max(0.0, b_ - tol), max(0.0, g_ - tol), max(0.0, r_ - tol)], dtype=np.float64)
    hi = np.array([min(255.0, b_ + tol), min(255.0, g_ + tol), min(255.0, r_ + tol)], dtype=np.float64)
    mask = cv2.inRange(img, lo, hi)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    min_area = float(a.get("min_area", 12))
    maxn = int(a.get("max", 5))
    out = []
    for i in range(1, n):
        x, y, w, h, ar = [int(v) for v in stats[i][:5]]
        if ar < min_area:
            continue
        out.append({"x": int(cent[i][0]) + oleft, "y": int(cent[i][1]) + otop,
                    "w": w, "h": h, "area": ar})
    out.sort(key=lambda t: -t["area"])
    return {"ok": True, "count": len(out[:maxn]), "matches": out[:maxn],
            "rgb": [r_, g_, b_], "tolerance": tol, "printwindow": used_pw,
            "form": used_form, "_ms": round((_now() - t0) * 1000, 1)}


def _eval_condition(cond, img, oleft, otop):
    """在一张 BGR 图上评估 guard 条件。返回 (hit, point)。"""
    if "rgb" in cond:
        r_, g_, b_ = [int(p) for p in str(cond["rgb"]).split(",")]
        tol = float(cond.get("tolerance", 25))
        lo = np.array([max(0.0, b_ - tol), max(0.0, g_ - tol), max(0.0, r_ - tol)], dtype=np.float64)
        hi = np.array([min(255.0, b_ + tol), min(255.0, g_ + tol), min(255.0, r_ + tol)], dtype=np.float64)
        mask = cv2.inRange(img, lo, hi)
        n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
        best = None
        for i in range(1, n):
            ar = int(stats[i][4])
            if ar >= float(cond.get("min_area", 12)) and (best is None or ar > best[2]):
                best = (int(cent[i][0]) + oleft, int(cent[i][1]) + otop, ar)
        return (best is not None), best
    if "text" in cond:
        items, ms, cached, err = _ocr_items(img, oleft, otop, max_side=cond.get("max_side"))
        if err:
            return False, None
        target = _norm(cond["text"])
        for it in items:
            if target and target in _norm(it["text"]):
                return True, (it["x"], it["y"])
        return False, None
    return False, None


def op_guard(a):
    """本地反射回路（模型不在环）：按 fps 反复 抓图 → 评估条件 → 执行步骤，
    命中或超时退出。音游/自绘 UI 自我操控的核心原语。

    参数：
      --condition '{"rgb":"255,45,45"}'  或  '{"text":"READY"}'（JSON，必填）
      --then    '[{"op":"key","key":"space"}]'    首次命中后执行一次（推荐）
      --every   '[...]'                            命中期间每帧执行
      --else    '[...]'                            未命中时每帧执行（慎用，可能轰炸输入）
      --region x,y,w,h    强烈建议带上：快 + 防止命中屏幕上别处的同名目标
      --fps 20 [--timeout 10] [--stop_on_found 1] [--hwnd N]
      --hwnd 与 PrintWindow 配合：被遮挡也能看（但出手仍需要前台，键走 SendInput）
    返回：hit / frames / elapsed_s / fps_real / last_point / steps_ran
    """
    cond = a.get("condition")
    if isinstance(cond, str):
        try:
            cond = json.loads(cond)
        except Exception as e:
            return {"ok": False, "error": "condition_json_parse_failed: %s" % e}
    if not isinstance(cond, dict) or not cond:
        return {"ok": False, "error": "--condition 必填，如 '{\"rgb\":\"255,45,45\"}' 或 '{\"text\":\"READY\"}'"}

    def _steps(key):
        v = a.get(key)
        if not v:
            return []
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                return []
        return v if isinstance(v, list) else []

    then_steps = _steps("then")
    every_steps = _steps("every")
    else_steps = _steps("else")
    fps = max(1.0, min(60.0, float(a.get("fps", 20))))
    timeout = float(a.get("timeout", 10))
    stop_on_found = a.get("stop_on_found", True)
    if isinstance(stop_on_found, str):
        stop_on_found = stop_on_found.lower() in ("1", "true", "yes")
    region = common.parse_region(a.get("region")) if a.get("region") else None
    hwnd = a.get("hwnd")
    interval = 1.0 / fps
    t_start = _now()
    frames = 0
    frames_new = 0        # 真正换了内容的帧数（WGC 是推送式，轮询次数 ≠ 新感知次数）
    last_form = None      # 最后一次实际用的捕获形态（auto 会在 pw/wgc 之间切换，得报出去）
    _last_seq = -1
    steps_ran = 0
    hit_ever = False
    last_point = None
    then_ran = False

    def _run(steps):
        nonlocal steps_ran
        for st in steps:
            st = dict(st)
            op = st.pop("op", None)
            fn = HANDLERS.get(op)
            if not fn:
                continue
            if st.get("via") == "post" and "hwnd" not in st and hwnd:
                st["hwnd"] = int(hwnd)   # guard 里写 --via post 不必每步重复 hwnd
            try:
                fn(st)
                steps_ran += 1
            except Exception:
                pass

    while (_now() - t_start) < timeout:
        f0 = _now()
        img, oleft, otop, used_form, _n = _capture(a)
        if img is None:
            img, oleft, otop = _grab(region=region, monitor=a.get("monitor"))
        hit, pt = _eval_condition(cond, img, oleft, otop)
        frames += 1
        last_form = used_form
        if used_form == "wgc" and hwnd:
            seq = _wgc_frames(int(hwnd))
            if seq != _last_seq:
                frames_new += 1
            _last_seq = seq
        else:
            frames_new += 1
        if hit:
            hit_ever = True
            last_point = pt
            if not then_ran and then_steps:
                _run(then_steps)
                then_ran = True
            if every_steps:
                _run(every_steps)
            if stop_on_found:
                break
        else:
            if else_steps:
                _run(else_steps)
        dt = _now() - f0
        if dt < interval:
            time.sleep(interval - dt)
    el = _now() - t_start
    # 首帧即命中（stop_on_found）时 frames=1，用它算出来的"fps"是纯噪声，不如不报
    _rate = (lambda x: round(x / max(el, 0.001), 1)) if frames >= 3 else (lambda x: None)
    return {"ok": True, "hit": hit_ever, "frames": frames, "frames_new": frames_new,
            "form": last_form,
            "elapsed_s": round(el, 2), "fps_real": _rate(frames),
            "fps_fresh": _rate(frames_new),
            "last_point": last_point, "steps_ran": steps_ran}


HANDLERS = {
    "ping": op_ping, "shot": op_shot, "shot-pw": op_shot_pw, "shot-wgc": op_shot_wgc,
    "front-policy": op_front_policy,
    "ocr": op_ocr, "locate-text": op_locate_text, "click-text": op_click_text,
    "find": op_find, "wait-text": op_wait_text, "wait-text-gone": op_wait_text_gone,
    "wait-stable": op_wait_stable,
    "move": op_move, "click": op_click, "drag": op_drag, "scroll": op_scroll,
    "type": op_type, "key": op_key, "hotkey": op_hotkey, "pos": op_pos,
    "color": op_color,
    "win-list": op_win_list, "win-activate": op_win_activate, "win-hide": op_win_hide,
    "win-show": op_win_show, "win-move": op_win_move,
    "win-stash": op_win_stash, "win-restore": op_win_restore,
    "win-restore-all": op_win_restore_all, "win-stash-list": op_win_stash_list,
    "wake": op_wake, "batch": op_batch, "monitors": op_monitors,
    # v1.3.0 新增
    "key-down": op_key_down, "key-up": op_key_up,
    "mouse-down": op_mouse_down, "mouse-up": op_mouse_up,
    "key-hold": op_key_hold, "sleep": op_sleep,
    # v1.5.0 新增（游戏操控）
    "move-rel": op_move_rel, "mouse-hold": op_mouse_hold,
    "win-front": op_win_front, "find-color": op_find_color, "guard": op_guard,
    "probe": op_probe,
}

_BOOT_TS = _now()


# ---------------------------------------------------------------------------
# v1.3.6：失败时把「该读什么」直接递出去
# 为什么要这一层：SKILL.md 里写「遇到 X 情况去读 Y」是靠模型自觉触发的，而模型不知道
# 自己缺哪条知识，规划阶段常直接跳过。下面这些「情况」服务进程本来就能判断
# （遮挡 / 提权 / 半分辨率 / 黑帧 / Electron 吞合成消息 / 推送式新帧率…），
# 所以把触发权从模型手里拿走：出问题时在返回 JSON 里附一行可照做的 hint + 该读的文件路径。
# 只在真出问题时加字段，正常结果零污染。
# ---------------------------------------------------------------------------
APX = "E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录"
SCENE = {
    "installer": APX + "/场景-安装器与提权窗口/安装器与UIPI边界.md",
    "webform": APX + "/场景-网页表单与输入法/网页表单与输入法干扰.md",
    "miniprogram": APX + "/场景-微信开发者工具与小程序/模拟器点击与原生picker.md",
    "cloud": APX + "/场景-微信开发者工具与小程序/云开发控制台实操.md",
    "gui_launch": APX + "/场景-沙箱里启动GUI/沙箱启动GUI.md",
    "marathon": APX + "/场景-长跑闭环与OCR校验/马拉松长跑闭环.md",
    "banner": APX + "/场景-banner操作横幅/banner历史bug复盘.md",
    "color": APX + "/场景-色彩取值与通道顺序/RB通道互换复盘.md",
    "dialog": APX + "/场景-桌面客户端与文件对话框/文件对话框与splash卡死.md",
}
_LAST_CAP = {}          # 上一次 _capture 用了哪个形态、是否带 region/hwnd
_REQ_SEQ = [0]          # 请求序号：_LAST_CAP 只对本请求有效，防止跨请求残留误导提示
_ELEV_CACHE = {}
# 只有"需要抓图才能回答"的 op，count==0 才值得给排障提示；
# win-list/monitors/color 这类返回 0 条是正常结果，别硬塞 hint
CAPTURE_OPS = {"ocr", "locate-text", "click-text", "wait-text", "wait-text-gone",
               "find", "find-color", "guard", "shot", "shot-pw", "shot-wgc"}


def _hwnd_pid(hwnd):
    import ctypes
    pid = ctypes.wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _is_elevated(hwnd):
    """目标进程是否以管理员权限运行（UIPI 边界：提权窗口我们点不动，也 SendInput 不进去）。
    查不到就返回 False（不臆断），只有证明提权才据此给提示。"""
    import ctypes
    import ctypes.wintypes as wt
    pid = _hwnd_pid(hwnd)
    if not pid or pid in _ELEV_CACHE:
        return _ELEV_CACHE.get(pid, False)
    k = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32
    res = False
    h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        tk = wt.HANDLE()
        if adv.OpenProcessToken(h, 0x0008, ctypes.byref(tk)):   # TOKEN_QUERY
            class _TE(ctypes.Structure):
                _fields_ = [("TokenIsElevated", ctypes.c_uint32)]
            te = _TE()
            sz = ctypes.c_uint32(0)
            if adv.GetTokenInformation(tk, 20, ctypes.byref(te), ctypes.sizeof(te),
                                       ctypes.byref(sz)):
                res = bool(te.TokenIsElevated)
            adv.CloseHandle(tk)
        k.CloseHandle(h)
    _ELEV_CACHE[pid] = res
    return res


def _win_family(hwnd):
    """按窗口类名认门：chromium(Electron/CEF/浏览器) / dialog(系统对话框) / game(GL/SDL/DirectX)"""
    import ctypes
    if not hwnd:
        return ""
    b = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(int(hwnd), b, 256)
    c = b.value or ""
    if "Chrome_WidgetWin" in c:
        return "chromium"
    if c == "#32770":
        return "dialog"
    if any(x in c for x in ("SDL", "GLFW", "Direct3D", "Unity", "Unreal")):
        return "game"
    return ""


def _pw_is_halfres(hwnd):
    """PrintWindow 在 DPI 无关窗口上只印出左上角（内容/缓冲区宽 <0.75）。
    只在失败路径调用——它要多抓一次图，热路径不付这个钱。"""
    res = _printwindow(int(hwnd))
    if not res:
        return False
    pa = np.asarray(res[0])[..., :3]
    ys, xs = np.nonzero(pa.sum(axis=2) > 6)
    if not len(xs):
        return False
    return (int(xs.max() + 1) / max(int(pa.shape[1]), 1)) < 0.75


def _advise(args, res):
    if not isinstance(res, dict):
        return res
    import ctypes
    op = args.get("op")
    blob = str(res.get("error", "")) + str(res.get("detail", ""))
    hint = read = None
    hwnd = args.get("hwnd") or _LAST_CAP.get("hwnd")
    # 只信"这次请求自己抓的图"：_LAST_CAP 是跨请求的，残留的 hwnd 会把上一次的目标当成这次的情况
    if _LAST_CAP.get("req") != _REQ_SEQ:
        hwnd = args.get("hwnd")
        if hwnd and not ctypes.windll.user32.IsWindow(int(hwnd)):
            hwnd = None          # 无效句柄不等于"被遮挡"，别拿它推导出错提示

    if "target_minimized" in blob:
        hint = ("窗口最小化时 screen/pw/wgc 都拿不到有效画面。先 mc.py win-front --hwnd %s 恢复；"
                "注意 win-front 会抢前台，用户没允许抢前台时请先问用户" % (args.get("hwnd") or _LAST_CAP.get("hwnd")))
        read = "SKILL.md §11.3"
    elif "wgc_failed" in blob or "wgc_slot_busy" in blob:
        hint = ("wgc 这次用不上。auto 本来就不走 wgc，改 --form pw 继续；"
                "换窗口直接给新的 --hwnd（worker 会自己切会话）")
        read = "SKILL.md §12.2"
    elif res.get("error") == "screenshot_is_black" or res.get("black") is True:
        hint = "画面全黑：先 mc.py wake 唤醒显示器（DPMS 睡眠下只有 SendInput 有效），仍黑改 shot-pw --hwnd 或 --form wgc"
        read = SCENE["dialog"]
    elif res.get("ok") and res.get("count") == 0 and op in CAPTURE_OPS:
        if hwnd and _occluded(int(hwnd)):
            hint = ("目标正被别的窗口压住，抓屏看到的是遮挡物的像素（不是目标没显示）。"
                    "改 --form pw / --form wgc，或 mc.py win-front --hwnd %s（会抢前台，未获允许前先问用户）" % int(hwnd))
            read = "SKILL.md §12.4"
        elif hwnd and _is_elevated(int(hwnd)):
            hint = ("目标窗口是管理员权限(UIPI)：非提权进程对它既读不到也点不动。"
                    "请用管理员终端重启 svc，或改走它自己的 CLI/接口")
            read = SCENE["installer"]
        elif _LAST_CAP.get("form") == "pw" and hwnd and _pw_is_halfres(int(hwnd)):
            hint = ("pw 在这类 DPI 无关窗口上是半分辨率（内容只占缓冲区左上角）："
                    "min_area 要按 (1/scale)^2 缩小，图上坐标要乘系数才能落到屏幕上。免换算请用 --form wgc")
            read = "SKILL.md §12.3"
        elif op in ("ocr", "locate-text", "click-text", "wait-text", "wait-text-gone"):
            hint = ("OCR 没读到字：region 高度别小于 200px（太扁整行字在降采样里丢）；"
                    "网页先 ctrl+- 缩到最小再截；关键长 ID/编号禁止走 OCR")
            read = SCENE["webform"] if _win_family(hwnd) == "chromium" else SCENE["marathon"]
        elif op == "find-color":
            hint = "颜色没命中：先确认 rgb 是从同一张图取的、放宽 --tolerance、必要时缩小 --region 只框住画布"
            read = SCENE["color"]
        else:
            hint = "没找到目标：条件与画面要取自同一通路同一帧，别一半用屏幕一半用窗口"
    elif args.get("via") == "post" and op in ("key", "click", "type", "hotkey") \
            and _win_family(hwnd) == "chromium":
        hint = ("Chromium/Electron 系应用常忽略 PostMessage 合成输入：截屏核对若没变化，"
                "改用 --via send（需要前台，未获允许前先问用户能不能抢前台）")
        read = "SKILL.md §11.5"
    elif op == "banner" and res.get("visible") is False:
        hint = "横幅没显示：见 banner 历史 bug 复盘；改过 banner.py 必须先 banner.py stop 再调用才生效"
        read = SCENE["banner"]
    if op == "guard" and res.get("fps_fresh") is not None and res.get("fps_real"):
        if res["fps_fresh"] < res["fps_real"] * 0.7:
            hint = ("新帧 %sfps 明显低于轮询 %sfps：推送式通路的正常上限。判定节奏请按 fps_fresh 设计，"
                    "别把轮询次数当感知次数" % (res["fps_fresh"], res["fps_real"]))
            read = "SKILL.md §12.1"
    if hint:
        res["hint"] = hint
    if read:
        res["must_read"] = read
    return res


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(op_ping({}))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._send({"ok": False, "error": "bad_request: %s" % e}, 400)
        op = payload.get("op")
        fn = HANDLERS.get(op)
        if not fn:
            return self._send({"ok": False, "error": "unknown op: %s" % op,
                               "available": sorted(HANDLERS)}, 400)
        t0 = _now()
        _REQ_SEQ[0] += 1
        _LAST_CAP.clear()          # 本请求的捕获状态从干净开始，不吃上一条命令的残留
        try:
            global _OP_INFLIGHT, _LAST_OP_TS
            _OP_INFLIGHT = True
            _LAST_OP_TS = _now()  # 开始即打点：wait-text 等长 op 不误报假死
            try:
                r = fn(payload)
            except Exception as e:
                # 以前 op 内抛异常 = HTTP 500 + 裸 traceback，客户端只能看到 http_500 什么都不知道
                r = {"ok": False,
                     "error": "op_crashed: %s: %s" % (type(e).__name__, str(e)[:160]),
                     "hint": "op 内部异常，多半是必填参数缺失/格式不对。对照 SKILL.md 参数表检查；"
                             "反复复现请把这条命令原样贴出来"}
            finally:
                _OP_INFLIGHT = False
                _LAST_OP_TS = _now()
            r = _advise(payload, r)     # 出问题时把「下一步怎么做 + 该读哪个文件」递出去
            # ping 在 op 执行期间拍摄的 inflight 恒为 True（自指）→ 覆写为收尾后的真实值
            if r.get("pong"):
                r["inflight"] = _OP_INFLIGHT
                r["last_op_age_s"] = round(_now() - _LAST_OP_TS, 1)
            r["_ms"] = round((_now() - t0) * 1000, 1)
            self._send(r)
        except SystemExit:
            self._send({"ok": False, "error": "exit"}, 500)
        except Exception as e:
            import traceback
            self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                        "trace": traceback.format_exc()[-800:]}, 500)


# 用 ThreadingMixIn 但加 _OP_LOCK 在 do_POST 外面做请求级串行
# —— 这样多个请求可以并发进入服务端，但 op 执行是串行的
# 避免锁嵌套 / 锁顺序问题导致的死锁
class _SerializingMixIn:
    def process_request_thread(self, request, client_address):
        # ⚠️ 血泪：这里只调 finish_request 就够了（它在内部 new 出
        # RequestHandlerClass 并跑 setup/handle/finish）。
        # 曾误写成 self.setup_request(request)（不存在的方法），
        # 导致每个请求都抛 AttributeError —— 服务"接而不答"，
        # 客户端全部 90s 超时。已修。
        with _OP_LOCK:                         # 请求级串行
            try:
                self.finish_request(request, client_address)
            finally:
                self.shutdown_request(request)


class ThreadingHTTPServer2(_SerializingMixIn, ThreadingHTTPServer):
    daemon_threads = True
    # 同时只处理 1 个请求（排队）。mss/rapidocr 不是线程安全的，
    # 真要并发必须每个 op 内自己细粒度加锁——不值得。
    # 用单线程串行更稳，也避免了全屏 OCR 等长 op 卡死整个服务。


def _watchdog_loop(port):
    """看门狗：单 op 超过 _MAX_OP_SECONDS 仍不结束 → 判定假死，自杀退出。
    客户端（mc.py ensure_svc）下次调用发现连不上会自动重启新 svc（自愈）。"""
    while True:
        time.sleep(_WATCHDOG_TICK)
        if _OP_INFLIGHT and (_now() - _LAST_OP_TS) > _MAX_OP_SECONDS:
            try:
                print("[watchdog] op stuck >%ss, suicide for auto-restart"
                      % _MAX_OP_SECONDS, file=sys.stderr, flush=True)
            except Exception:
                pass
            _rm_pid(port)
            os._exit(1)


def cmd_serve(a):
    srv = ThreadingHTTPServer2(("127.0.0.1", a.port), Handler)
    _write_pid(a.port)
    threading.Thread(target=_watchdog_loop, args=(a.port,), daemon=True).start()
    common.emit({"ok": True, "serving": True, "port": a.port, "pid": os.getpid()})
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _daemon_kill()
        _rm_pid(a.port)


def cmd_start(a):
    py = sys.executable
    f = os.path.abspath(__file__)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    # 优先 pythonw.exe 避免弹出控制台窗口
    cand = py
    if py.lower().endswith("python.exe"):
        pw = py[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(pw):
            cand = pw
    try:
        subprocess.Popen([cand, f, "serve", "--port", str(a.port)],
                         creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except Exception:
        subprocess.Popen([py, f, "serve", "--port", str(a.port)],
                         creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    # 等就绪
    import urllib.request
    for _ in range(120):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/ping" % a.port, timeout=1) as r:
                d = json.loads(r.read().decode("utf-8"))
                common.emit({"ok": True, "started": True, "port": a.port, "pid": d.get("pid")})
                return
        except Exception:
            time.sleep(0.25)
    common.fail("svc 启动超时")


def cmd_stop(a):
    import urllib.request
    pid = None
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/ping" % a.port, timeout=1) as r:
            pid = json.loads(r.read().decode("utf-8")).get("pid")
    except Exception:
        pass
    if not pid:
        # ping 不通（svc 可能假死）→ 用 pid 文件兜底
        try:
            with open(_pid_file(a.port)) as f:
                pid = int(f.read().strip() or 0)
        except Exception:
            pid = None
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        _rm_pid(a.port)
        common.emit({"ok": True, "stopped": True, "pid": pid})
    else:
        common.emit({"ok": True, "stopped": False, "note": "service not running"})


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "start", "stop", "ping"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, default=_DEFAULT_PORT)
    a = p.parse_args()
    if a.cmd == "serve":
        cmd_serve(a)
    elif a.cmd == "start":
        cmd_start(a)
    elif a.cmd == "stop":
        cmd_stop(a)
    else:
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/ping" % a.port, timeout=1) as r:
                common.emit(json.loads(r.read().decode("utf-8")))
        except Exception:
            common.fail("svc not running")


if __name__ == "__main__":
    main()
