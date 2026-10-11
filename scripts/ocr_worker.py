# -*- coding: utf-8 -*-
"""ocr_worker.py —— 独立 OCR 进程（解决 rapidocr 在 svc HTTP 请求线程内推理卡死的问题）。

第 4 轮改造：支持【常驻 daemon 模式】。
  旧版（第 3 轮）是"每次 OCR 临时 spawn 一个 python 子进程跑单次命令"，
  模型每次都要重新加载（python 冷启 + ONNX 模型初始化 ≈ 1~2.5s/次），
  导致全屏 OCR 虽不再卡死但仍有 ~3s 的固定开销。
  新版由 svc 启动时拉起一个常驻 daemon，RapidOCR 引擎只初始化一次，
  之后每个 OCR 请求走 stdin/stdout 行协议，模型常驻内存（推理本身仅 ~0.3s）。

两种模式：
  1) 单次模式（兼容旧调用）: python ocr_worker.py <图片路径> [max_side]
     跑完即退出，向 stdout 输出一行 JSON 结果。
  2) 常驻 daemon 模式:       python ocr_worker.py --daemon [--max-side N]
     从 stdin 逐行读 JSON 指令，处理一行回一行结果，循环不退出。
     指令格式: {"img": "<png路径>", "max_side": N}  或  {"op": "ping"}
     结果格式: {"ok":true,"count":N,"items":[...]} / {"ok":false,"error":"..."}
     stdin 读到 EOF（svc 进程死亡/被 kill，管道写端关闭）→ 自动退出，不留孤儿进程。

  返回的 items 坐标为【图片原尺寸】坐标系（已按缩放还原，不含屏幕偏移）。

⚠️ 引擎调用必须在"普通主线程"里做：
  rapidocr(onnxruntime) 推理实测在 socketserver 请求线程内 100% 卡死；
  在普通进程主线程（含本 daemon 的 stdin 读取主循环）内正常。
  svc 负责：拉起本进程 / 喂指令 / 超时 kill 并重建，绝不在 svc 请求线程里推理。
"""
import sys
import json
import io
import contextlib

# 强制 stdout/stderr 用 UTF-8 输出（避免 Windows GBK 控制台把中文结果写坏）
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_ENGINE = None


def _engine():
    """RapidOCR 引擎单例（daemon 常驻模式下只初始化一次）。

    ⚠️ 初始化/推理时的任何第三方 print/logging 输出都必须被吞掉：
    daemon 模式下 stdout 是行协议通道（svc 逐行读 JSON），混入杂音会破坏协议。
    onnxruntime/RapidOCR 的启动日志用 redirect_stdout 兜底；stderr 由 svc 接 DEVNULL。
    """
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        with contextlib.redirect_stdout(io.StringIO()):
            _ENGINE = RapidOCR()
    return _ENGINE


def ocr_file(png, max_side=1280):
    """读图 → 降采样（长边压到 max_side）→ OCR → 坐标还原到原图。
    返回 dict（含 ok 字段）。"""
    import cv2
    img = cv2.imread(png)
    if img is None:
        return {"ok": False, "error": "img_read_fail: " + png}
    h, w = img.shape[:2]
    scale = 1.0
    small = img
    if max(h, w) > max_side:
        scale = max_side / float(max(h, w))
        small = cv2.resize(img, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                           interpolation=cv2.INTER_AREA)
    try:
        res, _ = _engine()(small)
    except Exception as e:
        return {"ok": False, "error": "ocr_infer_fail: %s" % e}
    items = []
    if res:
        for box, text, score in res:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            items.append({
                "text": text,
                "confidence": round(float(score), 4),
                "x": int(((x1 + x2) / 2) / scale),
                "y": int(((y1 + y2) / 2) / scale),
                "left": int(x1 / scale), "top": int(y1 / scale),
                "width": int((x2 - x1) / scale), "height": int((y2 - y1) / scale),
            })
    return {"ok": True, "count": len(items), "items": items}


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--daemon":
        max_side = 1280
        if "--max-side" in args:
            i = args.index("--max-side")
            if i + 1 < len(args):
                max_side = int(args[i + 1])
        # 预热引擎：失败则打一行错误直接退出（svc 首次读 stdout 会拿到这行）
        try:
            _engine()
        except Exception as e:
            _emit({"ok": False, "error": "ocr_init_fail: %s" % e})
            sys.exit(1)
        # 常驻主循环：阻塞读 stdin；svc 死亡 → EOF → 退出
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception:
                _emit({"ok": False, "error": "bad_cmd_json"})
                continue
            if cmd.get("op") == "ping":
                _emit({"ok": True, "pong": True})
            else:
                png = cmd.get("img")
                if not png:
                    _emit({"ok": False, "error": "missing img"})
                    continue
                ms = int(cmd.get("max_side") or max_side)
                _emit(ocr_file(png, ms))
        return  # EOF：svc 已退出，本进程自然结束
    # ---- 单次模式（兼容第 3 轮直接调用）----
    png = args[0] if args else ""
    max_side = int(args[1]) if len(args) > 1 else 1280
    if not png:
        _emit({"ok": False, "error": "missing png arg"})
        sys.exit(1)
    try:
        _emit(ocr_file(png, max_side))
    except Exception as e:
        _emit({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
