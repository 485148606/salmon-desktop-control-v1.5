# -*- coding: utf-8 -*-
"""mc.py —— salmon-desktop-control 快速客户端（走常驻 svc，毫秒级响应）。

替代直接用 screen.py / mouse.py：后者每次都要重启 Python + 加载 cv2/rapidocr，
单命令 2-5 秒；走 svc 后依赖只加载一次，单命令通常 < 100ms。

用法（第一个词是 op，后面 --key value 任意组合）：
  python mc.py ping
  python mc.py shot --out D:/x.png [--region 0,0,800,600] [--require_content 1]
  python mc.py shot-pw --hwnd 4915506 --out D:/x.png
  python mc.py ocr --region 0,0,800,600
  python mc.py locate-text --text 使用教程 [--region 0,0,800,600]
  python mc.py click-text --text 使用教程 [--offset_y 30]
  python mc.py wait-text --text 编译完成 --timeout 30 --interval 0.6
  python mc.py wait-stable --region 2280,80,600,1800 --timeout 40
  python mc.py click --x 100 --y 200 [--count 2] [--button right]
  python mc.py move --x 100 --y 200
  python mc.py drag --x1 10 --y1 20 --x2 300 --y2 20
  python mc.py scroll --amount -5 [--x 100 --y 200]
  python mc.py type --text "中文也可以"
  python mc.py key --key enter
  python mc.py hotkey --keys "ctrl,s"
  python mc.py pos
  python mc.py color --x 100 --y 200
  python mc.py win-list [--keyword 微信] [--pid 1234]
  python mc.py win-activate --hwnd 4915506
  python mc.py win-hide --hwnd 25755890
  python mc.py win-stash --hwnd 25755890 --reason "OCR期间藏起WorkBuddy"   # 推荐：藏 + 打快照
  python mc.py win-restore --hwnd 25755890          # 按快照还原单个
  python mc.py win-restore-all                      # 还原全部（收尾必调）
  python mc.py win-stash-list                       # 看还有谁没还原
  python mc.py wake
  python mc.py banner --subtitle "正在打开微信 / 第 1 步"   # 第5轮：屏幕顶部横幅更新（title 缺省保留）
  python mc.py banner --title "三文鱼大模型正在操控屏幕，请勿触碰" --subtitle "..."
  python mc.py banner-hide                                   # 第5轮：自动化结束收尾，关闭横幅
  python mc.py batch --json '[{"op":"shot","out":"D:/a.png"},{"op":"click-text","text":"下一步"}]'

说明：
  - 布尔开关：--exact / --require_content / --stop_on_error 等，出现即为 true
    （想显式给 false 就写 --exact 0）
  - 数字/字符串自动识别：能转 int/float 就转，否则当字符串
  - svc 没起会自动后台拉起（首次拉起 ~3-5s，之后就是快的）
  - --port 可指定端口（默认 8765）
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.join(HERE, "svc.py")
BANNER = os.path.join(HERE, "banner.py")
DEFAULT_PORT = 8765
BANNER_PORT = 8767


# ---- banner op 走独立 HTTP（banner 进程独立于 svc，沙箱回收 svc 时不影响） ----
def ensure_banner(port=BANNER_PORT, wait_s=10):
    import urllib.request, urllib.error
    base = "http://127.0.0.1:%d" % port
    try:
        urllib.request.urlopen(base + "/ping", timeout=0.8).read()
        return True
    except Exception:
        pass
    pid_path = os.path.join(HERE, "banner.%d.pid" % port)
    pid = None
    if os.path.exists(pid_path):
        try:
            pid = int(open(pid_path).read().strip() or 0) or None
        except Exception:
            pid = None
    if pid:
        # 旧 pid 假死则杀掉
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
    _spawn_detached(sys.executable, [BANNER, "serve", "--port", str(port)])
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/ping", timeout=1.0).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _coerce(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "1", "yes", "y", "on"):
        return True
    if low in ("false", "0", "no", "n", "off"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# 这些 key 的值必须原样保持字符串，绝不能做数字/布尔转换。
# 否则：`type --text "13900002222"` 会被转成 int → pyperclip 抛异常、回退的
# pyautogui.write(int) 再抛 "TypeError: 'int' object is not iterable"，
# 表现为「输入纯数字（手机号/验证码/金额）必然失败」。2026-09-15 实测踩到。
STR_KEYS = {"text", "json", "out", "template", "title", "subtitle", "keyword", "note",
            "keys", "rgb", "condition", "then", "else", "every", "script", "region", "scales"}


def parse_argv(argv):
    """['click','--x','100','--y','200','--exact'] -> ('click', {'x':100,'y':200,'exact':True})"""
    if not argv:
        return None, {}
    op = argv[0]
    args = {}
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("--"):
            i += 1
            continue
        key = tok[2:].replace("-", "_")
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            args[key] = argv[i + 1] if key in STR_KEYS else _coerce(argv[i + 1])
            i += 2
        else:
            args[key] = True
            i += 1
    return op, args


def http_get(url, timeout=1.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url, payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _spawn_detached(py, args):
    """以完全脱离的方式启动 svc。

    关键：从 Bash/WorkBuddy 工具启动的后台进程会随该次调用结束被整个进程组
    回收（即便 DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 也不一定够），
    导致 svc 每次都被杀掉重启（表现为每次都慢 3-5 秒 + `_svc_booted: true`）。
    真正最稳的方案：cmd /c start /B —— Windows 的 `start` 内部用 CreateProcess
    把子进程完全脱离当前控制台/进程组，沙箱收不到。
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    # 优先 pythonw.exe（与 python.exe 同目录）
    cand = py
    if py.lower().endswith("python.exe"):
        pw = py[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(pw):
            cand = pw

    # 用 cmd /c start /B 是 Windows 上最稳的"完全脱离父进程"方式
    cmdline = 'start /B "" ' + subprocess.list2cmdline([cand] + args)
    try:
        return subprocess.Popen(cmdline, shell=True,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True,
                                creationflags=0x08000000)
    except Exception:
        # 退化方案：pythonw + DETACHED_PROCESS
        return subprocess.Popen([cand] + args, creationflags=flags,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                close_fds=True)


def ensure_svc(port, wait_s=90):
    base = "http://127.0.0.1:%d" % port
    try:
        http_get(base + "/ping", timeout=0.8)
        return base, False
    except Exception:
        pass
    # ---- 自愈（第 4 轮）：svc 假死/僵住时 ping 不通，先杀掉旧进程再拉起 ----
    pid = _read_pid(port)
    if pid and _pid_alive(pid):
        _taskkill(pid)
    elif pid is None:
        # 没有 pid 文件也尝试按端口查占用进程（防止 pid 文件丢失的旧 svc 占着端口）
        pid = _pid_on_port(port)
        if pid:
            _taskkill(pid)
    # 拉起（脱离进程组，避免随本次调用结束被回收）
    _spawn_detached(sys.executable, [SVC, "serve", "--port", str(port)])
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            http_get(base + "/ping", timeout=1.0)
            return base, True
        except Exception:
            time.sleep(0.3)
    raise SystemExit(json.dumps({"ok": False, "error": "svc_start_timeout"}, ensure_ascii=False))


def _pid_file_path(port):
    return os.path.join(HERE, "svc.%d.pid" % port)


def _read_pid(port):
    try:
        with open(_pid_file_path(port)) as f:
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


def _taskkill(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _pid_on_port(port):
    """netstat 查监听该端口的 PID（pid 文件丢失时的兜底）。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10)
        out = r.stdout.decode("utf-8", "ignore")
        for line in out.splitlines():
            if ":%d" % port in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        return int(parts[-1])
                    except ValueError:
                        pass
    except Exception:
        pass
    return None


def main():
    argv = sys.argv[1:]
    port = DEFAULT_PORT
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            port = int(argv[i + 1])
            argv = argv[:i] + argv[i + 2:]
    # 客户端 HTTP 超时：默认 180s；wait-* 长 op 可自行加大/缩小。
    # 注意用 --http-timeout（不能叫 --timeout：wait-text/wait-stable 的轮询超时也叫 --timeout，
    # 同名会在下面被剥离导致 op 收不到自己的 timeout 参数 —— 第4轮实测抓到的真 bug）。
    http_timeout = 180.0
    if "--http-timeout" in argv:
        i = argv.index("--http-timeout")
        if i + 1 < len(argv):
            try:
                http_timeout = float(argv[i + 1])
            except ValueError:
                pass
            argv = argv[:i] + argv[i + 2:]
    op, args = parse_argv(argv)
    if not op:
        print(__doc__)
        return

    # banner / banner-hide 走 banner 独立端口（不经 svc）
    if op in ("banner", "banner-hide"):
        ensure_banner(BANNER_PORT)
        payload = dict(args); payload.pop("op", None)
        if op == "banner-hide":
            url = "http://127.0.0.1:%d/hide" % BANNER_PORT
            data = b""
        else:
            url = "http://127.0.0.1:%d/set" % BANNER_PORT
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"} if data else {}),
                timeout=2.0).read()
            sys.stdout.write(r.decode("utf-8") + "\n")
            sys.exit(0)
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": str(e)},
                                       ensure_ascii=False) + "\n")
            sys.exit(2)

    base, booted = ensure_svc(port)

    # ---- 第 6 轮安全网：window 类 op 的收尾自动还原 ----
    # 背景：把 WorkBuddy 藏起来/挪走之后忘了还原，用户回到电脑前发现窗口
    # 不见了（已发生过一次，用户来问"为什么打不开"）。
    # 约定：win-stash / win-move / win-hide 默认在**本次命令结束时**立刻还原。
    # 需要跨多条命令保持隐藏的（例如连续多次 shot+OCR），显式加
    # --keep 表示"先别还原，我后面自己调 win-restore-all"。
    RECOVER_OPS = {"win-stash", "win-move", "win-hide"}
    keep = bool(args.pop("keep", False))
    did_stash = op in RECOVER_OPS and not keep

    payload = dict(args)
    payload["op"] = op
    try:
        r = http_post(base + "/cmd", payload, timeout=http_timeout)
    except urllib.error.HTTPError as e:
        r = {"ok": False, "error": "http_%s" % e.code,
             "detail": e.read().decode("utf-8", "ignore")[:600]}
    except Exception as e:
        r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if booted:
        r["_svc_booted"] = True

    # 自动还原（只还原本 op 刚碰过的那个 hwnd，避免误动别人寄存的窗口）
    if did_stash and r.get("ok") and args.get("hwnd"):
        try:
            rr = http_post(base + "/cmd",
                           {"op": "win-restore", "hwnd": int(args["hwnd"])},
                           timeout=10)
            r["_auto_restore"] = {"ok": rr.get("ok"), "hwnd": int(args["hwnd"])}
        except Exception as e:
            r["_auto_restore"] = {"ok": False, "error": str(e)}

    sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(0 if r.get("ok") else 2)


if __name__ == "__main__":
    main()
