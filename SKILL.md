---
name: 三文鱼大模型 v1.5.0
description: >-
  桌面级 UI 自动化技能（Windows）。控制鼠标（移动/单击/双击/右键/拖拽/滚轮/按住）与键盘
  （打字/快捷键/后台投递），并用屏幕图像识别定位 UI：整屏或区域截图、按窗口截图
  （PrintWindow / WGC，被遮挡也能看）、模板找图、OCR 读字与按文字定位、按颜色找目标、
  窗口置顶与恢复、操作横幅，以及本地反射回路 guard（模型不在环，可实时操控小游戏/音游）。
  当用户要求「自动操作某个软件或网页」「点击屏幕上的 XX 按钮/图标」「移动鼠标/双击/右键/
  拖拽/滚动」「找 XX 在哪、给坐标」「识别屏幕上的文字」「截图看屏幕」「模拟键盘输入/快捷键」
  「三文鱼大模型 / 桌面鼠标控制」「实时看画面去操控游戏」时使用。
  正文只保留每次动手都生效的核心规则；各场景的实战细节按 §0 场景索引外置，命中才读。
display_name: 三文鱼大模型 v1.5.0
alias: [三文鱼大模型, 三文鱼, salmon, 桌面鼠标控制]
version: 1.5.0
---
# 三文鱼大模型 v1.5.0（salmon-desktop-control）—— 鼠标控制 + 屏幕图像识别（Windows 桌面 UI 自动化）

> 🚨 **【硬红线 · 2026-09-15 用户明确要求】永远不要把用户的 WorkBuddy 主窗口 `win-move` 移出屏幕。** 用户原话："不要一执行就把我的 work 关闭死，我都打不开了"。要躲遮挡就改用 `shot-pw --hwnd <目标>`（PrintWindow，不受遮挡影响）+ `win-activate` 切换前台。详见 §0.5.4 第 1 条。
> 🚨 **【引用本技能铁律】主代理必须先完整通读本 SKILL.md 全文并严格遵守，再动手。** 不得只扫一眼 description 就开干——漏读章节必翻车（本轮实战就因没读透 `banner.py` 实现，横幅一整轮 missing 才被用户抓出）。下文所有章节（感知-行动循环、横幅节奏、窗口铁律、速度教训十则、已知限制、收尾纪律）引用即对该对话**长期生效、跨多轮有效**，中途换话题也不失效。

> **正式名：三文鱼大模型 v1.5.0**（技术名 `salmon-desktop-control`；旧名 `mouse-control` 已在第 4 轮删除，请勿再引用）
> ⚠️ **本技能在本机上有两份副本，改完必须同步并对 md5**：
> `~/.qoder-cn/skills/salmon-desktop-control/` 与 `~/.workbuddy/skills/salmon-desktop-control/`。
> 老文档在这里写的是「技能只有一份」，**那是错的**——本轮实测两副本一旦分叉，
> 回归里的「两份一致」检查立刻失败，而且请求打到哪份全看运气。
> 内部无 LLM：OCR 用 rapidocr，控制逻辑 = `svc.py` / `mc.py` / `ocr_worker.py` / `banner.py` 四件套。
> **v1.3.0 = 第 14 轮（2026-10-02 贪吃蛇+场景矩阵实测）：连续动作原语（按住/高频）、遮挡自愈置顶、find-color 颜色定位、guard 本地反射回路。详见文末「## 🚀 v1.3.0 连续操控与遮挡自愈」。**
> v1.22.2 = 第 9 轮（2026-09-15）：修截图/取色 R、B 通道互换。
> **v1.22.1 = 第 8 轮（2026-09-14 微信开发者工具实地考察复盘）：新增 `win-move` / `win-show` 两个 op（文档一直要求把 WorkBuddy 挪出屏幕却从没有对应命令）；新增 §0.5.5「沙箱里怎么真正启动 GUI 程序」、§0.5.6、§0.5.7。**
> v1.22 = 第 7 轮（2026-09-09 实战复盘）：新增「感知-行动循环规范」§0.5 —— 最高优先级，每次操控先读它。
> v1.2.1 对应第 6 轮（**1h23m 马拉松实测 1531 轮闭环 + 修 2 bug + wake 升级 SendInput + svc 补齐 monitors**）。
> v1.2.0 对应第 5 轮改进（**banner 屏幕顶部操作横幅**——告别盲 hide WorkBuddy）。
> v1.1.0 对应第 4 改进（OCR daemon 常驻化 + 看门狗自愈 + find + max_side）。

## 0 场景索引（动手前先扫这张表）

**核心正文只写"每次动手都生效"的规则；每个场景的实战细节外置在下面这些文件里。**
命中右列任何一条场景时，**必须先读那个文件再动手**——不要凭印象，那些条目每一条都是真翻过车才写下来的。

| 什么情况（命中任一就去读） | 读这个文件 |
|---|---|
| 要操作**微信小程序 / 微信开发者工具**；模拟器里点击没反应、tabBar 不切页、原生 picker 滚轮驱动不了、要改云函数超时或建集合 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-微信开发者工具与小程序/模拟器点击与原生picker.md`<br>`E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-微信开发者工具与小程序/云开发控制台实操.md` |
| 要**装软件 / 点安装向导**；窗口点了没反应；怀疑是**管理员权限(UIPI)**窗口；安装器截图整片白 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-安装器与提权窗口/安装器与UIPI边界.md` |
| 要在**网页表单**里填邮箱/手机号/验证码；输入法把 `@ : /` 打成中文标点；要用"运行"框开网址 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-网页表单与输入法/网页表单与输入法干扰.md` |
| 要操作**网盘/桌面客户端**，或面对 **Windows 打开/保存文件对话框**（手输完整路径没反应）；客户端 splash 卡死、窗口一直黑 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-桌面客户端与文件对话框/文件对话框与splash卡死.md` |
| 要在 **WorkBuddy 沙箱 Bash 里启动 GUI 程序**，但进程一执行完就没了 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-沙箱里启动GUI/沙箱启动GUI.md` |
| 要**长跑批量闭环**（几百上千轮 type/截图/OCR 校验）；`batch` 里放 wait 类 op 报连接重置 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-长跑闭环与OCR校验/马拉松长跑闭环.md` |
| **横幅不显示 / 忘了收尾关横幅**；要改 banner 的 alpha 或 visible 语义 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-banner操作横幅/banner历史bug复盘.md` |
| **取色对不上**、存出来的 PNG 红蓝反了、模板匹配分数长期偏低 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/场景-色彩取值与通道顺序/RB通道互换复盘.md` |
| 要找回**已退役的 screen.py / mouse.py**（老一次性脚本）或查"老命令 → 现命令"对照 | `E:/C_Drive_Reloc/Libraries/Documents/三文鱼大模型附录/README.md` |

> 表里没有的情况 = 用核心正文就够，别去翻附录。附录是给"撞过墙的人"留的地图。

## 0.5 🚨 v1.22 感知-行动循环规范（最高优先级，每次操控必须执行）

**背景**：2026-09-09 实战（Sketchfab 网页下载 114k 面模型）暴露"慢得出奇"的根源不是工具，而是循环不规范——按钮在视口外反复找不到、焦点被抢导致 OCR 读错窗口、每步跨进程丢状态。以下规范直接消灭这些浪费。

### 0.5.1 每次截屏前的"两件套"（每循环必做，不是只做一次）
1. **窗口最大化全屏**：确认目标窗口最大化占满屏幕（网页类可 F11）。
2. **网页缩放到最小**：对网页按 `ctrl+-`（2~4 档）调到最小，**所有网页一律如此**——让整页按钮/面板/底部区块全部进入视口。原因：网页折叠面板（如 Sketchfab"下载3D模型"展开区）常整段在视口外，100% 缩放下 OCR 永远找不到；缩到最小一屏看全，一步定位。
3. 然后才截屏（`shot`/`shot-pw`）+ OCR。

> 口诀：**"先全屏、再缩放、后截屏"——每一次循环都重来，别指望上一次的状态还在。**

### 0.5.6 只动工作区、不碰别人的窗口：`win-move` / `win-show`

- `win-move` 用 `SWP_NOZORDER|SWP_NOACTIVATE`，**不抢焦点、不改 z-order**，纯挪位置；挡住的窗口挪出屏幕是比 `win-hide` 更温柔的做法（用户任务栏还能点到）。
- **最大化的窗口直接 MoveWindow 会被系统弹回原位**（现象：返回的 x/y 跟没挪一样）—— `win-move` 内部已先 `IsZoomed` 判断并 `SW_RESTORE`，不用自己处理。
- **挪出去必须挪回来**。WorkBuddy 本体尤其会在几秒内自行恢复原位置，所以"一次挪走、后面十几步都指望它不在"是行不通的，**每次循环都要重挪**。
- 🚨 **哪些窗口能动、哪些绝对不能动（2026-09-15 用户红线）**：
  - ✅ 可以挪/藏：**目标程序自己的辅助窗口**（如微信开发者工具的"云开发控制台"子窗口、目标软件自身弹出的面板）。
  - ❌ **绝对不能动**：**用户的工作窗口**（WorkBuddy 主窗口）、用户自己的浏览器/编辑器/文档等个人窗口。挪走或藏掉它们会让用户"找不到、打不开自己的软件"，属于翻车级操作。**WorkBuddy 主窗口永远只用 `win-activate` 之外的手段绕开遮挡 —— 即改用 `shot-pw` 抓目标窗口。**

### 0.5.7 截图路径别踩反斜杠

- `mc.py shot --out` 的路径用 **正斜杠**。写成 `D:\dir\name.png` 时，shell 会把 `\n` 当转义吃掉，**文件会静默落到 `D:\dirname.png`（少一级目录）**，接下来 `Image.open` 直接 FileNotFoundError。同理裁剪脚本里也用 `/`。

### 0.5.2 banner 节奏化（让用户知道到哪一步）
- **出手操控前**：`mc.py banner --subtitle "<当前环节>"` 显示横幅
- **截屏完成、进入思考/分析时**：`mc.py banner-hide` 立即隐藏（不挡用户看页面，用户也不会误以为还在操作）
- **思考完再次出手前**：再 `banner --subtitle "<下一环节>"` 显示
- 即：**banner 跟着"动手"走，不跟着"动脑"走**。任务结束必须 banner-hide。

### 0.5.3 思考强度分级（提速关键）
- **感知-行动循环内（截屏→OCR→定位→点击）**：轻量快速决策，**单脚本闭环**——把"激活窗口→缩放→截屏→OCR→条件点击→验证"写进**一个 python 进程**一次跑完。禁止拆成多轮对话式工具调用：每跨一次进程 = 焦点丢一次 + WorkBuddy 抢一次前台 + 慢 5~20 秒。
- **正常规划/排障思考**：最大强度不变。快慢分明：**手上快，脑里深**。

### 0.5.4 速度教训十则（2026-09-09 实战失败原因，每条都真翻过车）
1. **WorkBuddy 抢前台** → OCR 读到的"按钮"全是聊天记录文字，白找半天。
   🚨 **【2026-09-15 用户明确禁止】绝对不要把用户的 WorkBuddy 主窗口 `win-move` 移出屏幕** —— 用户原话"不要一执行就把我的 work 关闭死，我都打不开了"，挪走后用户找不到、打不开自己的软件，体验极差。
   **正解（不碰用户窗口）**：
   - **截图改用 `shot-pw --hwnd <目标窗口>`（PrintWindow）**：它直接抓窗口自己的位图，**完全不受遮挡影响**，WorkBuddy 在前台也照样能抓到目标内容。返回的 `width/height` 就是窗口物理尺寸、`left/top` 是窗口原点，换算 `屏幕坐标 = 图片坐标 + left/top` 即可（实测 DevTools 窗口 (1,1) 3094x1942，图片 1:1，直接 +1 就行）。
   - **只有"点击/OCR"才需要目标在前台**：用 `win-activate --hwnd <目标>`（这是正常的窗口切换，用户窗口仍在任务栏、随时可用），并在每次点击前重新 activate 一次；点完立刻用 `shot-pw` 验收，不受 WorkBuddy 弹回影响。
   - 需要 OCR 定位元素时：先 `win-activate` 再 `locate-text`，或把 `shot-pw` 存下的 PNG 用本地 rapidocr 自己识别（`ocr_worker.py` / rapidocr 已在 venv 里）。
   - **收尾仍然保持**：`banner-hide` 必做。
2. **跨进程间隙焦点丢失** → 每条命令一个进程，间隔期窗口失焦/被抢。**对策：单进程闭环**（见 0.5.3）。
3. **面板展开在视口外** → 点了开关"看起来没反应"，反复空点。**对策：ctrl+- 缩到最小再找**（见 0.5.1）。
4. **mss 全屏截到黑屏**（浏览器/CEF 渲染丢失假象）→ 误判页面坏了。**对策：`shot-pw`（PrintWindow）抓窗口真实内容兜底**。
   🚨 **2026-09-19 重要修正**：原写"判断页面状态以 shot-pw 为准"——**实测是反的**！PrintWindow 对 Electron/CEF 窗口会返回**陈旧缓存帧且完全不报错**（`black:false` 照样骗你，两张图二进制可以完全相同）。**验证"操作有没有生效"一律用 batch 内 `{"op":"win-activate"}+{"op":"shot"}` 的 mss 全屏截图**；`shot-pw` 只用来"看内容"，绝不用来判断"有没有变化"。详见文末第 12 轮。
5. **点击无效果** → 落点被浮窗挡住（如 UE"内容浏览器"压在目标窗口上）。**对策：点击前用 WindowFromPoint 验证落点窗口归属**，不符先 win-activate 提到最上层。
6. **Enter 键丢失 / IME 把 ASCII 吞成中文**（邮箱被打成"@侵权。co'm"）→ **对策：关键文本一律剪贴板粘贴（mc.py type 就是粘贴）**；导航 ctrl+l + 粘贴后要 OCR 验证 URL 再继续。
7. **shot-pw 图像坐标 ≠ 屏幕物理坐标** → 按窗口抓图算的坐标点到别处。**对策：定位/点击一律用 mss 全屏物理截图（1:1）**；PrintWindow 只用来看内容，不用来算坐标。
8. **Edge 136+ 默认 profile 忽略 --remote-debugging-port** → CDP 起不来。**对策：独立目录 `--user-data-dir=E:\edge_cdp --remote-debugging-port=9222`**（2026-10-05 重装后该 profile 实际在 `E:\edge_cdp`，旧文档写的 `D:\` 已不存在）+ playwright-core connectOverCDP 接管；登录态在该 profile 登一次永久有效。
9. **发对话消息 = WorkBuddy 必抢前台** → 一条消息毁一次循环。**对策：静默推进，只在需决策/交付时发消息**（顺便省积分）。
10. **页面重载后滚动位置被恢复** → 误判"没跳转"。**对策：导航后重新 OCR 校对视口内容**，不沿用旧坐标；必要时 Home/PageUp 归位。

把"看屏幕 → 想坐标 → 点鼠标"做成一条链：都由**常驻服务 `svc.py` + 客户端 `mc.py`** 承担
（`shot`/`find`/`ocr`/`locate-text` 是眼睛，`click`/`drag`/`scroll`/`type`/`key` 是手），每条命令返回**单行 JSON**，方便继续编程处理。
**唯一入口是 `mc.py`**（`screen.py` / `mouse.py` 两条老骨架已于 v1.3.5 退役，见「已退役」节）。
**v1.2.0 新增** `banner.py` —— 屏幕顶部操作横幅：第 5 轮改进，替代"盲 hide WorkBuddy"，让用户实时看到 AI 当前环节。

## 📌 使用元规约（引用本技能即整段对话长期生效）

> 以下三条是**元规则**：只要当前对话框引用了本技能，就对**整段对话的每一次交互**长期生效，无需重复提醒；用户中途换话题、代理切换子任务、对话跨多轮都不例外。

1. **省积分：后台思考，少发对话。** 思考、规划、等待截图/OCR、编排多步操作时，**不要把中间过程一条条发成对话消息**——那会大量消耗积分。应**挂在后台静默推进**：在内部完成「看屏 → 推断 → 执行 → 验证」闭环，只在遇到**需用户决策**（二选一 / 确认风险 / 授权）或**任务最终交付**时，才发一条对话。**能用一次工具调用解决的事，就不要拆成多条消息。**

2. **技能规定对整段对话长期生效。** 本技能内的所有约定（坐标体系、横幅、窗口铁律、排障、本条元规约等）一旦引用即绑定整个对话，跨多轮持续有效；不要因为对话变长或切换了子目标就「忘记」这些规则。

3. **搞不定，直接用技能操控电脑解决。** 遇到难以直接推断、纯推理无法确认的事（界面长什么样、按钮在哪、状态对不对），**不要空想或猜**，直接用本技能去「看屏幕 + 操控电脑」拿到事实：截图核对、OCR 取字、定位点击、读窗口状态。实物操作比脑补可靠——**优先动手验证，再下结论。**

## 运行环境（可移植，换机即用）

- 技能根目录：把 `salmon-desktop-control/` 放进你的技能目录即可——
  WorkBuddy 用户级：`<你的 workbuddy 配置>/.workbuddy/skills/salmon-desktop-control/`，
  或项目级：`<项目>/.workbuddy/skills/salmon-desktop-control/`。
- 解释器：任意 Python 3.10+，**不写死路径**。本文档统一用 `PY` 占位，自行替换：
  - 普通情况：`PY="python"`（PATH 中需有 python）
  - WorkBuddy 托管 venv：`PY="<workbuddy>/binaries/python/envs/default/Scripts/python.exe"`
- 技能脚本目录统一记为 `SCR="<技能根目录>/scripts"`
- 依赖：`pyautogui mss opencv-python-headless numpy Pillow rapidocr-onnxruntime`
- 新机首次：`python -m pip install pyautogui mss opencv-python-headless numpy Pillow rapidocr-onnxruntime`

> ⚠️ 全文 `PY` / `SCR` / 截图路径都是占位符，按你机器替换；不要照抄示例里的 `Administrator`、`D:/workbuddy/...` 等绝对路径。

## 🚀 常驻加速版：svc.py + mc.py（推荐，替代直接调 screen.py/mouse.py）

**为什么慢（旧方案）**：每次调用 screen.py/mouse.py 都是全新 Python 进程——解释器启动 ~0.3s、
import cv2/numpy ~1.5-2.5s、RapidOCR 模型加载 1-3s。一个"截图→OCR→点击→再截图"循环 ≈ 10-20s。

**新方案**：
- `svc.py`：常驻本地 HTTP 服务（默认 `127.0.0.1:8765`），把重量级依赖只加载一次；截图/鼠标/窗口操作在进程内，单命令 <100ms。
- `mc.py`：客户端，第一个词是 op，后面 `--key value`。svc 没起会自动拉起；**第 4 轮加自愈**：svc 假死/无响应时自动读 pid 文件杀掉旧进程再拉起，不再"接而不答"永久挂起。
- `ocr_worker.py`：OCR 推理常驻 **daemon**。⚠️ 血泪：rapidocr(onnxruntime) 在 socketserver 请求线程内推理 100% 卡死（实测独立进程正常）——所以 svc 截图后把图片交给**常驻 daemon** 跑 OCR（stdin/stdout JSON 行协议）。第 4 轮改成 daemon 模型，RapidOCR 模型只加载一次，不再每次冷启。**不要再把 rapidocr 拿回 svc 线程里跑。**
- daemon 卡死超时（默认 90s）→ 自动 kill 重建；svc 整体假死（op 超 300s）→ watchdog 自杀等客户端自愈重启。

```bash
PY="python"   # 或你的 WorkBuddy 托管 venv python 绝对路径
MC="$SCR/mc.py"   # SCR = <技能根目录>/scripts

"$PY" "$MC" ping                                # 服务是否活着（正常 <10ms；返回 ocr_daemon/inflight/last_op_age_s 健康字段）
"$PY" "$MC" shot --out D:/x.png --region 0,0,800,600 --require_content 1
"$PY" "$MC" shot-pw --hwnd 198788 --out D:/x.png        # PrintWindow 抓窗口（黑屏兜底）
"$PY" "$MC" ocr --region 200,200,600,300                 # 区域 OCR（~150-500ms 热态）
"$PY" "$MC" ocr --max_side 1920                         # 全屏 OCR（~1.5-2.5s 热态；可调精度档位 640/900/1280/1920）
"$PY" "$MC" locate-text --text 使用教程 --region 0,80,1200,900
"$PY" "$MC" click-text --text 下一步 [--offset_y 30]     # 找字并点
"$PY" "$MC" find --template D:/icon.png --confidence 0.7 [--scales 0.9,1.0,1.1]  # 模板匹配找图标（第4轮补齐到 svc）
"$PY" "$MC" wait-text --text 编译完成 --timeout 30       # 轮询等文字出现（OCR 连续失败2次立即报错，不空等超时）
"$PY" "$MC" wait-stable --hwnd 7146434 --timeout 40      # v1.3.6：等"这个窗口"稳定（别处重绘不打扰，被遮挡也照判）
"$PY" "$MC" wait-stable --region 0,80,1200,1800 --timeout 40   # 等屏幕上这块区域稳定（共享桌面上易误判）
"$PY" "$MC" click --x 100 --y 200 --count 2
"$PY" "$MC" scroll --amount -6 --x 500 --y 700           # 负数向下滚
"$PY" "$MC" type --text "中文"
"$PY" "$MC" win-list --keyword 演唱会                     # 找窗口（DevTools 标题=项目名）
"$PY" "$MC" win-activate --hwnd 7146434                  # 安全前置（禁用 TOPMOST！）
"$PY" "$MC" win-hide --hwnd 25755890                     # 藏起遮挡窗口（如云开发控制台）
"$PY" "$MC" win-show --hwnd 25755890                     # v1.22.1：把 win-hide 藏起来的窗口放回来（SW_SHOW+SW_RESTORE）
"$PY" "$MC" win-move --hwnd 133122 --x -4000 --y 0       # v1.22.1：把抢前台的窗口（如 WorkBuddy）挪出屏幕；最大化窗口会先自动 SW_RESTORE
"$PY" "$MC" win-move --hwnd 133122 --x 0 --y 0           # v1.22.1：挪回原位（收尾必做）
"$PY" "$MC" wake                                         # 显示器睡眠先唤醒（v1.2.1：SendInput 硬件级，DPMS 睡眠唯一有效手段）
"$PY" "$MC" monitors                                     # v1.2.1：列出全部显示器几何 + DPI scale（多屏/缩放排查先跑它）
"$PY" "$MC" banner --subtitle "正在打开微信 / 第 1 步"   # 第5轮：顶部横幅更新（title 缺省保留）
"$PY" "$MC" banner --title "三文鱼大模型正在操控屏幕，请勿触碰" --subtitle "..."
"$PY" "$MC" banner-hide                                  # 第5轮：自动化结束收尾，关闭横幅
"$PY" "$MC" batch --json '[{"op":"win-activate","hwnd":1},{"op":"shot","out":"D:/a.png"}]'
```

svc 手动管理：`python svc.py serve`（前台，调试用）/ `python svc.py start` / `python svc.py stop`（用 pid 文件兜底杀假死）。
客户端超时：`mc.py ... --http-timeout 60`（默认 180s）。⚠️ 别用 `--timeout`——那是 wait-text/wait-stable 的轮询超时参数，同名会被客户端剥掉（实测 bug，见坑 G）。

**注意**：
- 首次调用含自动拉起 svc，约 2-3s；之后单命令 <100ms（OCR 除外）。
- 在 WorkBuddy 沙箱 Bash 里，svc 会在每次调用结束时被回收 → 每条 mc.py 都重新拉起（首次 ~2-3s）。
  需要真正跨命令常驻时，请在一个**不结束的会话**里跑 `svc.py serve`，或在普通终端跑 `svc.py start`。
- 窗口操作铁律：**前置窗口只用 win-activate（SW_RESTORE+SetForegroundWindow+BringWindowToTop+自动重试）；禁止 SetWindowPos TOPMOST**，否则窗口被压 z-order 底层、mss 永久抓黑。activate 后看 `is_foreground`，false 会自动重试（Windows 前台锁定，坑 H）。
- `is_foreground:true` 不等于视觉可见：目标可能被别的窗口盖住。判别 = 全屏 shot 亮度 >230 且 OCR 0 条 → 先 `win-hide` 盖住它的窗口（坑 I）。
- OCR region 高度别小于 ~200px，太扁整行字会在降采样后丢失（坑 J）。
- 屏幕突然全黑：先 `wake`（**v1.2.1 起为 SendInput 硬件级唤醒**，DPMS 睡眠下移鼠标/F15 无效、SendInput 才有效），别去动窗口；仍黑再用 `shot-pw`（PrintWindow 不依赖显示器）。真物理断电/锁屏只能人工。

## 坐标体系（必读，防点歪）

- 所有命令使用**物理像素**，脚本启动时已设置 Per-Monitor DPI Aware。
  因此 `svc.py` 抓的图坐标与它自己下发的点击坐标完全一致，直接互用。
- 多显示器时坐标可为负（副屏在左侧）。**注意 DPI 是本轮翻车重灾区**：目标窗口若是 DPI 无关程序，
  PrintWindow 出来的图是半分辨率（见 §12.3），此时形态换 `wgc` 或按 `probe` 给的系数换算。
- 经验规则：**先截图确认再动手**。凡是"点击/双击/拖拽"前，若目标坐标来自猜的，先用
  `shot` 或 `find`/`locate-text` 拿到真实坐标，别硬点。

## 调用方式

```bash
PY="python"                       # 或你的托管 venv python 绝对路径
SCR="<技能根目录>/scripts"          # 例如 salmon-desktop-control/scripts
MC="$SCR/mc.py"
"$PY" "$MC" <op> [--key value ...]     # 唯一入口，走常驻 svc，单命令通常 <100ms
```

> ⚠️ 本技能在机器上有**两份副本**，改完必须同步并对 md5：
> `~\.qoder-cn\skills\salmon-desktop-control\` 与 `~\.workbuddy\skills\salmon-desktop-control\`。
> （历史上正因为多处副本导致过行为漂移。）

## 已退役：screen.py / mouse.py（不在技能里了）

`screen.py`、`mouse.py` 是 `svc.py` 之前的"每条命令重启一次 Python"老骨架，能力已被
`mc.py` 全量覆盖且慢一个数量级，**v1.3.5 起从技能目录撤除**。源码、完整命令表、
以及"老命令 → 现命令"对照表都搬到了电脑上的附录目录，需要时去那里读：

> **`E:\C_Drive_Reloc\Libraries\Documents\三文鱼大模型附录`**（打开 `README.md`）
>
> **正常使用不需要读它。只有在改 bug、要拿老实现交叉对答案的时候才去读。**

## 推荐工作流（主代理执行时照此编排）

1. **看**：`shot` 全屏/目标区域截图 → 用 Read 工具查看 PNG，弄清界面布局。
2. **找**：目标有图标 → 让用户给模板图或用 `find`；目标是文字 → `locate-text`；
   眼睛看得到但识别不出 → 缩小 `--region` 后 `ocr` 观察原始识别文本再定匹配串。
3. **动**：把上一步返回的 `{x,y}` 交给同一条 `mc.py`（click/move/drag）。
4. **验**：操作后再次 `shot` 截图确认界面变化，必要时循环 2→4。
5. 涉及 OCR 时每次约 1~5 秒（大屏较慢），优先给 `--region` 裁剪区域提速。

## 已知限制与排障

- **OCR 首次运行**会自动初始化模型（约 1~3 秒），后续快；若报网络/模型下载错误，
  检查 venv 中 rapidocr 模型缓存，或改用 `find`（模板匹配不依赖模型）。
- **find 找不到**：降低 `--confidence`（默认 0.8）；图标被缩放时用
  `--scales 0.8,0.9,1.0,1.1,1.25`；模板应尽量小、无多余背景。
- **半透明/毛玻璃图标**匹配不稳，模板优先取完全不透明的小块特征图。
- 自动化正在运行时，请勿抢占鼠标；把鼠标甩到屏幕角落不会急停（默认 FAILSAFE=off）。
- 被控应用若以**管理员权限**运行，模拟输入可能被拒（UIPI）——需以管理员启动本终端。
- 所有脚本需在真实桌面会话运行；远程桌面/锁屏状态下截图为黑或失败属正常。

### 固有缺点（整合自技能设计纪要 v1.2.0 第 5 节「缺点（如实记录）」+ 实战）

以下是技能本身无法绕过的天花板，引用前心里要有数——别在它们身上反复撞：

- **沙箱回收 svc（本质限制）**：WorkBuddy Bash 里每条命令结束 svc 都会被回收，下次调用重新拉起（首次 ~2-3s）；真正跨命令常驻要在持续会话跑 `svc.py serve` 或普通终端 `svc.py start`。第 4 轮已加自愈，但回收本质未解（banner 因独立端口才幸免，环境全杀时同样挂）。
- **并发能力 = 1**：mss/pyautogui 非线程安全，全 op 串行，多请求只能排队。做"按 region 切块并发 OCR"需先打破这个假设。
- **OCR 漏字（长文本/关键 ID 风险）**：rapidocr 轻量，中文长文本会漏字（fileID 曾漏读中段 → 静默失败）。**关键长 ID 禁止走 OCR**，改用大字渲染 + 接口自检。
- **全屏 OCR 偏慢**：~2s；视频播放等动态画面逐帧识别不稳定。大图优先 `ctrl+-` 缩到最小再识别，或给 `--region` 分块（region 高度别小于 ~200px，见坑 J）。
- **onnxruntime × socketserver 不兼容**：OCR 推理放请求线程 100% 卡死，已用独立 daemon 进程规避，代价是多一跳、daemon 首冷启 ~1s。
- **仅支持 Windows 真实桌面会话**：不支持远程桌面 / 锁屏 / 虚拟显示（黑屏不可用）。
- **模板匹配局限**：半透明/毛玻璃图标易失配，模板优先取完全不透明的小块特征图（同上"find 找不到"那条）。

## 🚀 v1.3.0 连续操控与遮挡自愈（2026-10-02，第 14 轮：贪吃蛇 + 场景矩阵实测）

本轮驱动：要给"音游/动作类自绘 UI"做低延迟操控，并解决"被遮挡就点不中"。以下结论全部来自本机实测（贪吃蛇靶子 + 12 个困难场景矩阵）。

### 10.1 新原语（都在 svc 常驻进程内，单次 <10ms）

```bash
"$PY" "$MC" key-down --key w          # 按住（蓄力/持续移动/音游长按）
"$PY" "$MC" key-up   --key w          # 松开
"$PY" "$MC" mouse-down --button left  # 按住鼠标键
"$PY" "$MC" mouse-up   --button left
"$PY" "$MC" key-hold --key right --hold 1.2   # 按住 1.2s 再松（一次调用完成）
"$PY" "$MC" sleep --seconds 0.3       # batch 里占位等待（连点间隔/动画）
```

> 在 v1.3.0 之前，三文鱼的 `key` 就是 `pyautogui.press()`（按下即松开），**没有"按住"**——任何需要长按的游戏操作都做不了。

### 10.2 遮挡自愈：win-front 与 --hwnd 前置

```bash
"$PY" "$MC" win-front --hwnd 7146434          # 拉到最上端（被遮挡/任务栏最小化都吃）
"$PY" "$MC" locate-text --text 确定 --hwnd 7146434   # 先拉最上端，再在窗口图上找字
"$PY" "$MC" click-text  --text 确定 --hwnd 7146434   # 同上，找到直接点
```

- `win-front` 用 **AttachThreadInput 强拉前台**（后台进程直接 `SetForegroundWindow` 会被 Windows 前台锁忽略，2026-10-02 实测）+ `SW_RESTORE` 从任务栏恢复；返回 `is_foreground` / `was_iconic`。老 `win-activate` 已改走同一路径。
- **`--hwnd` 给了就优先 PrintWindow**（被遮挡时屏幕上没有目标画面，"黑屏才兜底"那一套根本不触发——这是 v1.22 的盲区）。
- 铁律没变：**不能动的窗口只有用户的个人窗口**；把窗口拉到最上端是"看它"的手段，不是"乱动它"。

### 10.3 find-color：游戏/自绘 UI 的正确定位方式

```bash
"$PY" "$MC" find-color --rgb 255,45,45 --tolerance 30 --region 260,300,900,660
"$PY" "$MC" find-color --rgb 255,45,45 --hwnd 7146434     # 被遮挡/后台也能找（PrintWindow）
```

返回 `matches: [{x,y,w,h,area}]`（中心 = 屏幕物理坐标）。**不要用 `find` 模板匹配去定位纯色小块**——实测它会给 `confidence=1.0` 的假命中（命中屏幕/区域左上角）。颜色掩码是唯一稳的。

### 10.4 guard：本地反射回路（模型不在环）

```bash
"$PY" "$MC" guard   --region 260,300,900,660   --condition '{"rgb":"255,45,45","tolerance":30,"min_area":20}'   --then '[{"op":"key","key":"up"}]'   --fps 25 --timeout 8
```

含义：按 `--fps` 反复 抓图 → 评估条件 → 命中后执行 `--then` 一次（还有 `--every` 命中期间每帧、`--else` 未命中每帧），命中或超时退出。返回 `hit / frames / frames_new / elapsed_s / fps_real / fps_fresh / steps_ran / last_point`。
（`frames_new` / `fps_fresh` 是 v1.3.5 加的：推送式通路里「轮询次数」不等于「新画面次数」，见 §12.1。）

- 实测：**PrintWindow 通路 23–80 fps，屏幕抓取通路 81 fps**，命中按键方向真的变了。
  （这组是当天早些时候 4K/250% 环境下的数；当前显示配置的最新三通路数据见 §12.1、§12.6。）
- `guard --hwnd` 同样是 PrintWindow 优先 + 屏幕兜底（与 `_locate` 一致，别一个走窗口一个走屏幕）。
- **模型不在环**——整个循环发生在 svc 进程内。模型应该只做一次性的事：选好 condition、选好 then 动作，然后把逐帧交给 guard。这是"音游级低延迟"的唯一正确架构（模型在环一轮 = 几十格游戏时间，实测 21–58 格）。
- `--else` 会在未命中时每帧执行，容易轰炸输入，慎用。

### 10.5 本轮的测量结论（选型别再靠感觉）

| 指标 | 实测 |
|---|---|
| 单次点击 | 7.5ms（134 次/秒） |
| PrintWindow 抓一帧 | 37–44ms（~23–27 fps） |
| 屏幕区域抓一帧 | 43ms（23 fps） |
| 区域 OCR 800×600 | 33ms |
| 全屏 OCR 1920 | 2.1–3.1s（**永远别全屏 OCR**） |
| guard 反射回路 | 23–81 fps |
| 模型在环一轮 | 21–58 格游戏时间（几十秒） |

### 10.6 用这套打游戏的硬边界（如实记录）

1. **联机反作弊是红线**（EAC/BattlEye/ACE/网易 MC 服务端会检测合成输入，轻则踢线重则封号）。
2. ~~`guard` 的出手仍走 SendInput，需要目标窗口在前台；将来用 PostMessage 直投（v1.4 计划）~~
   **这条已在 v1.3.1 兑现**：`--via post --hwnd` 走 PostMessage 后台投递，
   实测目标全程不在前台也能把一整局打完（§11.1 的 ③pw+post 那轮）。
3. **DirectX 独占全屏/受保护渲染表面**，PrintWindow 大概率给黑帧 → 用 **`--form wgc`**
   （v1.3.5 接进 svc，v1.3.6 挪进独立 worker；一个服务进程只服务一个窗口，
   auto 只在会话已热时复用它，原因见 §12.2）。独占全屏本身仍是硬边界。
4. 高频连点会被目标应用 UI 线程合并（实测 10 次亚毫秒点击只落 1 次），需要间隔 ≥50ms 或在 `batch` 里加 `sleep`。


---

## 11. v1.3.1 双形态（实时游戏实测驱动）

### 11.1 为什么必须双形态

同一台机器、同一个实时接球游戏（tkinter canvas，760×560，60fps，UIA 完全不可见），
5 种形态组合各跑 20 秒、**每轮重启游戏**（否则难度随累计分数递增，是不公平对比），
成绩由游戏自己 POST/写盘，不看任何截图：

| 形态组合 | 每帧中位 | 帧/秒 | 实际按键 | 接住/漏 | 是否抢前台 |
|---|---|---|---|---|---|
| ①screen+send | 69.6ms | 14.2 | **0** | 1/6 | 想抢但被前台锁挡住 |
| ②screen+post | 68.1ms | 14.1 | **0** | 2/5 | 否 |
| ③pw+post | 86.8ms | 11.5 | 163 | 3/5 | **否（全程 230 帧游戏都不在前台）** |
| ④pw+send | 89.6ms | 11.2 | 184 | 3/5 | 是 |
| ⑤auto+post | 96.1ms | 10.3 | 158 | **4/4** | 否 |

**screen 形态两轮按键数为 0**：目标被别的窗口盖住时，抓屏看到的是**别人的画面**，
找不到挡板/球，于是整个回路一步都走不动——而且它还会**误命中**（同一区域里把别的应用的
绿色进度条当成挡板）。pw 形态全程看得见、能在游戏不占前台的情况下完成闭环。
①的"想抢前台没抢到"也是本机实况：并发 agent 环境下 `SetForegroundWindow` 会被前台锁忽略。

**所以：形态不是选一个，是按场景切。**

### 11.2 用法

```bash
# 诊断：现在该用哪个形态、各花多少时间
"$PY" "$MC" probe --hwnd 1314218 --region 0,0,900,700
# → {"form_screen_ms":16.7,"form_pw_ms":18.0,"recommended":"screen","occluded":false,
#     "visible_unoccluded":false,"pw_half_resolution":true,"pw_scale_est":0.5,"wgc_ok":false,...}
# ⚠️ 遮挡看 occluded，别看 visible_unoccluded（那字段实际是"是否前台"）—— 见 §12.4

# 显式指定捕获形态（ocr / locate-text / click-text / find / find-color / guard 都支持）
"$PY" "$MC" find-color --rgb 255,45,45 --hwnd 1314218 --form pw
"$PY" "$MC" locate-text --text 确定 --hwnd 1314218 --form auto

# 出手形态：后台投递（不抢前台、不动鼠标焦点）
"$PY" "$MC" key   --key up   --via post --hwnd 1314218
"$PY" "$MC" click --x 500 --y 400 --via post --hwnd 1314218
"$PY" "$MC" type  --text abc123 --via post --hwnd 1314218     # 走 WM_CHAR

# 反射回路：看画面→判条件→出手，全在本地，模型不在环
"$PY" "$MC" guard --hwnd 1314218 --form pw   --condition '{"rgb":"255,45,45","tolerance":30,"min_area":20}'   --then '[{"op":"key","key":"left","via":"post"}]' --fps 25 --timeout 10
```

`--via` 不传时**默认仍是 SendInput**，老用法一律不变。`guard` 里写了 `--via post` 就不用每步重复 `--hwnd`。

### 11.3 形态选择规则（auto 的实际逻辑）

| 情况 | 选 | 原因 |
|---|---|---|
| 目标**真被遮挡**（z-order 实测，v1.3.5 起）| pw | screen 此时拿到的是遮挡物像素，且会误命中 |
| ↑ 且该窗口的 wgc 会话已热（v1.3.6） | wgc | 取帧 0.4ms 且满分辨率；冷会话不自动开（0.48s 冷启对单发是倒退） |
| 没给 region | pw | 抓整屏既慢又容易命中屏幕上别处的同名目标（实测命中过聊天窗口里的字） |
| 未被遮挡且给了 region | screen | 最快，且是 GPU 合成后的真实画面。**v1.3.5 起不再要求"在前台"**——以前不是前台就被赶去 pw，白白多花 7~20ms |
| 目标**最小化** | 直接报错 `target_minimized` | 最小化窗口不重绘，pw 也拿不到有效画面；先 `win-front` 恢复 |
| pw 失败/黑帧（提权、部分 GPU 合成） | 退回 screen | 保 v1.22 原有能力不断档 |

### 11.4 三遍检查的结论（含一条重要提醒）

1. **第一遍**：29 项全量回归（新旧 op + CLI 传参 + 纯数字 type + banner）→ 0 失败。
2. **第二遍**：与 v1.3.0 基线逐项对比，5 项报"倒退 +33%~+156%"。
3. **第三遍**：加**噪声对照组**（挑几个我一行代码都没碰的 op 连测 9 次）——
   `ping` 中位 0.6ms 但**极差 25.4ms**，`shot` 中位 15.5ms 极差 22.8ms。
   第二遍那些"倒退"全在 ±25ms 噪声带内。同时把开销拆开：
   `find-color(pw) 62.1ms` vs `纯 shot-pw 63.2ms` → **我引入的间接层开销 ≈ -1.1ms，即测不出来**。
   结论：**没有可归因于本次改造的性能倒退**；但请记住一条方法论——
   **在这台并发 agent 的机器上，任何 <25ms 的单次差异都不能当结论，必须用未改动 op 做对照组。**

### 11.5 已知边界（别踩）

- `via=post` 对 tkinter / WinForms / 大多数 Win32 控件有效（实测有效），但 **Chromium/Electron 一类自带输入栈的应用可能忽略合成消息**——遇到就退回 `via=send`（需前台）。
- 本机 Edge 对 `file://` + `--app` 模式**完全不执行页面脚本**（连 `document.title=` 都不生效，实测），网页游戏自动化在这台机器上走不通；这是环境限制，不是技能问题。
- 高频连点会被目标 UI 线程合并，`batch`/`guard` 里要留 `sleep` 或 `post_step` 间隔。


## 12. v1.3.5 第三捕获形态 WGC 与两条被名字骗了的旧逻辑

第 16 轮（2026-10-02）。这轮不是"加功能"，是拿 Qoder 原生 computer-use 的抓帧后端（`nativeProvider=rust-wgc`）
去照三文鱼，结果顺手挖出两条**名字和实现对不上**的老逻辑。数据全部来自同一个自绘 canvas 靶子
（tkinter 760×560 接球游戏，DPI 无关，200% 缩放，屏幕物理 1044×864）。

### 12.1 三条捕获通路，各自的真实边界

| 通路 | 单次取帧 | 被遮挡时 | 图的内容分辨率 | 坐标换算 | 冷启 |
|---|---|---|---|---|---|
| `screen`（mss 抓屏） | 10.6ms | ❌ 拿到的是遮挡物（红球 0 px） | 屏幕物理像素 | 需要 region 对齐 | 无 |
| `pw`（PrintWindow） | 17–30ms | ✅ | **只有左上 1/4**（见 12.3） | 要乘 scale | 无 |
| `wgc`（Windows Graphics Capture） | **0.4ms** | ✅ | 屏幕物理像素 | **1:1，零换算** | 建会话 ~0.3s |

WGC 的坐标正确性是硬验过的：帧尺寸与 `DWMWA_EXTENDED_FRAME_BOUNDS` **逐像素相等**，
且同一静止目标 `find-color --form wgc` 与 `--form screen` 给回**同一个点**（dist=0.0）。

但它是**推送式**——画面更新了才回调。所以取帧几乎不花时间，代价是"帧龄"。
`guard` 里实测：轮询 58.7 次/秒，真正换内容的只有 42.1 帧/秒。
**别把轮询次数当感知次数**，这就是 `frames_new` / `fps_fresh` 两个字段存在的理由。

### 12.2 【v1.3.5 时】wgc 只能显式点单，auto 不选它（重要）

`windows-capture 2.0.1` 在常驻 svc 进程里，新建会话那一步
（`WindowsCapture.start_free_threaded`）会触发 `Windows fatal exception: access violation`，
**整个 svc 进程直接没了**：没有 Python 堆栈，看门狗也不触发（`_MAX_OP_SECONDS=300`，没到），
只有 `PYTHONFAULTHANDLER=1` 才看得见。已上的缓解：

- `WGC_MAX_SESSIONS = 1`：一个 svc 进程只允许一个活跃会话，换窗口返回 `wgc_slot_busy_by_<hwnd>`；
- `_WGC_GRAVE`：被淘汰/窗口已消失的会话，引用**永不释放**（怀疑对象被 GC 后毒化下一次 create）；
- `probe` 默认**不**建新会话（那正是崩溃点），要探路得显式 `probe --wgc_probe 1`。

当时效果：15 轮压力测试全活，但混合长序列仍复现 1 次 —— 所以那时 auto 不敢用它。
**这一条在 v1.3.6 已被 worker 化解决，见 §14.5。**

### 12.3 PrintWindow 在 DPI 无关窗口上只印出左上四分之一（老缺陷，现在能自动识别）

实测：缓冲区 1072×878，**内容只占 536×431**，其余全黑；挡板在图里宽 120px，屏幕上实际 240px。
也就是 pw 的图既半分辨率又坐标要乘系数——这正是历史上"pw 找到了却点歪"的一类根因。
`probe` 现在直接报：`pw_content=[宽,高,缓冲区宽,缓冲区高]`、`pw_scale_est`、`pw_half_resolution`、
以及一句 `pw_fix` 告诉你乘几。

### 12.4 `visible_unoccluded` 名不副实（已更正判据，字段仍保留）

它内部只比了 `GetForegroundWindow() == hwnd`，是**"是否前台"**，不是"是否被遮挡"。
实测一块布盖在它上面，它照样报 True。而 auto 规则从 v1.3.1 起一直拿这个字段当遮挡信号用。

v1.3.5 新增 `_occluded()`：沿 z-order 往上一层层走（`GW_HWNDPREV`），有别的可见窗口与目标矩形相交就判被压；
跳过目标自己的子窗和分层窗口（salmon 自己的 banner 是分层置顶窗，不该算遮挡源）。
`probe` 两个字段都给，`compat_note` 里写明哪个能信。

顺带的提速：判据改对以后，"没在前台但其实没被压住"的目标不再被赶去 pw，走 screen 省 7~20ms。

### 12.5 本轮踩到的环境坑（别再踩）

- **测试道具必须能自己消失。** 上一轮我用无边框 + 置顶的灰布模拟遮挡，脚本一死布就留在屏幕上关不掉，
  用户只能重启电脑，现场全没。现在道具带标题栏 + TTL 自毁，脚本 `finally` 兜底。
- ctypes 取窗口标题：`buf = create_unicode_buffer(256); GetWindowTextW(h, buf, 256); buf.value`。
  写成 `(c_wchar*N)()` 再 `str(...)` 会得到 `<c_wchar_Array_256 object at 0x...>`，
  于是"找不到窗口"——我这次差点把它当成通路失效来查。
- `DwmGetWindowAttribute` 在 `dwmapi`，不在 `user32`；`GetDpiForSystem` 在 `shcore`。
- mss 新版本 `grab()` 返回 ScreenShot 对象不能直接切片，要 `np.array(s.grab(box))`。
- 控制台是 GBK：脚本里打中文会炸，输出走文件 + `PYTHONIOENCODING=utf-8`。
- **拿移动目标做跨通路坐标一致性校验是错的**（球速 22 物理px/帧，两次调用就飘 150px）。
  用几乎静止的目标（挡板）当锚点，或者先确认目标没被压住。

### 12.6 三遍检查（1.3.5）

1. 29 项全量回归：0 失败（含 op 注册数 42）。
2. v1.3.5 专项 16 项：16/16，其中 `find-color` 跨通路 dist=0.0、遮挡下 screen 0 命中而 wgc 命中。
3. 指标对比仍报若干"倒退"项，但**每轮报的项目列表都不一样**（本轮 ping/monitors/pos/shot/find-color/guard，
   上轮 monitors/pos/shot/find-color/guard/win-activate），且包含我一行没碰的 op
   ——符合 §11.4 的噪声带结论：**这台机器上 <25ms 的单次差异不能当结论**。

## 13. 缺陷台账（历轮如实记录 → v1.3.5 现状）

技能从 v1.1.0 起每一轮都把"实际翻过的车"写进 SKILL.md，不散、不删、不改口。
下面把 §固有缺点 + 第 10~16 轮的记录归并成一张表，标出现在到底还堵不堵：

| # | 缺陷 | 首次记录 | v1.3.5 现状 |
|---|---|---|---|
| 1 | 沙箱把 svc 回收，下次调用重拉 ~2-3s | v1.1.0 | **仍在**（本质限制）。另加一条新坑：8765 上会留下**孤儿监听进程**，`svc.py stop` 只按 pidfile 杀，杀不到它 → 本轮实测有两个 svc 抢端口，请求打到旧代码，指标全乱。查端口要用 `netstat -ano | grep :8765` |
| 2 | 并发能力 = 1（mss/pyautogui 非线程安全） | v1.1.0 | **仍在**，但实测串行不是瓶颈（§12.1 里 find-color 14.7ms） |
| 3 | OCR 漏字，长 ID 禁止走 OCR | v1.1.0 | **仍在**。本轮 `ocr --form wgc` 读 HUD 仍会丢数字。语义读取（UIA）是正解，`comtypes` 本机已装，未做 |
| 4 | 全屏 OCR ~2s | v1.1.0 | **仍在**；region OCR 32.7ms 是既有结论，"给 region"永远是第一优化 |
| 5 | onnxruntime 与 socketserver 请求线程不兼容 | v1.1.0 | **已规避**：独立 `ocr_worker.py` daemon |
| 6 | 仅 Windows 真实桌面会话（RDP/锁屏不可用） | v1.1.0 | **仍在** |
| 7 | 模板匹配在半透明/毛玻璃上失配 | v1.1.0 | **仍在**；纯色块场景已由 v1.3.0 `find-color` 接管（`find` 在纯色块上会给 confidence=1.0 假命中） |
| 8 | 管理员/UIPI 窗口点不动 | 第 11 轮 | **仍在**（系统边界）。`_locate --hwnd` 的 pw 失败会自动退回屏幕路径，不再硬报错 |
| 9 | 被遮挡时点不中 | 第 10-12 轮 | **已解**：`win-front` 自愈置顶 + `pw` 抗遮挡 + 本轮 `wgc`；出手侧 `--via post` 后台投递 |
| 10 | 最小化窗口给"pw 失败"误导上层反复换形态 | v1.3.1 | **已解**：给 `target_minimized` + 怎么恢复 |
| 11 | **PrintWindow 在 DPI 无关窗口上只印左上 1/4，坐标要乘系数**（半分辨率） | 本轮新记录（缺陷本身早就在） | **可自动识别**：`probe` 报 `pw_half_resolution` / `pw_scale_est` / `pw_fix`；免换算的正解是 `--form wgc` |
| 12 | **`visible_unoccluded` 名不副实**（只比了是否前台，被布盖着也报 True），auto 一直拿它当遮挡信号 | 本轮新记录（缺陷本身早就在） | **已更正**：新增 `_occluded()` 走 z-order 实测；auto 改用它，副带提速 7~20ms |
| 13 | 推送式通路把轮询次数当感知次数（虚高帧率） | 本轮 | **已加护栏**：`guard` 报 `frames_new` / `fps_fresh` |
| 14 | windows-capture 建新会话会 access violation 打死 svc | 本轮 | **已隔离**（v1.3.6 worker 化，§14.5）：AV 只打死 worker 进程，svc 10ms 自愈重建；第三方原生缺陷本身仍在 |
| 15 | **`_locate` 等四个函数被重复定义，v1.3.1 的两个修复被后定义的老版本压住**（pw 失败不退回屏幕路径 + 每次 --hwnd 都抢前台） | 本轮 | **已删重复块并实测确认修复生效**。教训：改完 svc.py 必须扫顶层重复定义，它不报错只安静地让后一份赢 |
| 16 | `cv2.inRange` 边界 dtype 退化（近黑色 + 宽 tolerance 直接崩，find-color/guard 都中） | 本轮 | **已修**：lo/hi 显式 float64 |
| 17 | op 内部抛异常 = HTTP 500 + 裸 traceback，客户端只能看到 http_500 | 本轮 | **已修**：统一 `op_crashed` 结构化错误 + hint |
| 18 | 抢前台无法被外部约束，全靠代理自觉 | 本轮 | **已修**：`SALMON_ALLOW_FRONT=0` / `mc.py front-policy --allow 0` 服务级硬开关，见 §14.1 |
| 19 | **推送式会话一旦停更，wgc 给的是"看起来完全正常"的旧帧**（循环场景等于闭眼开车，比慢严重得多） | v1.3.6 | **已兜住**：auto 只接受帧龄 <120ms 的热会话帧，超了自动退回 pw 并在 note 里说明，见 §14.6 |

台账的用法：新模型接手时先读这张表，**别在第 1~8 条上重新交学费**；第 14 条现在由 worker 进程替你挡着，
但别因此以为 windows-capture 变安全了（它只是死得离服务远了）。

## 14. v1.3.6 抢前台策略 + 失败即递提示（并记录一次"文档说已修、代码其实没修"的事故）

### 14.1 为什么需要硬开关

共享桌面上常有别的模型在用前台。用户说"别抢前台"时，如果靠代理记住"哪个 op 会抢"，一定漏——
本轮实测 `wait-text --hwnd` 就把靶子拉到前台了，而它在文档里恰恰属于"轮询不该反复抢前台"那一类。

所以做成服务级策略，而不是靠自觉：

```bash
SALMON_ALLOW_FRONT=0 python svc.py start      # 启动时就禁止
python mc.py front-policy --allow 0            # 运行时关掉（1 恢复，默认 1=历史行为）
python mc.py front-policy                      # 查当前状态 + 已挡下几次
```

关掉后的行为（都是有意设计，不是坏掉）：

| op | 策略关闭时 |
|---|---|
| `win-front` / `win-activate` | 直接返回 `front_policy_denied` + 后台替代路径（`--form pw/wgc` 看、`--via post` 出手） |
| `locate-text` / `click-text` / `wait-text` 带 `--hwnd` 的自动前置 | **静默跳过前置**，照常走 PrintWindow（本来就不需要前台） |
| `ping` | 多报 `allow_front` 与 `front_suppressed_times`，一眼看出当前策略 |

### 14.2 失败时把「下一步 + 该读哪个文件」递回来

起因：SKILL.md 的场景索引写的是"遇到 X 去读 Y"，触发权在模型手里，而模型不知道自己缺哪条知识。
下面这些"情况"服务本来就能判断，于是把触发权收归代码——**只在真出问题时**给结果加 `hint`（一行可照做）
和 `must_read`（§0 那张表里的外置文件），正常结果一个字段都不多加。

| 情况（代码怎么判出来的） | hint 说什么 |
|---|---|
| 目标被别的窗口压住（`_occluded` z-order 实测） | 你看到的是遮挡物；用 `--form pw/wgc` 或 `win-front`（后者要前台，需授权） |
| 目标进程提权（`TokenElevation` 实测） | 非提权进程对它既读不到也点不动，用管理员终端重启 svc |
| `--form pw` 空结果且该窗口是半分辨率 | `min_area` 要按平方缩小、坐标要乘系数，或直接 `--form wgc` |
| OCR 读到 0 条 | region 高度别小于 200px / 网页先 `ctrl+-` / 长 ID 禁止走 OCR |
| 颜色 0 命中 | 颜色要取自同一张图、放宽 tolerance、region 只框住画布 |
| `--via post` 发给 Chromium/Electron | 这类应用可能忽略合成消息，改 `--via send`（要前台，先问用户） |
| 画面全黑 | 先 `wake`（DPMS 睡眠只有 SendInput 有效），仍黑走 pw/wgc |
| `wgc` 不可用 / 槽位被占 | 退回 `--form pw`；换窗口要先重启 svc |
| OCR 置信度 < 0.5 | 可能在漏字，关键编号别信 OCR |
| 横幅 `visible:false` | 指向 banner 历史 bug 复盘（改过 banner.py 必须先 stop 再调） |
| op 内部抛异常 | 统一成 `op_crashed` 结构化错误（以前是 HTTP 500 + 裸 traceback，客户端只能看到 http_500） |

两条实现纪律：**跨请求状态必须按请求号隔离**（`_LAST_CAP` 曾被上一条命令的 `hwnd` 污染，
导致给出一条完全无关的错提示）；**无效句柄不等于"被遮挡"**，推导前先 `IsWindow`。

### 14.3 事故记录：`_locate` 有两份定义，v1.3.1 的两个修复其实一直没生效

`op_ocr` / `_norm` / `_text_hits` / `_locate` 整块被重复定义了第二遍（77 行），Python 里后定义的
覆盖前面的，于是**生效的是修之前那份老 `_locate`**：PrintWindow 失败直接硬报错（不退回屏幕路径）、
且每次带 `--hwnd` 的定位都无条件 `_force_front`（就是 14.1 里抢前台的那个动作）。
文档里"已修"两条写了两轮，代码里被自己压住了。已删除重复块，现生效的是修好的那份，并实测确认：
`locate-text --hwnd <无效句柄>` 现在返回 `text_not_found`（说明退回屏幕路径了）而不是 `printwindow_failed`。

**教训（写进台账第 0 条级别）：改完 `svc.py` 必须跑一次"顶层重复定义"扫描。**
一个函数被定义两次不会报错、不会告警，只会安静地让后一份赢——这是所有冲突里最难发现的一种。

### 14.4 顺手修掉的一个潜伏崩溃

`cv2.inRange(img, lo, hi)` 的边界必须显式 `float64`：`max(0, 负数)` 返回的是 **int 0**，
当三个通道都被截到 0 时 `lo` 整体退化成 int64 而 `hi` 还是 float64，OpenCV 直接断言失败
（`lb.type() == ub.type()`）。触发条件就是"目标色接近黑色 + tolerance 略宽"——
深色/暗色主题 UI 正好中招，`find-color` 和 `guard` 都受影响。现在两处都写死 dtype。

另外 `guard` 首帧即命中时（`stop_on_found` 默认开）只有 1 帧，用它算出的 fps 是纯噪声，
现在 `frames < 3` 时 `fps_real` / `fps_fresh` 直接报 `None`。

### 14.5 WGC 已挪进独立 worker（v1.3.6），崩溃不再能打死服务

`scripts/wgc_worker.py` 独占 WGC 会话，帧写进共享内存（seqlock 双缓冲），svc 只读内存：

```
svc  ──stdin JSON──▶  wgc_worker.py（pythonw，无窗口）
     ◀─stdout JSON──   {"ok":true,"name":"wnsm_xxx","w":782,"h":648}
     共享内存 ◀────────  帧头 + 双缓冲（seq 奇=正在写，偶=定稿）
```

实测（当前 150% 缩放、782×648 靶子）：冷启建会话 484ms 一次；之后 `shot-wgc` 端到端 **18–22ms**
（含写 PNG），纯取帧 ~0.4ms；`guard --form wgc` 轮询 58.8/s、**新帧 33.3/s**；换 `--hwnd` 直接切会话
（不再需要重启服务）；**把 worker 进程杀掉后下一次调用 10ms 自动重建**。

`auto` 的新规则：目标被遮挡且该窗口的 wgc 会话**已经热**时走 wgc（0.4ms + 满分辨率），
冷会话仍旧走 pw —— 因为单发场景下 484ms 冷启相对 pw 的 30ms 是实打实的倒退。
要长跑/打游戏就显式点一次 `--form wgc`，之后 auto 会跟着它。

#### worker 化过程中踩到的三个坑（都值得记，因为都是"看着像别人的问题"）

1. **帧头一直发布奇数 seq**：我先写头再写数据，头里永远是"正在写"，读侧按规矩全部拒绝，
   表现成"wgc 一帧都取不到"。正解：先发布奇数占位 → 写数据 → 再发布偶数定稿，槽号由帧号决定。
2. **`frame_buffer` 可能非 C 连续**（row_pitch 带行距填充）→ `memoryview.cast("B")` 直接失败。
   写前统一 contiguous 化（约 0.4ms）。
3. **预猜缓冲区尺寸会偏小**：`GetWindowRect` 拿到的是 DPI 虚拟化后的矩形（536×438），
   而 WGC 帧是物理尺寸（782×648）→ 共享内存开小了，每帧都被暂存、永远写不进去。
   正解：拿到真实帧尺寸后再兜底扩一次容。

另外还有两条属于"自己的补丁没打上却以为打上了"：一处 `assert` 中途抛错让脚本没写盘；
`release` 发 `stop` 没读回复，导致下一条命令把 `{"ok":true}` 当成自己的答复（表现为
`shm_open_failed: 'name'`，看着像共享内存故障，其实是协议串位）。**改完一定要看一眼
真实返回，别信自己的假设。**

依赖补一条：worker 用的是 `sys.executable`，本机是 **pythonw.exe**（好处：不会有控制台窗口闪）。
用 CIM 找它时别按 `Name='python.exe'` 过滤，会找不到。

### 14.6 worker 版进 auto 之后的端到端复验（v1.3.6 收尾，8/8 PASS）

同一靶子（tkinter 520×400，物理 782×648），遮挡道具盖住整窗，全程不抢前台：

| 检查 | 结果 |
|---|---|
| 冷会话时 auto 仍旧走 pw（不擅自付 0.3~0.5s 冷启） | 3/3 次都是 `pw`，中位 13.8ms |
| 显式 `--form wgc` 一次把会话捂热 | 成功，782×648 满分辨率 |
| 会话热了之后 auto 自动改走 wgc | 3/3 次都是 `wgc`，中位 **4.7ms** |
| `guard --form auto` 新帧率 | 236 次轮询 / **137 新帧** = 34.1 fps，`form=wgc` |
| svc 被 `taskkill /F` 后 worker 有没有变孤儿 | **没有**（它靠 `for line in sys.stdin` 撞到 EOF 自己退，所以没另加 atexit） |
| 遮挡会不会让 DWM 停止合成、wgc 就此冻帧 | **不会**：被完全盖住时帧龄始终 <40ms，连测 3s 新帧率 28~35fps |

**顺带补的一条硬护栏（重要）**：有一次 `guard` 里 4 秒只数到 1 个新帧 —— 会话没报错、取帧 3.8ms、
图像也"正常"，但头部帧计数器不动，也就是 auto 在把一张冻帧反复端给循环。后面两轮都复现不出来，
所以这不是"查清了"，是"没查清"。处理办法是加结构性防线而不是等它再犯：

```
_WGC_AUTO_MAX_AGE_MS = 120.0   # svc.py:1412
auto 用热会话 wgc 的前提是"帧龄 <120ms"，超了直接退回 pw，并在 note 里写明为什么
```

配套改动：`guard` 现在返回 `form`（最后一次实际用的形态；auto 会在 wgc/pw 之间切换，不报出来就没法定罪），
`probe` 的 `recommended` 也可能是 `wgc` 了。

**另一条测试自身的方法论错误**（值得记，因为它差点被当成产品缺陷）：查"有没有孤儿 worker"时用
`Get-CimInstance Win32_Process | Where CommandLine -like '*wgc_worker*'`，**匹配到的是执行这条查询的
powershell 自己**（它的命令行里就带着 `wgc_worker` 这串字）。必须先按 `Name='python.exe' OR Name='pythonw.exe'`
收窄，再看命令行。上一节那条 pythonw 的坑在这里第二次咬人。

### 14.7 `wait-stable` 补了窗口判据（同一轮收尾，4/4 PASS）

原来它只有整屏 `--region` 的 hash 判稳。在"这台机器同时有别的代理在动"的桌面上，
这等于把别人的重绘算进"我等的东西还没稳定"，是之前那轮 `wait-stable` 判失败的直接嫌疑。

现在给 `--hwnd` 就只看那个窗口（默认 pw，可 `--form wgc`），实测：

| 场景 | 结果 |
|---|---|
| 静止窗口 | 1.63s 判稳 |
| 60fps 动画窗口 | 正确报 `timeout_waiting_stable`，并附一句"目标确实一直在变（这通常就是答案）" —— 不假装稳定 |
| **被一块布完全盖住**的静止窗口 | 1.63s 判稳（这就是 `--hwnd` 相对 `--region` 的全部意义） |
| 最小化/抓不到图 | `wait_stable_no_image` + 怎么补救，不再空转到超时 |

另外 `wait-stable` / `guard` 现在都把**实际用的捕获形态**报成 `form` 字段。
不定罪就没法排查：auto 会在 pw/wgc 之间来回切，只报耗时不报形态，等于指标没法复现。

---

## 15. v1.5.0 游戏级操控（《我的世界》开发测试实测驱动）

第 17 轮（2026-10-06）。目标：让三文鱼"能像人一样玩《我的世界》"。全部结论来自实机。

### 15.1 两个新原语

```bash
"$PY" "$MC" move-rel --dx 700 --dy -120 --steps 14   # 相对位移（转视角用）
"$PY" "$MC" mouse-hold --button left --hold 2.0      # 按住鼠标 N 秒（挖矿/蓄力）
```

`move-rel` 用 ctypes 直接发 `SendInput(MOUSEEVENTF_MOVE)`，**不带 ABSOLUTE 标志**——
`pyautogui.moveTo` 发的是 `ABSOLUTE|VIRTUALDESK`，游戏（DirectInput/Raw Input）只读相对位移，
所以绝对定位对视角完全无效。`sizeof(INPUT)` 必须是 40（x64），写错会静默不生效。

### 15.2 实测边界：键盘进得去，鼠标进不去

| 输入 | 通道 | 结果 |
|---|---|---|
| `key t` / `f3` / `j` / `esc` | SendInput 键盘 | **生效**（开聊天、切调试、翻页、关页面） |
| `key-hold w 8s` | SendInput 键盘 | **生效**，遥测坐标 `884,71,8 → 884,59,16`，还带真实重力下落 |
| 方向键 / PageUp / PageDown / Home / End | SendInput 键盘 | **生效**（前提：模组把这些键绑到转向上，见 15.3） |
| `move-rel` 相对位移 | SendInput 鼠标 | **视角不动**：整帧只差 0.5%，光标自己跑到屏幕角（游戏没抓指针） |
| 老 API `mouse_event` 相对位移 | — | 同上，也不吃 |

**结论：这个构建的相机只认物理鼠标，注入的相对鼠标一律无效。**
不要在这上面继续耗——转视角改走"模组绑空闲键"，实测右转 6 次整帧变化 24.3%。

### 15.3 让游戏可玩的标准做法（键位表）

在被测游戏里绑一组**游戏本身没用**的键做相机，比破解输入注入便宜得多：

```
← →            偏航 ±15°        PageUp / PageDown  偏航 ±45°
↑ ↓            俯仰 ∓10°        Home / End         抬头到顶 / 低头到脚
```

实现要点（网易基岩版 ModSDK）：`OnKeyPressInGame` 的 `args["key"]` 是**虚拟键码字符串**
（J 是 `"74"`，方向键是 37/38/39/40），先 `GetRot()` 再叠加增量 `SetRot((pitch, yaw))`，
俯仰夹在 ±89°，别直接写死角度。

### 15.4 修掉一个长期存在的 OCR 故障（中文账户必现）

`cv2.imwrite` 在**非 ASCII 路径**上直接返回 `False`，而中文账户的 `%TEMP%` 正是非 ASCII
（用户名是中文），于是 OCR 报 `img_write_fail`——看起来像"OCR 引擎坏了"。
现在 `_ascii_tmp_dir()` 会挑一个保证全 ASCII 且可写的目录（优先 `%ProgramData%\salmon_tmp`）
给 OCR 临时图，失败时 `hint` 里会把实际用的目录带出来。
**教训：任何要落盘的中间文件都不能信任默认 temp 目录。**

### 15.5 测量纪律：像素字体不要交给 OCR

游戏 HUD 的点阵字（F3 的 `XYZ:` 等）rapidocr 会读成 `12:` 这种废串。
需要精确数值时，让被测程序自己写文件（本项目里模组每 10 秒写一行 `pos x y z day=`），
截图只用来做"变了没"的粗判（整帧差异百分比）。

### 15.6 操控前的硬性检查：界面有没有挡住键位

本项目里角色"按 W 不动"的真实原因是**说明书界面开着**（进世界会自动弹书）。
所以每次进游戏第一件事：截图确认没有 GUI 挡着，再谈"键位失效"。
