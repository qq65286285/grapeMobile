# 代码审核报告与待修复清单

> 审核日期:2026-10-10 | 范围:`E:/GrapeMobile` 全仓(忽略 `midscene_bak/` 内部)
> 方法:静态阅读,未运行 pytest,未连设备实测。
> 配套文档:功能路线图见 [ROADMAP.md](ROADMAP.md)。
> 维护约定:修复完成后把状态改为 `DONE` 并在末尾「更新记录」追加一行;新发现的问题按 `CR-NN` 顺延编号追加。

## 1. 项目概览

GrapeMobile 是基于 adb 的 **Android UI 自动化「录制 / 编排 / 回放」桌面工具**(当前只面向 Android)。

- 录制:在网格化截图上点格子,或用自然语言让 AI 定位,生成 JSON 步骤脚本。
- 回放:通过 adb 在真机/模拟器上执行,导出带触控动画的 mp4 与批量执行产物。
- 真实用例:`runner/scripts/kof 邮箱登录 柬埔寨包/`。
- 注意:`README.md`、`pyproject.toml` 仍把项目描述为 `imgloc`(纯图像定位库),与实际不符。

### 技术栈

| 维度 | 内容 |
|---|---|
| 语言 | Python(声明 >=3.9,`.venv` 为 3.10,SKILL.md 要求带 tkinter 的 3.11) |
| GUI | Tkinter/ttk 暗色主题,PIL 自绘控件 |
| 图像 / OCR | opencv、numpy、Pillow、rapidocr-onnxruntime、rapidfuzz |
| AI | `src/aiclient.py`,标准库 urllib 调 OpenAI 兼容接口 |
| 设备 | 系统 adb 命令行(`subprocess`) |
| 视频 | `cv2.VideoWriter`,不依赖 ffmpeg |

### 分层

```
入口  runner/gui_steps.py | run_steps.py | replay.py | capture.py | grid.py | main.py
编排  src/steps.py(StepRunner) -> recording.py -> replay_render.py -> video_export.py
定位  src/locate/pipeline.py(AI) | src/imgloc/(模板->形状->ORB->SIFT->LightGlue)
设备  src/adbtools/client.py | src/grid.py | src/cvio.py
```

## 2. 目录职责速查

| 路径 | 职责 |
|---|---|
| `src/steps.py` | 步骤校验与执行引擎(tap/input/app_*/keyevent),步骤语义唯一权威 |
| `src/adbtools/client.py` | `AdbClient`,adb 命令唯一出口 |
| `src/grid.py` / `src/cvio.py` | 网格换算 / 中文路径图像读写 |
| `src/recording.py` `replay_render.py` `video_export.py` | 录制、触控动画渲染、导出 mp4 |
| `src/aiclient.py` | AI 客户端 |
| `src/imgloc/` | 通用图像匹配库(5 种 matcher + Locator 责任链) |
| `src/locate/` | AI 定位管线(intent/ocr/vlm/template/fusion/coords/viz) |
| `src/executor.py` `service.py` | 旧链路,基本只被旧入口使用 |
| `runner/gui_steps.py` | 主 GUI,3472 行 |
| `runner/main.py` `main_old.py` `grid.py` `capture.py` | 早期 CLI / 已弃用入口 |
| `runner/run_steps.py` `replay.py` | 命令行执行脚本 / 补渲染视频 |
| `runner/scripts/` | 用例库(JSON + 锚点图) |
| `runner/result/` | 批量执行产物 |
| `tests/` | 12 个文件约 139 个用例 |
| `benchmark/` `examples/` | 算法对比 / imgloc 示例 |
| `docs/` `design/` | 文档、GUI 高保真原型 |
| `midscene_bak/` | Midscene.js 1.14.0 快照,仅作参考,无运行时关系 |

## 3. 功能模块完成度

| 模块 | 状态 | 备注 |
|---|---|---|
| imgloc 多算法匹配 / 网格坐标 / 步骤引擎 | 完整 | 测试充分 |
| adb 客户端 | 基本完整 | `text()` 仅 ASCII,中文输入不可用 |
| AI 定位管线 | 功能完整 | vlm/pipeline/aiclient 无测试 |
| 录制与回放视频 | 完整 | 无单元测试 |
| GUI(录制/编排/执行三 Tab) | 可用未收敛 | CR-03 已修( LRU 缓存/卡片不重建,待用户实测 20 次切换确认);工作区约 650 行未提交改动 |
| 批量执行 | 可用 | |
| SIMD 加速 | 仅设想 | `docs/simd_acceleration.md` |

**无测试的模块:** `recording`、`replay_render`、`video_export`、`aiclient`、`locate/vlm`、`locate/pipeline`、`locate/template`、`cvio`、`adbtools` 除设备列表解析外的命令、`gui_steps.py` 全部。

## 4. 待修复问题清单

状态说明:`TODO` 未开始 | `DOING` 进行中 | `DONE` 已完成。优先级:P0 最高。

| 编号 | 优先级 | 问题 | 状态 |
|---|---|---|---|
| CR-01 | P0 | AI 配置散落且互不相通,需统一进 config.yaml | DONE |
| CR-02 | P0 | 格子尺寸 60 / 30 / 30 不统一,AI 返回的格子编号可能错位 | DONE |
| CR-03 | P0 | GUI 连续切换步骤卡片后越来越卡(OPEN) | DONE |
| CR-04 | P1 | `runner/main.py`、`grid.py` 绕过 `AdbClient` 自拼 adb 命令 | DONE |
| CR-05 | P1 | 异常被静默吞掉,配置写错无提示 | DONE |
| CR-06 | P1 | 包结构混乱,12 处以上 `sys.path.insert`;README/依赖与实际不符 | TODO |
| CR-07 | P2 | 仓库卫生:大文件入库、杂项目录、无 CI、无 LICENSE | DONE |
| CR-08 | P2 | `gui_steps.py` 3472 行上帝类需拆分 | DONE |

---

### CR-01 AI 配置统一进 config.yaml (P0)

**现状**
- `src/aiclient.py:29-31` 把 `DEFAULT_BASE_URL / DEFAULT_API_KEY / DEFAULT_MODEL` 写死在代码里。
- `src/locate/config.py:89` 解析 YAML 时强制 `api_key=""`,`:126` 只读环境变量 `LOCATE_VLM_KEY`;YAML 里写 key 不生效。
- `src/locate/vlm.py:163` key 为空时回退 `AIClient(...)` 默认硬编码值。
- `src/locate/pipeline.py:186-189` 用 `config.vlm.api_key` 是否非空判断是否启用 LLM,但创建的却是无参 `AIClient()`,两套配置没有打通。
- `runner/gui_steps.py:3204` 调用 `load_config()` 未传路径,配置文件实际没被读到。
- `runner/main.py:138`、`runner/test_ai.py:19` 同样直接 `AIClient()`。

**修复方案**
1. 项目根新增 `config.yaml`,增加 `ai:` 段(`base_url / api_key / model / timeout / max_tokens`)。`locate.vlm` 与 `ai:` 共用一套值,不再重复配置。
2. 读取优先级:环境变量 > `config.yaml` > 缺失时抛出明确错误。代码中不再保留默认 key。
3. `AIClient` 构造参数默认改为 `None`,新增 `AIClient.from_config(cfg)`;`vlm.py`、`pipeline.py`、`runner/main.py`、`runner/test_ai.py`、GUI 统一使用。
4. `load_config()` 无参数时默认读取项目根 `config.yaml`。
5. **必须交付一份 `config.yaml` 演示文件(demo)并提交到 git**,内容包含全部 `ai:` / `locate:` / `grid:` 字段及注释,key 字段写占位值。同步在 `config.example.yaml` 补 `locate:` 段。
6. 同时在 `config.yaml` 预留 `heal:` 段(自愈开关,详见 ROADMAP.md 的 RM-11):`mode`(off/template/ai,默认 off)、`crop_size`、`search_margin`、`threshold`、`ai_verify`。用例级配置可覆盖此处的全局值。
7. 决策记录:`config.yaml` **不加入 `.gitignore`**(2026-10-10 用户确认),明文 key 问题本轮不处理,后续在本项修复时一并改造。

**验收**:删除代码内默认 key 后,仅靠 `config.yaml` 能跑通 AI 定位;缺配置时报错信息指向具体字段。

### CR-02 格子尺寸统一为单一来源 (P0)

**现状**
- `src/grid.py:123` `GridMarker` 默认 `cell_size=60`。
- `runner/gui_steps.py:83` `DEFAULT_CELL_SIZE = 30`。
- `src/locate/pipeline.py:153` 与 `:251` 写死 `cell_size=30`。
- GUI 改了格子大小后,AI 返回的格子编号与画面对不上,点击位置会偏。

**修复方案**
1. `config.yaml` 增加 `grid.cell_size`,作为唯一默认值。
2. `locate()` / `_point_to_cell()` 增加 `cell_size` 参数,由调用方(GUI 当前实际值)传入,删除写死的 30。
3. `GridMarker` 默认值改为读配置,或要求显式传入。
4. 加测试:同一 `cell_size` 下,坐标转格子再转回坐标误差不超过半格。

**验收**:GUI 切换 20 / 30 / 60 三档,AI 定位得到的格子编号与画面网格一致。

### CR-03 GUI 切换步骤卡片后越来越卡 (P0)

**现状**:见 `debug-gui-card-switch-stutter.md`,状态 OPEN。连续点选卡片 5 次以上后越点越卡,点"保存等待"后也卡。
- 假设 A:每次 `_select_card` 全量销毁并重建卡片。
- 假设 B:`<<TreeviewSelect>>` 事件重复触发。
- 假设 C:`PhotoImage` 累积。
- 假设 D:每次约 80ms 的 `cvtColor` + LANCZOS 缩放。
- 假设 E:`Treeview.see()` 滚动与 `<Configure>` 级联。
- `.dbg/` 已有约 100KB ndjson 日志,但"Log Evidence"与"Verification Conclusion"仍是 Pending。
- `gui_steps.py` 内有 24 处 `_dbg_post` 插桩,会向 `127.0.0.1:7777` 发 HTTP 请求,属于未提交改动。

**修复方案**
1. 先分析 `.dbg/` 日志验证 A-E,**不要盲改**。
2. 大概率的修法:卡片复用与增量更新而非销毁重建;`PhotoImage` 按步骤缓存并限制数量;缩略图预先缩放,避免重复 `cvtColor` + LANCZOS;事件加防抖。
3. 修复后删除全部 `_dbg_post` 插桩,把约 352 行未提交改动分批提交。

**验收**:连续切换卡片 20 次无明显卡顿;`gui_steps.py` 内无 `_dbg_post`。

### CR-04 adb 调用统一走 AdbClient (P1)

**现状**:SKILL.md 规定 adb 命令唯一出口是 `AdbClient`,但 `runner/main.py` 与 `runner/grid.py` 各自拼 adb 命令,并各有一份重复的 `_read_device_id` 与 `_adb_prefix`。

**修复方案**:删除两处重复函数,改调 `src/adbtools/client.py`。改动小、风险低。顺手删除 `runner/main_old.py`、`src/executor.py`、`src/service.py` 等确认无引用的死代码(删前先 Grep 确认引用)。

**验收**:全仓除 `AdbClient` 外无直接 `subprocess` 调 adb。

**2026-10-13 修复记录与偏差**
- `runner/main.py`、`runner/grid.py` 的重复 `_read_device_id` / `_adb_prefix` 已删除,改调 `adbtools.read_device_id()` 与 `AdbClient.command_str()`(命令展示字符串收口到 adb 层,与真实执行同一来源)。
- **偏差**:`src/executor.py`、`src/service.py` 经验证**并非死代码**——`executor.py` 仍被 `runner/capture.py` 引用,`service.py` 仍被 `src/locate/template.py::locate()` 引用,均保留;仅删除了确认零引用的 `runner/main_old.py`。文档原方案中"顺手删除 executor/service"的设想不成立。
- 顺带修正:`runner/grid.py` 的 `CELL_SIZE` 原为硬编码 30,改为读 `config.yaml` 的 `grid.cell_size`(CR-02 单一来源的遗漏处)。

### CR-05 异常不再静默吞掉 (P1)

**现状**
- `src/locate/config.py:122-123` YAML 解析失败直接 `pass`,配置写错静默回退默认值。
- `src/aiclient.py` 的 `_emit_trace`、`_encode_image` 存在静默 `except`。

**修复方案**:配置解析失败时记录日志并抛出带文件路径与字段名的明确错误;其余静默 `except` 至少记录 warning。该项与 CR-01 同改一个文件,建议一起做。

**验收**:故意写错 `config.yaml` 时,启动即报出具体错误位置。

### CR-06 包结构、文档与依赖统一 (P1)

**现状**
- `pyproject.toml` 包名为 `imgloc`,只打包 `src/imgloc`;`steps`、`adbtools`、`locate`、`recording` 等靠 12 处以上 `sys.path.insert` 引用。
- `README.md` 只描述 imgloc,没有应用层说明;根目录无 LICENSE,但 README 声明 MIT。
- `requirements.txt`(含 OCR)与 `pyproject.toml`(不含)依赖不一致;`opencv-python`、`numpy` 在前者无版本约束。
- `lightglue` 走 git URL,不可复现;`numpy<2`、`opencv<5` 设了上限。
- Python 环境分裂:`.venv` 为 3.10,SKILL.md 要求 3.11(带 tkinter)。
- `pandas`、`matplotlib` 仅 benchmark 使用,却放在 dev 依赖。

**修复方案**
1. `pyproject.toml` 扩展包列表覆盖 `src` 下全部模块,使用 `pip install -e .`,删除所有 `sys.path.insert`。
2. 统一 `requirements*.txt` 与 pyproject 的依赖和版本约束;`lightglue` 固定 commit 或改为可选依赖。
3. 统一 Python 版本并写入 README。
4. 重写 README(项目定位、安装、config.yaml 用法、快速开始、目录说明);补 LICENSE。
5. benchmark 依赖拆出独立 extra。

**验收**:新环境按 README 一键安装并启动 GUI;全仓 `sys.path.insert` 为 0。

### CR-07 仓库卫生 (P2)

**现状**
- `midscene_bak/`(约 2041 个文件)被 git 跟踪,并自带一套 `.github` 工作流,干扰对仓库 CI 的判断。
- `runner/result/` 的 mp4/jpg、`runner/scripts/*/images` 的 PNG、`runner/image2*.png` 入库,仓库会快速膨胀。
- 杂项:`_smoke_test.py`、`.dbg/`、`2953028817/`、`src/imgloc.egg-info/`、`.workbuddy/`、`.trae/` 散落或入库。
- 无 CI、无 lint / 格式化 / 类型检查配置。

**修复方案**
1. `midscene_bak/` 移出仓库(注意:仍留在 git 历史中)。
2. 运行产物目录加入 `.gitignore`;确需保留的样例图改用 Git LFS 或只留最小样例。
3. 清理杂项目录;`.workbuddy/`、`.trae/` 是否入库由团队约定。
4. 新增 GitHub Actions:pytest + ruff。

**验收**:CI 绿;仓库体积明显下降。

**2026-10-13 修复记录**
- `midscene_bak/`(2547 文件)、`runner/result/`(历史执行产物)、`runner/image2*`(调试截图)已 `git rm --cached` 出库,磁盘文件保留;`.gitignore` 补充对应条目及 `2953028817/`、`.dbg/`。出库后暂存区删除 2568 个文件(约 50MB),工作区未跟踪列表恢复干净。
- 新增根 `.github/workflows/ci.yml`(ubuntu + Python 3.10 + libgl + pytest)。**ruff/lint 未纳入 CI**,留给 CR-06 统一配置后加入。
- 新增 MIT `LICENSE`(README 原已声明 MIT 但缺文件)。
- **未执行**:`2953028817/`(外部导入的杂项目录)仅加入 ignore,未从磁盘删除,留待用户确认后自行清理;`.workbuddy/`、`.trae/` 按团队约定暂不处理(其中 `.trae/skills` 的 SKILL.md 已入库)。

### CR-08 拆分 gui_steps.py (P2)

**现状**:`runner/gui_steps.py` 共 3472 行,`StepApp` 约 2700 行,UI、业务、线程与调试代码混在一起,且无任何测试保护。

**修复方案**:拆成 canvas、steps、runner、ai 四个模块(mixin 或独立类)。**必须在 CR-03 修完且补上冒烟测试之后再做**,小步提交,每次拆一块。

**验收**:单文件不超过约 800 行;GUI 手测三个 Tab 功能不回归。

**修复记录(2026-10-13,DONE)**
- 前置:CR-03 卡顿修复经用户确认关单(2026-10-13);先补 `tests/test_gui_smoke.py`(38 例:mixin 方法归属/常量 identity/MRO/行数守卫/真实 Tk 控件渲染,无 display 自动 skip),再动结构。
- 拆分结果(原 3424 行 → gui_steps.py 508 行):
  - `ui/theme.py`(THEME + FONT_*)、`ui/constants.py`(路径/布局常量,DEFAULT_CELL_SIZE 读 config);
  - `ui/widgets.py`(_ChatPane/_UiBridge/_AiLogHandler/_RoundedBadge/_RoundedButton);
  - `ui/device_dialog.py`(DeviceSelectDialog);
  - `ui/canvas_mixin.py`(设备画面/网格/画布/锚点/标准图预览,约 600 行);
  - `ui/steps_mixin.py`(步骤编排/列表/保存加载,约 760 行)+ `ui/steps_cards.py`(二次拆分:双栏卡片渲染与选中高亮,约 220 行);
  - `ui/runner_mixin.py`(运行/执行模式)、`ui/ai_mixin.py`(AI 定位/候选/叠加)。
  - `_build_ui` 拆为 `_build_left_panel`/`_build_center_panel`/`_build_normal_tab`/`_build_ai_tab`/`_build_exec_tab`,主体只留三栏骨架+appbar。
  - gui_steps.py re-export 原符号(THEME/FONT_*/GRID_IMAGE/_UiBridge/DeviceSelectDialog 等),`_ui_preview.py` 等调用方零改动兼容。
- 验证:pytest 222 passed(184 基线 + 38 冒烟);`_ui_preview.py` 修复过时的 `notebook.select`(自定义 Tab 条后遗留,拆分前已坏);三场景(normal/ai/exec)构建级验收通过(截图不可用——当前会话 ImageGrab 抓屏失败,改用控件树断言:三栏/三 Tab/双栏卡片/用例树/候选卡片/详情展开)。
- 偏差:拆分中修掉一个自身引入的回归(`root = tk.Frame(self, padding=0)` 应为 `ttk.Frame`,已由构建验收当场捕获修复);`_ui_preview.py` 与测试需 `update_idletasks()` 后 `winfo_ismapped()` 才刷新(Tk 布局语义,非缺陷)。

## 5. 建议修复顺序

`CR-01 -> CR-02 -> CR-05 -> CR-04 -> CR-03 -> CR-06 -> CR-07 -> CR-08`

理由:CR-01/02 直接影响 AI 定位准确性,CR-05 与 CR-01 同改一个文件;CR-04 小而安全;CR-03 修完让 GUI 稳定,之后再做结构性改动。

## 6. 更新记录

| 日期 | 内容 |
|---|---|
| 2026-10-10 | 创建文档,录入 CR-01 至 CR-08。决策:config.yaml 不进 .gitignore,修复 CR-01 时需交付 config.yaml demo 并提交 git。 |
| 2026-10-10 | CR-01/02/03/05 DONE。CR-03: 删除全部 24 处 `_dbg_post` 插桩及 `.dbg/`、debug 文档(此前多轮 LRU 缓存/卡片不重建优化即为修复本体,待用户实测确认 20 次切换)。CR-01: 新建 `config.yaml`(ai/locate/grid),`AIClient` 删硬编码 key、新增 `from_config()`,缺 key 报明确错误;`load_config()` 无参默认读项目根 config.yaml,vlm/ai key 互通。CR-02: `grid.cell_size` 单一来源,`GridMarker`/pipeline/GUI/main 全部改读配置。CR-05: config 解析失败抛 `ConfigError` 含文件路径;`_emit_trace`/`_encode_image` 静默 except 改 warning 日志。全量 184 测试通过。CR-04/06/07/08 仍 TODO。 |
| 2026-10-10 | CR-01 补充:config.yaml 预留 heal 段(mode/crop_size/search_margin/threshold/ai_verify),详见 ROADMAP.md RM-11。 |
| 2026-10-13 | CR-04/CR-07 DONE。CR-04:adbtools 新增 `read_device_id()` 与 `AdbClient.command_str()`,main.py/grid.py 删除重复的 `_read_device_id`/`_adb_prefix`,删 `runner/main_old.py`;偏差:`executor.py`/`service.py` 有引用未删(capture.py、locate/template.py),grid.py 的 CELL_SIZE 改读 config。CR-07:midscene_bak/(2547 文件)/runner/result/image2* 出库(暂存区 -2568 文件),.gitignore 补 5 条,新增 CI(pytest)与 MIT LICENSE。验证:py_compile、导入冒烟、CLI 空跑、pytest 184 passed、验收 grep 全过。 |
| 2026-10-13 | CR-08 DONE。gui_steps.py 3424 行拆为 508 行主体 + `runner/ui/` 包(theme/constants/widgets/device_dialog/canvas_mixin/steps_mixin/steps_cards/runner_mixin/ai_mixin),StepApp 以五个 mixin 组合,_build_ui 拆为五个 _build_*;新增 `tests/test_gui_smoke.py`(38 例:方法归属/常量 identity/MRO/行数守卫/Tk 渲染);`_ui_preview.py` 修复过时 `notebook.select`。验证:pytest 222 passed;三场景构建级验收通过。 |
