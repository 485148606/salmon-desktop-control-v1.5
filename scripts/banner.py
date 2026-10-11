# -*- coding: utf-8 -*-
"""banner.py —— 三文鱼大模型「屏幕顶部操作横幅」（第 5 轮新增，v1.2.0）

为什么需要它（本轮血泪）：
  第 1~4 轮做 GUI 自动化时，为了让目标窗口露出来，主代理会把 WorkBuddy
  整个 SW_HIDE 藏掉（win-hide）。后果：
    1. 用户眼前"一片黑/只剩被控软件"，不知道 AI 在干嘛，不敢碰键盘鼠标；
    2. 环节进展只能靠 WorkBuddy 会话文字，但 WorkBuddy 被藏了，用户看不到进度；
    3. 人机协作断裂：该用户接管时没人接管。
  第 5 轮改为「提示条协作」：不藏 WorkBuddy（改为最小化/露出），改在屏幕顶部
  挂一条半透明、点击穿透的横幅——
    第一行：三文鱼大模型正在操控屏幕，请勿触碰（警示）
    第二行：当前正在进行的环节（与 WorkBuddy 会话文字实时同步）
  用户看着横幅就知道 AI 进行到哪一步、该不该接管，彻底告别"盲操作"。

设计（纯 Win32，零第三方 GUI 依赖）：
  - 常驻独立进程（自带 HTTP 127.0.0.1:8767，mc.py 直连，不依赖 svc 8765）
  - 窗口：WS_POPUP + WS_EX_TOPMOST|WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE
    → 置顶、半透明、鼠标点击全部穿透到下方窗口（不挡操作）
  - WM_PAINT 自绘两行文本（微软雅黑），HTTP /set 更新后 InvalidateRect 重绘
  - 尽量窄（高度 ~ 屏幕 5%），避免遮挡目标应用顶部按钮；OCR/shot 时如遮挡顶部
    可先 banner-hide 再截，或用 shot-pw 抓目标窗口（详见 SKILL.md 第 5 轮说明）

用法（一般经 mc.py，主代理不直接调本文件）：
  python banner.py serve [--port 8767]   # 前台常驻（调试）
  python banner.py start  [--port 8767]  # 后台拉起（mc.py 自动做）
  python banner.py stop   [--port 8767]
  本文件自带 HTTP：
    GET  /ping            -> {ok,title,subtitle,visible}
    POST /set  {title?,subtitle?,visible?}   -> 更新并显示（title 缺省保留旧值）
    POST /hide            -> 隐藏横幅（执行结束收尾必调）
"""
import os
import sys
import json
import time
import ctypes
import ctypes.wintypes as wt
import threading
import subprocess
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PORT = 8767

# ---------------- DPI（必须在任何 GUI 调用前） ----------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------- Win32 常量 ----------------
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCHITTEST = 0x0084
WM_APP = 0x8000
WM_APP_UPDATE = WM_APP + 1
COLOR_WINDOW = 5
CW_USEDEFAULT = 0x80000000
SW_HIDE = 0
SW_SHOWNA = 8
LWA_ALPHA = 0x00000002
HTTRANSPARENT = -1

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

# wintypes 缺失的类型补丁（64-bit Windows）
COLORREF = ctypes.c_uint32
WPARAM = ctypes.c_size_t        # UINT_PTR
LPARAM = ctypes.c_ssize_t       # LONG_PTR
ATOM = ctypes.c_ushort
HMENU = ctypes.c_void_p


class WNDCLASSW(ctypes.Structure):
    """ctypes.wintypes 未提供 WNDCLASSW，按文档手动定义（64-bit 对齐）。"""
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


# c_void_p 句柄类型会做 int->指针 转换，argtypes 需声明
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                   wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, HMENU,
                                   ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                             ctypes.POINTER(wt.RECT), wt.UINT]
user32.DrawTextW.restype = ctypes.c_int
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = ctypes.c_bool
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_bool
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, COLORREF, ctypes.c_byte,
                                              wt.DWORD]
user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_bool]
user32.InvalidateRect.restype = ctypes.c_bool
user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = ctypes.c_void_p
user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.EndPaint.restype = ctypes.c_bool
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = ctypes.c_bool
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                              wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_wchar_p]
gdi32.CreateFontW.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteObject.restype = ctypes.c_bool
gdi32.CreateSolidBrush.argtypes = [COLORREF]
gdi32.CreateSolidBrush.restype = ctypes.c_void_p
gdi32.SetTextColor.argtypes = [ctypes.c_void_p, COLORREF]
gdi32.SetTextColor.restype = COLORREF
gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT), ctypes.c_void_p]
user32.FillRect.restype = ctypes.c_int

# ---------------- 全局状态 ----------------
_STATE = {
    "title": "三文鱼大模型正在操控屏幕，请勿触碰",
    "subtitle": "准备中…",
    "visible": True,
    "hwnd": None,
    "font_title": None,
    "font_sub": None,
}
_LOCK = threading.Lock()

# 中文字体
FACE = "Microsoft YaHei UI"
# 颜色（BGR 顺序，GDI 用）
BG_COLOR = 0x001A1A2E          # 深蓝黑 (46,26,26 -> BGR)
TITLE_COLOR = 0x0044D7FF       # 亮橙黄 (FF,D7,44) —— 警示醒目
SUB_COLOR = 0x00F0F0F0         # 近白


# ---------------- 进程管理（pid 文件 + 自愈，风格同 svc.py） ----------------
def _pid_file(port):
    return os.path.join(HERE, "banner.%d.pid" % port)


def _read_pid(port):
    try:
        with open(_pid_file(port)) as f:
            return int(f.read().strip() or 0) or None
    except Exception:
        return None


def _pid_alive(pid):
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                           capture_output=True, timeout=5)
        out = r.stdout.decode("utf-8", "ignore")
        return str(pid) in out and "No tasks" not in out
    except Exception:
        return False


def _spawn_detached(py, args):
    """完全脱离父进程组启动（cmd /c start /B —— 沙箱回收不到）。"""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    cand = py
    if py.lower().endswith("python.exe"):
        pw = py[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(pw):
            cand = pw
    cmdline = 'start /B "" ' + subprocess.list2cmdline([cand] + args)
    try:
        return subprocess.Popen(cmdline, shell=True,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True,
                                creationflags=0x08000000)
    except Exception:
        return subprocess.Popen([cand] + args, creationflags=DETACHED_PROCESS,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True)


def _stop_banner(port):
    pid = _read_pid(port)
    if pid and _pid_alive(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=10)
        time.sleep(0.3)
    # 兜底：按端口查
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10)
        out = r.stdout.decode("utf-8", "ignore")
        for line in out.splitlines():
            if ":%d" % port in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        subprocess.run(["taskkill", "/PID", parts[-1], "/F"],
                                       capture_output=True, timeout=10)
                    except Exception:
                        pass
    except Exception:
        pass
    for suffix in ("", ) if False else ("",):
        try:
            if os.path.exists(_pid_file(port)):
                os.remove(_pid_file(port))
        except Exception:
            pass


def _http_request(port, path, payload=None, timeout=3.0):
    import urllib.request
    url = "http://127.0.0.1:%d%s" % (port, path)
    if payload is None:
        req = urllib.request.Request(url)
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------- Win32 窗口 ----------------
def _create_font(px_height, bold=False):
    """px_height>0 表示字符像素高。返回 HFONT。"""
    weight = 700 if bold else 400
    return gdi32.CreateFontW(-px_height, 0, 0, 0, weight, 0, 0, 0,
                             0x86, 0, 0, 5, 0, FACE)


def _paint(hwnd):
    """WM_PAINT：自绘两行文字。"""
    ps = ctypes.create_string_buffer(128)   # PAINTSTRUCT 占位（BeginPaint 只写不读）
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    try:
        rect = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top

        # 背景
        brush = gdi32.CreateSolidBrush(BG_COLOR)
        try:
            user32.FillRect(hdc, ctypes.byref(rect), brush)
        finally:
            gdi32.DeleteObject(brush)

        with _LOCK:
            title = _STATE["title"]
            subtitle = _STATE["subtitle"]

        # 第一行：警示（大字、居中）
        old_font = gdi32.SelectObject(hdc, _STATE["font_title"])
        old_color = gdi32.SetTextColor(hdc, TITLE_COLOR)
        gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
        tr = wt.RECT(int(w * 0.02), int(h * 0.05),
                     int(w * 0.98), int(h * 0.52))
        DT_CENTER = 0x0001; DT_VCENTER = 0x0004
        DT_SINGLELINE = 0x0020; DT_NOPREFIX = 0x0800
        DT_END_ELLIPSIS = 0x8000
        FLAGS = DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS
        user32.DrawTextW(hdc, title, -1, ctypes.byref(tr), FLAGS)

        # 第二行：环节（居中）
        gdi32.SelectObject(hdc, _STATE["font_sub"])
        gdi32.SetTextColor(hdc, SUB_COLOR)
        sr = wt.RECT(int(w * 0.04), int(h * 0.55),
                     int(w * 0.96), int(h * 0.95))
        user32.DrawTextW(hdc, subtitle, -1, ctypes.byref(sr), FLAGS)

        gdi32.SetTextColor(hdc, old_color)
        gdi32.SelectObject(hdc, old_font)
    finally:
        user32.EndPaint(hwnd, ctypes.byref(ps))


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, WPARAM, LPARAM)   # LRESULT


def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_NCHITTEST:
        # 鼠标点击穿透：让窗口对 hit-test 透明，点击全部落到下方窗口
        return HTTRANSPARENT
    if msg == WM_PAINT:
        if _STATE.get("font_title"):
            _paint(hwnd)
        else:
            ps = ctypes.create_string_buffer(128)
            user32.BeginPaint(hwnd, ctypes.byref(ps))
            user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    if msg == WM_ERASEBKGND:
        return 1  # 背景在 WM_PAINT 画，避免闪
    if msg == WM_APP_UPDATE:
        user32.InvalidateRect(hwnd, None, True)
        return 0
    if msg == 0x0010:  # WM_CLOSE
        user32.DestroyWindow(hwnd)
        return 0
    if msg == 0x0002:  # WM_DESTROY
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# 窗口过程回调：必须在 def _wndproc 之后创建，且全局持有防 GC
_WNDPROC_REF = WNDPROC(_wndproc)


def _screen_rect():
    """返回 (x, y, w, h) 主显示器整屏（物理像素）。"""
    x = user32.GetSystemMetrics(0)   # SM_CXSCREEN
    y = user32.GetSystemMetrics(1)   # SM_CYSCREEN
    return 0, 0, x, y


def _create_window():
    """创建顶部横幅窗口，返回 hwnd。
    v1.2.2 微调：整窗 alpha 降到 ~10%（透明度约 90%），宽度收窄到 55%、高度压到 ~3% 屏高
    —— 让横幅不再盖住/挡住下方网页顶部（mss 截图与 OCR 也能透过它读到底层内容）。
    """
    sx, sy, sw, sh = _screen_rect()
    bw = int(sw * 0.55)
    bh = max(44, min(72, int(sh * 0.030)))
    bx = (sw - bw) // 2
    by = 0

    cls_name = "SalmonBannerWnd"
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = ctypes.cast(_WNDPROC_REF, ctypes.c_void_p)
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = None
    wc.lpszMenuName = None
    wc.lpszClassName = cls_name
    # 注册（类可能已注册，已注册时 RegisterClassW 返回 0 + ERROR_CLASS_ALREADY_EXISTS=1410）
    user32.RegisterClassW(ctypes.byref(wc))

    ex_style = WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    style = WS_POPUP          # 先不 WS_VISIBLE，字体建好再显示（否则首次 WM_PAINT 无字体）
    hwnd = user32.CreateWindowExW(
        ex_style, cls_name, "", style,
        bx, by, bw, bh, 0, 0, wc.hInstance, 0)
    if not hwnd:
        return None

    _STATE["hwnd"] = hwnd
    _STATE["font_title"] = _create_font(max(12, int(bh * 0.42)), bold=True)
    _STATE["font_sub"] = _create_font(max(10, int(bh * 0.28)), bold=False)

    # v1.2.3：alpha 25(≈10%) 淡到肉眼不可见（用户实测"看不到横幅"），提到 200(≈78%)。
    # 横幅仅占顶部 61px，OCR/shot 按约定避开顶部即可；若嫌挡视线可再调低。
    user32.SetLayeredWindowAttributes(hwnd, 0, 200, LWA_ALPHA)
    # 字体就绪后再显示（触发首次自绘）
    user32.ShowWindow(hwnd, SW_SHOWNA)
    user32.InvalidateRect(hwnd, None, True)
    return hwnd


# ---------------- HTTP ----------------
class BannerHTTPHandler(BaseHTTPRequestHandler):
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
        if self.path.startswith("/ping"):
            with _LOCK:
                self._send({"ok": True, "title": _STATE["title"],
                            "subtitle": _STATE["subtitle"],
                            "visible": _STATE["visible"]})
        else:
            self._send({"ok": False, "error": "unknown path"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            p = json.loads(raw or "{}")
        except Exception as e:
            self._send({"ok": False, "error": "bad_json: %s" % e}, 400)
            return

        if self.path.startswith("/set"):
            with _LOCK:
                if p.get("title"):
                    _STATE["title"] = p["title"][:60]
                if p.get("subtitle") is not None:
                    _STATE["subtitle"] = str(p["subtitle"])[:120]
                # v1.2.3 修复：/set 语义应为「更新并显示」——visible 缺省置 True。
                # 旧逻辑保留旧状态，导致 banner-hide 之后所有 banner 调用永远 visible:false。
                vis = p.get("visible", True)
                _STATE["visible"] = bool(vis)
                title = _STATE["title"]
                subtitle = _STATE["subtitle"]
                visible = _STATE["visible"]
                hwnd = _STATE["hwnd"]
            if hwnd:
                user32.ShowWindow(hwnd, SW_SHOWNA if visible else SW_HIDE)
                if visible:
                    user32.PostMessageW(hwnd, WM_APP_UPDATE, 0, 0)
            self._send({"ok": True, "title": title, "subtitle": subtitle,
                        "visible": visible})
        elif self.path.startswith("/hide"):
            with _LOCK:
                _STATE["visible"] = False
                hwnd = _STATE["hwnd"]
            if hwnd:
                user32.ShowWindow(hwnd, SW_HIDE)
            self._send({"ok": True, "visible": False})
        else:
            self._send({"ok": False, "error": "unknown path"}, 404)


def _run_server(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), BannerHTTPHandler)
    srv.serve_forever()


def _run_serve(port):
    """前台运行：创建窗口 + 起 HTTP 线程 + GUI 消息循环。"""
    if _read_pid(port) and _pid_alive(_read_pid(port)):
        print(json.dumps({"ok": False, "error": "banner already running"},
                         ensure_ascii=False))
        return 1
    with open(_pid_file(port), "w") as f:
        f.write(str(os.getpid()))
    hwnd = _create_window()
    if not hwnd:
        print(json.dumps({"ok": False, "error": "create_window_failed"},
                         ensure_ascii=False))
        return 1
    threading.Thread(target=_run_server, args=(port,), daemon=True).start()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="start",
                    choices=["serve", "start", "stop", "ping"])
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT)
    a = ap.parse_args()

    if a.cmd == "serve":
        sys.exit(_run_serve(a.port))

    if a.cmd == "stop":
        _stop_banner(a.port)
        print(json.dumps({"ok": True, "stopped": True}, ensure_ascii=False))
        sys.exit(0)

    if a.cmd == "ping":
        try:
            r = _http_request(a.port, "/ping", timeout=1.5)
            print(json.dumps(r, ensure_ascii=False))
            sys.exit(0 if r.get("ok") else 2)
        except Exception as e:
            print(json.dumps({"ok": False, "error": "no_banner: %s" % e},
                             ensure_ascii=False))
            sys.exit(2)

    if a.cmd == "start":
        # 若已在跑，直接 ping 成功返回
        try:
            r = _http_request(a.port, "/ping", timeout=1.0)
            if r.get("ok"):
                print(json.dumps(r, ensure_ascii=False))
                sys.exit(0)
        except Exception:
            pass
        pid = _read_pid(a.port)
        if pid and _pid_alive(pid):
            _stop_banner(a.port)
        _spawn_detached(sys.executable, [os.path.abspath(__file__), "serve",
                                         "--port", str(a.port)])
        # 等就绪
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = _http_request(a.port, "/ping", timeout=1.0)
                if r.get("ok"):
                    r["_booted"] = True
                    print(json.dumps(r, ensure_ascii=False))
                    sys.exit(0)
            except Exception:
                time.sleep(0.3)
        print(json.dumps({"ok": False, "error": "banner_start_timeout"},
                         ensure_ascii=False))
        sys.exit(2)


if __name__ == "__main__":
    main()
