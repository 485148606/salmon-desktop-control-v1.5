# -*- coding: utf-8 -*-
"""wgc_worker.py —— WGC 抓帧独立子进程（v1.3.6）

为什么单独开进程：windows-capture 2.0.1 在 start_free_threaded() 里会 access violation
把宿主进程整个打死（实测无 Python 堆栈，只有 PYTHONFAULTHANDLER 看得见）。放进子进程后
崩的只是 worker，svc 发现进程没了就重建，任务不断。这和 ocr_worker.py 当年为
onnxruntime 做的隔离是同一招。

协议（stdin 一行一条 JSON 命令，stdout 一行一条 JSON 回复）：
    {"cmd":"start","hwnd":123}   -> {"ok":true,"name":"shm名","w":..,"h":..}
    {"cmd":"stop"}               -> {"ok":true}
    {"cmd":"exit"}               -> 退出
帧走共享内存（seqlock 双缓冲）而不是管道：svc 每帧只付一次 memcpy（实测 1044x864 约 0.4ms）。

帧头 <I magic, Q seq, I w, I h, Q t_ns, Q frames>；seq 奇数=正在写，偶数=可读。
"""
import json
import struct
import sys

try:                                 # 与 svc 同口径：Per-Monitor DPI 感知
    import ctypes.windll.shcore as _sh
    _sh.SetProcessDpiAwareness(2)
except Exception:
    pass
import threading
import time
from multiprocessing import shared_memory

HDR = struct.Struct("<IQIIQQ")
MAGIC = 0x53574743                      # 'CGWS'
DATA_OFF = 64
MAX_BYTES = 64 + 2 * 4096 * 2160 * 4    # 双缓冲上限 ~140MB

st = {"shm": None, "size": 0, "frame_cap": 0, "ctl": None, "cap": None, "hwnd": None,
      "w": 0, "h": 0, "seq": 0, "frames": 0, "got": None, "err": None, "pending": None,
      "overflow": False, "closed": False, "lock": threading.Lock()}


def dbg(msg):
    """诊断走 stderr（svc 把它重定向到 TEMP/salmon_wgc_worker.log）。"""
    try:
        sys.stderr.write("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.write(chr(10))
        sys.stderr.flush()
    except Exception:
        pass


def reply(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ensure_shm(w, h):
    need = DATA_OFF + 2 * w * h * 4
    if need > MAX_BYTES:
        raise ValueError("frame too large: %dx%d" % (w, h))
    if st["shm"] is not None and st["size"] >= need:
        return st["shm"].name
    drop_shm()
    shm = shared_memory.SharedMemory(create=True, size=need)
    st["shm"], st["size"] = shm, need
    st["frame_cap"] = (need - DATA_OFF) // 2
    return shm.name


def prealloc(hwnd):
    """先按窗口矩形把共享内存开好，再开始捕获。
    否则会出现「首帧到达时还没有共享内存 → 窗口随后不再重绘 → 永远等不到第二帧」
    ——实测把目标窗口压到后台就会这样，不是偶发。"""
    import ctypes
    import ctypes.wintypes
    r = ctypes.wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(r)):
        return 1280, 720
    w, h = r.right - r.left, r.bottom - r.top
    return max(int(w * 1.2), 320), max(int(h * 1.2), 240)


def flush_pending():
    """把共享内存就绪前暂存的那一帧回放一次（否则窗口不再重绘时就永远等不到帧）。"""
    p = st.get("pending")
    if not p:
        return
    fb, size = p                      # p 是 (ndarray, (w,h))，别把整个 tuple 当成帧
    st["pending"] = None
    write_frame(fb, size[0], size[1])


def drop_shm():
    if st["shm"] is not None:
        try:
            st["shm"].close()
            st["shm"].unlink()
        except Exception:
            pass
    st["shm"] = None
    st["size"] = 0
    st["frame_cap"] = 0


def write_frame(fb, w, h):
    """在捕获线程里调用。shm 没就绪 / 帧比缓冲大时先暂存一帧，等主线程开好再回放一次。"""
    st["got"] = (w, h)
    st["calls"] = st.get("calls", 0) + 1
    s = w * h * 4
    try:
        # frame_buffer 可能带行距填充（row_pitch > w*4）→ 非 C 连续，memoryview.cast("B") 会失败。
        # 统一走一次 contiguous 化（实测 ~0.4ms / 1044x864），比在写分支里抛异常好。
        if getattr(fb, "flags", None) is not None and not fb.flags["C_CONTIGUOUS"]:
            fb = fb.copy()
    except Exception as e:
        st["err"] = "contiguous: %s" % str(e)[:120]
    if st["shm"] is None or s > st.get("frame_cap", 0):
        if not st.get("stashed_once"):      # 每轮只记一次，别把日志刷满
            dbg("stash frame %dx%d need=%d cap=%d shm=%s" % (w, h, s, st.get("frame_cap", 0), bool(st["shm"])))
            st["stashed_once"] = True
        st["pending"] = (fb.copy(), (w, h))
        if st["shm"] is not None:
            st["overflow"] = True            # 窗口被放大过：让 svc 触发一次重建
        return
    try:
        with st["lock"]:
            fnew = st["frames"] + 1
            slot = ((fnew - 1) % 2) * s        # 双缓冲槽号：由帧号决定，读侧同一公式
            buf = memoryview(st["shm"].buf)
            # 先发布奇数 seq（=正在写），再写数据，最后发布偶数 seq（=定稿）。
            # 之前把奇数当定稿值写进头部，读侧永远判定"还在写"，一帧都取不到。
            buf[:HDR.size] = HDR.pack(MAGIC, 2 * fnew - 1, st["w"], st["h"],
                                      st.get("t_ns", 0), st["frames"])
            src = memoryview(fb).cast("B")
            buf[DATA_OFF + slot: DATA_OFF + slot + s] = src[:s]
            now = time.monotonic_ns()
            buf[:HDR.size] = HDR.pack(MAGIC, 2 * fnew, w, h, now, fnew)
            st["seq"] = 2 * fnew
            st["frames"] = fnew
            st["t_ns"] = now
            st["w"], st["h"] = w, h
    except Exception as e:
        st["err"] = "write: %s: %s" % (type(e).__name__, str(e)[:140])
        dbg("write failed: %s" % st["err"])


def stop():
    if st["ctl"] is not None:
        try:
            st["ctl"].stop()
        except Exception:
            pass
    st["ctl"] = st["cap"] = st["hwnd"] = None
    st["frames"] = 0
    st["got"] = None
    st["seq"] = 0
    st["err"] = None
    st["pending"] = None
    st["overflow"] = False
    st["stashed_once"] = False


def start(hwnd):
    from windows_capture import WindowsCapture

    def on_frame_arrived(frame, control):
        fb = frame.frame_buffer                 # (h,w,4) BGRA 连续
        try:
            write_frame(fb, fb.shape[1], fb.shape[0])
        except Exception as e:
            st["err"] = "write_frame: %s" % str(e)[:120]

    def on_closed():
        st["closed"] = True

    cap = WindowsCapture(window_hwnd=int(hwnd), cursor_capture=False, draw_border=False)
    cap.event(on_frame_arrived)
    cap.event(on_closed)
    ctl = cap.start_free_threaded()
    st["ctl"], st["cap"], st["hwnd"] = ctl, cap, int(hwnd)
    t0 = time.time()
    while st["got"] is None and time.time() - t0 < 4.0:
        time.sleep(0.02)
    if st["got"] is None:
        stop()
        raise RuntimeError("no_first_frame%s" % ((" err=" + st["err"]) if st["err"] else ""))
    return st["got"]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except Exception as e:
            reply({"ok": False, "error": "bad_cmd: %s" % e})
            continue
        c = cmd.get("cmd")
        try:
            if c == "exit":
                stop()
                drop_shm()
                reply({"ok": True, "bye": True})
                return
            if c == "stop":
                stop()
                reply({"ok": True})
                continue
            if c == "start":
                stop()
                pre_w, pre_h = prealloc(cmd["hwnd"])
                name = ensure_shm(pre_w, pre_h)      # 先开好共享内存，再开始捕获
                dbg("prealloc %dx%d shm_name=%s size=%d cap=%d" % (pre_w, pre_h, name, st["size"], st["frame_cap"]))
                w, h = start(cmd["hwnd"])
                # 预猜尺寸可能偏小（窗口矩形 ≠ 帧尺寸：DPI 虚拟化、隐形边框都会差），
                # 拿到真实帧尺寸后兜底扩容；已够大时 ensure_shm 直接复用，不会重新分配。
                name = ensure_shm(max(pre_w, w), max(pre_h, h))
                dbg("got %dx%d shm=%s cap=%d need=%d frames=%d pending=%s calls=%s err=%s" % (
                    w, h, bool(st["shm"]), st.get("frame_cap", 0), w * h * 4,
                    st["frames"], bool(st.get("pending")), st.get("calls", 0), st.get("err")))
                flush_pending()
                dbg("after flush frames=%d" % st["frames"])                      # 回放启动瞬间暂存的那一帧
                if st["frames"] == 0:
                    t0 = time.time()
                    while st["frames"] == 0 and time.time() - t0 < 2.0:
                        time.sleep(0.02)
                if st["frames"] == 0:
                    stop()
                    raise RuntimeError("no_frame_in_shm%s calls=%s" % (((" err=" + st["err"]) if st["err"] else ""), st.get("calls", 0)))
                reply({"ok": True, "name": name, "w": w, "h": h, "hwnd": int(cmd["hwnd"])})
                continue
            reply({"ok": False, "error": "unknown cmd %s" % c})
        except Exception as e:
            reply({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:180])})


if __name__ == "__main__":
    main()
