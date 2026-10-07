---
name: grapemobile-dev-standard
description: grapeMobile 项目开发规范与功能总览。在本项目内做任何代码修改、新增功能、重构或答疑时使用，统一目录结构、步骤 JSON 格式、线程与回调约定、录制回放链路、GUI 界面视觉规范及编码风格，防止多人协作跑偏。不适用于其他项目。
---

# grapeMobile 开发规范

在做任何改动前，先按本规范对齐位置与风格；改动完成后必须按「验证要求」自检。

## 1. 项目是什么

Android 手机自动化编排工具：在网格化截图上点选格子生成步骤脚本（JSON），由步骤引擎经 adb 在真机/模拟器上回放，支持画面锚点校验、AI 定位、批量执行与测试报告（录制 + 回放视频）。

技术栈：Python 3 + Tkinter（GUI）+ opencv/numpy（图像比对）+ adb 命令行（设备控制）。**无 web 前端，无数据库。**

## 2. 目录结构（改错位置 = 跑偏）

```
src/                     核心引擎层（与 GUI 完全解耦，可被 CLI 复用）
  steps.py               步骤校验/执行引擎 StepRunner —— 步骤语义的唯一权威
  adbtools/client.py     AdbClient —— 所有 adb 命令的唯一出口
  recording.py           Recorder —— 执行过程录制（recording.json + frames/）
  replay_render.py       录制数据离屏渲染为 replay.mp4
  aiclient.py            AI 定位客户端
  locate/                OCR/VLM 定位实现
  grid.py                网格标记与格子坐标换算
  cvio.py                中文路径兼容的图像读写（项目内禁止直接用 cv2.imread 写盘）
runner/                  应用层
  gui_steps.py           主 GUI（Tkinter 三 Tab：普通/AI/执行模式），入口
  capture.py             采集产物：device_id.txt、tmp/image/grid_meta.json
  scripts/<用例名>/      每条用例一个文件夹：<用例名>.json + images/ 锚点图
  runs/                  普通模式完整运行的录制输出
  result/plan-<时间戳>/  执行模式测试计划输出（plan.json + 每用例子目录）
```

职责红线：`src/` 不 import Tkinter；`runner/gui_steps.py` 不直接拼 adb 命令，一律走 `AdbClient`。

## 3. 核心架构约定（不可破坏）

- **引擎与 GUI 解耦**：`StepRunner.run(steps, on_event, wait_tick, recorder)` 不感知界面；GUI 通过回调拿进度。
- **事件回调签名固定三参数** `(idx, total, msg)`，idx 从 1 计。新增日志点必须走 `emit(idx, msg)`，不得自行改签名。
- **线程模型**：所有耗时操作（adb、AI、渲染）放后台 `threading.Thread(daemon=True)`；UI 更新必须经 `self._bridge.post(lambda: ...)` 切回主线程。
- **闭包陷阱（已踩坑两次）**：`except` 块里的 `exc` 在块结束即被删除，lambda 延迟执行会 NameError。必须先 `err_msg = str(exc)` 再用默认参数捕获：`self._bridge.post(lambda m=err_msg: ...)`。
- **锚点图相对路径**：相对"脚本 JSON 所在目录"（`StepRunner.script_dir`）解析，统一放 `images/` 子目录。
- **录制链路**：`Recorder.create(device_id, runs_dir, run_id)` → 执行中 before/after 截帧 → `finish()` 写 recording.json → `render_to_video()` 产 replay.mp4。录制失败只告警不阻断执行。
- **adb 调用**：subprocess 用 list 参数（不用 shell=True）、utf-8 + errors="replace" 解码；截图走 screencap→pull→rm 三段式（Windows 下 exec-out 会损坏二进制流）。

## 4. 步骤 JSON 规范（新增字段前先看这里）

权威校验在 `src/steps.py::validate_steps`，GUI 保存前同样走它。UI 步骤（tap/input）必须有 cell 或 x/y；非 UI 步骤无坐标无锚点。

| type | 专有字段 | 语义 |
|------|---------|------|
| tap | cell 或 x/y | 点击（AI 定位产出像素坐标时优先） |
| input | cell/x/y + text | 先点聚焦再输入（仅 ASCII） |
| app_stop | package | 强杀 App；package 留空 = 执行时自动识别前台 App |
| app_start | package | 启动 App |
| app_clear | package | 清除 App 数据（pm clear） |
| keyevent | key | 系统按键：home / back / recents |

公共字段：`delay_before`（执行前等待秒）、`wait_after`（执行后等待/画面跳转超时上限）、`before_image`/`after_image`（仅 UI 步骤）、`desc`（人类可读描述）。旧字段 `expect_image` 仅加载兼容，保存时不再写出。

**新增一种步骤类型的完整清单**（漏一处 = 执行/报告不一致）：
1. `steps.py`：validate_steps 分支 + run() 执行分支（非 UI 步骤走 `_execute_non_ui_step` 模式）；
2. `adbtools/client.py`：对应底层命令方法；
3. `recording.py::before_step`：事件携带新字段；
4. `replay_render.py`：render_frames 分发 + 专属渲染器；
5. `gui_steps.py`：添加入口、列表行文案（`_step_action_text`）、右侧详情、双击编辑弹窗、执行模式详情摘要。

## 5. 编码风格

- 注释、docstring、日志、弹窗文案全部**中文**；docstring 写清 Args/Raises/Returns（项目现有风格，别引入英文文档）。
- 模块级 logger：`logger = logging.getLogger("imgloc.<模块名>")`，不 print。
- 异常：`AdbError`（adb 层）/`StepError`（步骤语义层）自定义异常向上抛，由 GUI 层弹窗；引擎层不吞异常、不弹窗。
- GUI 布局用 grid；需要"切换显示"的容器保存为 `self.xxx_frame`，用 `grid_remove()/grid()` 显隐，不要重复创建。
- 模态编辑弹窗统一模式：Toplevel + transient + grab_set，确定时先 `focus_set()+update_idletasks()` 强制提交 Spinbox 值（已踩坑：不提交则改完又回退）。
- 中文路径图像读写一律 `cvio`，不用 `cv2.imread/imwrite`。

## 6. UI 设计规范（Tkinter 暗色克制风）

设计稿参考：`design/design_proposal_v3.html`（HTML 高保真原型，唯一主稿）。改界面前先对照原型，别凭想象调。

### 6.1 视觉定调（踩过的坑，别退回去）

- **暗色开发者控制台风**：对标 VS Code / GitHub Dark / DevTools。工具要长时间盯截图和日志，浅色/发光会累。
- **单一强调色**：葡萄紫 `THEME["accent"]`（#7c6cd9）是全局唯一强调色，只用于选中态、主 CTA、Tab 选中文字、品牌标识。
- **明令禁止**（这些是"AI 生成设计"的典型特征，评审已判为不合格）：霓虹/发光文字、渐变与渐变 logo、玻璃拟态半透明（backdrop-blur 类）、大范围柔光投影、emoji 当图标、第二个强调色（如青色）、满屏"AI 模式"类标签。
- **状态色用中性工程色**：在线绿 `#3fb950` / 错误红 `#f85149` / 离线用次要灰，**不加发光点**。
- **图标**：用 Unicode 几何符号（`▶ ✕ ⟳ ◎ ⏹ ＋ ← ◆`）或纯文字，不用彩色 emoji。
- **日志区**：暗底 + 冷静灰绿等宽字（`term_bg`/`term_fg`），不要绿字发光。

### 6.2 色板与字体的唯一入口

- 所有颜色集中在模块级 `THEME` dict，所有字体集中在 `FONT_*` 常量（`FONT_UI` / `FONT_UI_BOLD` / `FONT_CAPTION` / `FONT_LIST` / `FONT_MONO` / `FONT_MONO_CHAT`）。**代码里禁止裸写 `#hex` 和临时字体元组。**
- `StepApp._apply_dark_theme()` 在 `__init__` 早期、**任何控件创建之前**调用一次：`option_add` 覆盖 tk 原生控件（Text/Listbox/Canvas/Toplevel），`ttk.Style("clam")` 覆盖 ttk 控件。新增控件一律走 THEME/FONT，不留 Windows 默认皮肤色。
- 新增样式走命名 style（如 `Accent.TButton` 主 CTA、`Caption.TLabel` 区块小标题、`Brand.TLabel` 顶栏品牌），不要逐个控件写死参数。

### 6.3 排版层级与布局

- 层级：**区块小标题（次要色小字 Caption）> 内容 > 弱边框分隔**。分区用 `ttk.Separator`，不要靠大片留白硬撑。
- 主体结构固定：顶栏（品牌 + 设备状态灯，横跨三栏）→ 分隔线 → 左设备画布 / 中 Notebook 三 Tab / 右详情面板。
- 三个 Tab 固定为「普通模式 / AI 模式 / 执行模式」，**步骤列表只在普通模式 Tab 内**，不要另起中间栏。
- 容器显隐用 `grid_remove()` / `grid()`，不重复创建（见第 5 节）。

### 6.4 步骤列表：Treeview，不要退回 Listbox

- 步骤列表是卡片式 `ttk.Treeview` 五列：`no / title / meta / waits / marks`，配 `rowheight=30` + 斑马纹 tag（`odd`/`even`）；系统操作步骤加 `sysop` tag 灰显。
- 选中态操作**一律走三个 helper**：`_list_sel()` / `_list_select(i)` / `_list_see_end()`。不要直接调 Treeview 的 `selection_*`/`see()`（index↔iid 转换易错，历史上已踩）。
- 事件名是 `<<TreeviewSelect>>`（**不是** `<<ListboxSelect>>`）；双击取行用 `identify_row(event.y)`，没有 `nearest()`。
- 标记列语义固定：`▶` 有执行前标准图、`⏹` 有执行后标准图、`◎` AI 精确定位（带像素坐标）。
- 新增步骤类型时，列表行的 title/meta/marks 也要同步（见第 4 节第 5 条清单）。

### 6.5 无设备时怎么看界面（离屏预览器）

`runner/_ui_preview.py` 是开发用预览器：绕过设备闸门，用本地 `runner/tmp/image/grid_screenshot.png` 当画面 + 假步骤，渲染真实窗口并存 PNG。

```bash
D:/Python/Python311/python.exe runner/_ui_preview.py runner/tmp/ui.png normal
D:/Python/Python311/python.exe runner/_ui_preview.py runner/tmp/ui.png ai
```

**必须用系统 Python `D:/Python/Python311/python.exe`**：托管的 3.13.12 不带 tkinter，导入即 `ModuleNotFoundError`。

**已知局限（重要）**：`PIL.ImageGrab` 抓不到 Tk 子控件像素（canvas 内嵌帧、ttk 主题控件渲染为空白）。所以截图里"某块是空的"**不等于控件没渲染**，别据此改布局。判断依据要看 `winfo_width/height/rootx/rooty/ismapped()`：
- 需要逐控件验证时，写一次性脚本把几何信息写进 `runner/tmp/*.log` 再读，比截图可靠；
- 导出画布内容用 `canvas.postscript(colormode="color")`（Tk 自己渲染，不依赖屏幕抓取）。

后台跑预览脚本时：日志必须写**工作区内**的路径（Windows Python 的 `/tmp` 与 Git Bash 的 `/tmp` 不是同一目录，日志会丢），跑完 `taskkill //F //IM python.exe` 收尾。

### 6.6 Tk 布局陷阱（已踩，勿重犯）

- **容器撑不开 = 内容"消失"**：曾用 `grid` + 父容器 `sticky="n"` + 行权重，导致气泡面板被算成 1×1，Label 明明 `mapped=1` 却看不见。对策：需要"对话区吃掉多余高度、其余自然高度"的容器内部改用 **pack**（`fill="both", expand=True`），别硬用 grid 行权重。
- **`pack` 的 `before=`**：候选卡片要在对话区之后、输入行之前，用 `pack(before=self._ai_input_row)` 固定顺序，不能只 `pack()`。
- **固定 geometry 会裁切按钮**：设备选择框曾硬编码 `560x330`，暗色主题下 Treeview 行高变大后内容溢出，底部按钮被挤出窗口。窗口尺寸一律用 `winfo_reqwidth/reqheight()` **自适应**（见 `_fit_geometry`）。
- **PhotoImage 生命周期**：重新加载图片时若旧引用被 GC，Tcl 侧图片同步销毁，画布闪黑。必须先 `create_image` 再赋 `self.photo`，并把旧图存到 `self._photo_ref` 保活。
- **缩放不影响点击**：`self.scale` 同时用于显示缩放和点击坐标换算（`event.x / self.scale`），所以缩放后点击依旧准确，不要另设变量。
- 气泡/卡片这类纯色块在深色底上"看不见"是**对比度问题，不是布局问题**——给 1px `relief="solid"` 描边并提高底色对比度。

### 6.7 UI 改动纪律

- **只改视觉层，不动录制/回放/定位逻辑**；步骤语义、字段、校验一律不碰。
- **Tkinter 有天花板**：做不出圆角卡片、毛玻璃、图标字体、悬停动效。在 ttk 上死磕质感是浪费时间——若要更高完成度，路线是 Web 重写，不是继续调 ttk。
- GUI 必须连 adb 设备才能完整启动，沙箱内只能预览。改完不要声称"已验证观感"，让用户本地 `python runner/gui_steps.py` 截图回看，再按截图迭代。
- 视觉方向没拍板前不要大面积改；一次只收敛一个维度（配色 → 排版 → 细节），每轮给对比。

## 7. 验证要求（改完必做）

1. 语法：`python -m py_compile <改动的每个 .py>`；
2. 导入冒烟（改 src 或 gui 后）：`python -c "import sys; sys.path.insert(0,'src'); sys.path.insert(0,'runner'); import gui_steps"`；
3. 改了 validate_steps：跑正反用例（合法步骤通过、非法类型/key/package 被拒）；
4. 改了 UI 视觉层：grep 一遍改动文件，确认没有裸写 `#hex` 和临时字体元组（应全部走 THEME / FONT_*）；再跑 `runner/_ui_preview.py` 出图核对（注意 6.5 的截图局限，必要时查控件几何）；
5. 本机是 PowerShell：不支持 `&&` 连接、不支持 heredoc，多命令用 `;`；GUI/预览相关一律用 `D:/Python/Python311/python.exe`（托管 3.13 无 tkinter）。

## 8. 变更守则

- 不改步骤 JSON 已有字段语义；需要扩展时新增字段并保持旧脚本可加载。
- GUI 交互以"用户当前已习惯的形态"为准（如步骤列表行内展示全信息、双击弹窗编辑），改交互前先确认。
- 执行模式与普通模式共享 `self._running` 互斥；新增运行入口必须挂进这套互斥。
- 每完成一个独立功能点做一次 git 提交，message 用中文一句话说明"为什么"。
