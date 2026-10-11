# 三文鱼大模型 v1.5.0（salmon-desktop-control）

Windows 桌面级 UI 自动化技能：**鼠标/键盘控制 + 屏幕图像识别**。让 AI 代理能"看见并操作"Windows 桌面——整屏/区域/按窗口截图、模板找图、OCR 读字定位、按颜色找目标、窗口置顶与恢复、屏幕操作横幅、本地反射回路 guard（模型不在环，可实时操控小游戏/音游）。

> ## 🚀 本版（v1.5.0）拥有更强的看图能力
> 相比已发布的 v1.3.6 版（[salmon-desktop-control](https://github.com/485148606/salmon-desktop-control)），这一版重点升级了"看"与"打"：
>
> - **WGC 捕获通道（Windows Graphics Capture）**：被遮挡、DPI 缩放、独占全屏的窗口都能满分辨率实时取帧，端到端 18–22ms——修掉了 PrintWindow 在 DPI 无关窗口上"只印出左上 1/4"的顽疾，取帧坐标 1:1 零换算；
> - **游戏级操控**（v1.5.0 新增，《我的世界》开发实测驱动）：`move-rel` 相对鼠标位移（DirectInput/Raw Input 游戏专用）、`mouse-hold` 按住鼠标 N 秒（挖矿/蓄力）；
> - **guard 反射回路**：按 fps 抓图→判条件→出手全在本地循环，模型不在环，实测 23–81 fps，支持 `--via post` 后台投递（目标窗口不在前台也能操控）；
> - **遮挡自愈**：`win-front` 强拉前台 + `probe` 自动判遮挡与半分辨率，并给出修复建议；
> - **中文账户 OCR 修复**：cv2.imwrite 在非 ASCII 路径（中文用户名的 %TEMP%）必现故障已修。

## 文件构成

| 文件 | 作用 |
|---|---|
| `SKILL.md` | **完整手册**（30+ 节，全部来自实机实测复盘：坐标体系、感知-行动循环规范、窗口铁律、速度教训、缺陷台账） |
| `scripts/svc.py` | 常驻本地 HTTP 服务（127.0.0.1:8765），重量级依赖只加载一次 |
| `scripts/mc.py` | 唯一客户端入口：`shot` / `ocr` / `locate-text` / `click` / `type` / `key` / `find-color` / `guard` / `win-*` 等 40+ 操作 |
| `scripts/ocr_worker.py` | OCR 独立进程（rapidocr 常驻 daemon） |
| `scripts/wgc_worker.py` | WGC 抓帧独立进程（共享内存双缓冲） |
| `scripts/banner.py` | 屏幕顶部操作横幅（让用户实时看到 AI 在做什么） |
| `scripts/common.py` | 公共工具 |
| `requirements.txt` | 依赖清单 |

## 快速上手

```bash
pip install -r requirements.txt        # pyautogui mss opencv-python-headless numpy Pillow rapidocr-onnxruntime
python scripts/svc.py start            # 启动常驻服务
python scripts/mc.py ping              # 健康检查
python scripts/mc.py shot --out shot.png          # 全屏截图
python scripts/mc.py locate-text --text 确定      # 按文字定位
python scripts/mc.py click --x 500 --y 400        # 点击
python scripts/mc.py find-color --rgb 255,45,45 --tolerance 30   # 按颜色找目标
python scripts/mc.py guard --region 260,300,900,660 --condition '{"rgb":"255,45,45","tolerance":30,"min_area":20}' --then '[{"op":"key","key":"up"}]' --fps 25 --timeout 8   # 反射回路
```

更多（窗口操控、batch 批处理、双形态/三形态捕获选型、并发边界、缺陷台账）见 `SKILL.md`。

## 适用场景

「自动操作某个软件或网页」「点击屏幕上的按钮」「找元素给坐标」「识别屏幕文字」「实时看画面操控游戏」「长跑批量 UI 闭环」——只支持 Windows 真实桌面会话。

---

*本仓库与 [v1.3.6 版](https://github.com/485148606/salmon-desktop-control) 并存，互不覆盖。*
