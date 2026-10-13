# GrapeMobile — Android UI 自动化「录制 / 编排 / 回放」工具

基于 adb 的 Android 手机自动化桌面工具:在网格化截图上点选格子(或用自然语言让 AI 定位)生成步骤脚本,由步骤引擎经 adb 在真机/模拟器上回放,支持画面锚点校验、批量执行与回放视频导出。当前只面向 Android,目标用途是**自动化测试**。

> **项目阶段**:录制 / 编排 / 回放链路已可用;"操作 → 断言 → 失败诊断 → 报告"的测试闭环正在建设中。
> 待修问题见 [docs/CODE_REVIEW.md](docs/CODE_REVIEW.md),功能路线见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 功能特性

- **三种录制方式**:网格点选(普通模式)、自然语言 AI 定位(AI 模式)、命令行采集
- **步骤引擎**:`tap` / `input` / `app_stop` / `app_start` / `app_clear` / `keyevent`,执行前后可用锚点标准图校验画面
- **AI 定位管线**:意图解析 → OCR + VLM + 模板匹配多源融合,结果经 verify 二次校验
- **批量执行**:执行模式按测试计划逐用例运行,各自独立结果目录
- **回放视频**:执行过程离屏渲染为带触控动画的 mp4(不依赖 ffmpeg)
- **内置 imgloc 图像库**:模板 → 形状 → ORB → SIFT → LightGlue 五层算法自动降级(见下文)

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10+(GUI 需带 tkinter;无设备离屏预览建议用 3.11) |
| adb | 系统 PATH 中可用(Windows 下未入 PATH 时在代码里显式指定 `adb_path`) |
| 设备 | Android 真机或模拟器,已开 USB 调试 |
| 操作系统 | 开发环境为 Windows;核心引擎(`src/`)不依赖 GUI,可跨平台 |

## 安装

```bash
# 核心依赖(opencv / numpy / pyyaml + OCR)
pip install -r requirements.txt
# 开发与测试
pip install -r requirements-dev.txt
# 可选:深度学习兜底层(LightGlue)
pip install -r requirements-optional.txt
```

当前以**源码直接运行**(`python runner/gui_steps.py`,入口脚本自行将 `src/` 加入 `sys.path`),无需 `pip install -e .`;打包结构与 `sys.path` 清理在 CR-06 中统一。

## 配置(config.yaml)

项目根 `config.yaml` 是唯一配置文件,`load_config()` 默认读取它。
**优先级:环境变量 > config.yaml > 缺失时抛出指向具体字段的错误。**

| 段 | 字段 | 说明 |
|---|---|---|
| `ai` | `base_url` / `api_key` / `model` / `timeout` / `max_tokens` | OpenAI 兼容 AI 客户端;定位管线的 VLM 与 AI 模式共用这一套值 |
| `locate` | `mode` / `vlm` / `ocr` / `verify` / `fusion` / `debug` | 定位管线参数。`mode` 取 `full` / `ocr_only` / `vlm_only`;`fusion.weights` 调各来源融合权重 |
| `grid` | `cell_size` | 网格格子边长(像素),GUI / AI 定位 / CLI 的**单一来源** |
| `heal` | `mode` / `crop_size` / `search_margin` / `threshold` / `ai_verify` | 分层自愈定位开关(预留,默认 `off`,设计见 ROADMAP.md RM-11) |

字段样例(含占位 key)见 `config.example.yaml`。

## 快速开始

```bash
# 1. 主 GUI(录制 / 编排 / 回放三个 Tab)
python runner/gui_steps.py

# 2. 命令行链路(无 GUI)
python runner/capture.py [device_id]     # 连接设备 + 截图 + 生成网格图,记录 device_id
python runner/main.py                    # AI 识别网格图上的可点击元素(格子编号 + 坐标 + 点击命令)
python runner/grid.py A3                 # 格子编号 → 像素坐标,并直接执行点击
python runner/grid.py 40                 # 只按 40px 格子重新生成网格图

# 3. 无头执行与补渲染(不依赖 GUI)
python runner/run_steps.py [--no-record] runner/scripts/<用例>/<用例>.json
python runner/replay.py <run-id | 运行目录 | recording.json>

# 4. 无设备时离屏预览 UI(开发用,需带 tkinter 的 Python)
python runner/_ui_preview.py runner/tmp/ui.png normal
```

执行前提:`runner/device_id.txt`、`runner/tmp/image/` 下的截图与网格元数据已由 `capture.py` 或 GUI 生成过一次。

## 用例脚本格式

用例是 JSON 数组(每用例一个目录:`<用例名>.json` + `images/` 锚点图),权威校验在 `src/steps.py::validate_steps`,GUI 保存前同样走它。

| type | 专有字段 | 语义 |
|---|---|---|
| `tap` | `cell` 或 `x/y` | 点击(AI 定位产出像素坐标时优先) |
| `input` | `cell`/`x/y` + `text` | 先点聚焦再输入(仅 ASCII,中文输入见 RM-06) |
| `app_stop` | `package` | 强杀 App;留空 = 执行时自动识别前台 App |
| `app_start` | `package` | 启动 App |
| `app_clear` | `package` | 清除 App 数据 |
| `keyevent` | `key` | 系统按键:`home` / `back` / `recents` |

公共字段:`delay_before`(执行前等待)、`wait_after`(执行后等待/画面跳转超时上限)、`before_image` / `after_image`(仅 UI 步骤,相对 JSON 所在目录解析)、`desc`。旧字段 `expect_image` 仅加载兼容。

## 目录结构

```
GrapeMobile/
├── src/                      # 核心引擎层(与 GUI 解耦,可被 CLI 复用)
│   ├── steps.py              #   步骤校验/执行引擎 StepRunner —— 步骤语义唯一权威
│   ├── adbtools/client.py    #   AdbClient —— adb 命令唯一出口
│   ├── recording.py          #   Recorder —— 执行过程录制(recording.json + frames/)
│   ├── replay_render.py      #   录制数据离屏渲染为 replay.mp4
│   ├── aiclient.py           #   AI 客户端(OpenAI 兼容接口,标准库 urllib)
│   ├── locate/               #   AI 定位管线(intent/ocr/vlm/template/fusion/coords/viz)
│   ├── imgloc/               #   内置图像匹配库(5 种 matcher + Locator 责任链)
│   ├── grid.py / cvio.py     #   网格换算 / 中文路径图像读写
│   └── executor.py service.py#   旧链路(仍被 capture.py / locate/template.py 引用)
├── runner/                   # 应用层
│   ├── gui_steps.py          #   主 GUI(Tkinter 三 Tab:普通/AI/执行模式),入口
│   ├── _ui_preview.py        #   无设备离屏 UI 预览器(开发用)
│   ├── capture.py / main.py / grid.py   # 命令行小工具
│   ├── run_steps.py / replay.py         # 无头执行 / 补渲染
│   ├── scripts/<用例名>/     #   用例库(JSON + images/ 锚点图)
│   ├── runs/ result/         #   运行期录制产物 / 批量执行产物(均不入库)
│   └── tmp/                  #   截图、网格图、调试图(不入库)
├── tests/                    # pytest,184 个用例
├── benchmark/ examples/      # 算法对比基准 / imgloc 示例
├── docs/                     # CODE_REVIEW(待修问题)、ROADMAP(功能路线)、simd 加速设想
├── design/                   # GUI 高保真原型(HTML)
├── .trae/skills/grapemobile-dev-standard/  # 开发规范(改动前必读)
└── config.yaml / config.example.yaml      # 配置文件 / 样例
```

## 内置图像定位库 imgloc

`src/imgloc/` 是可独立使用的多算法图像匹配库:输入大图(App 截图)与小图(模板/图标),输出位置、置信度、角度与缩放。按 `模板匹配 → 形状匹配 → ORB → SIFT → LightGlue` 顺序自动降级,先用最快的算法。

| 层级 | 算法 | 速度 | 鲁棒性 | 适用场景 |
|---|---|---|---|---|
| 1 | 模板匹配 `template` | 最快 | 中 | 无形变、仅缩放差异 |
| 2 | 形状匹配 `shape` | 中 | 抗光照/主题变化 | 线条类图标、深色模式切换 |
| 3 | ORB `orb` | 快 | 抗旋转/部分遮挡 | 通用场景首选,无专利问题 |
| 4 | SIFT `sift` | 中慢 | 精度更高 | ORB 失败后的高精度备选 |
| 5 | LightGlue `lightglue` | 慢(需 GPU 更快) | 最强 | 大形变/跨主题/弱纹理复杂场景 |

```python
from imgloc import Locator

locator = Locator(strategy="auto")   # 走完整责任链,自动降级
result = locator.find("screenshot.png", "icon_template.png", threshold=0.8)
if result.found:
    print(result.center_point, result.confidence, result.method_used)
```

大图与小图均支持文件路径、bytes、numpy 数组三种输入;`strategy` 也可指定单一算法(如 `"template"`)。算法在各场景下的量化对比见下节基准测试。

## 测试与基准

```bash
pytest tests/ -v        # 184 个用例(引擎 / 网格 / 各 matcher / 步骤 / adb 解析)
python benchmark/run_benchmark.py --output benchmark/results/report.csv
```

基准测试输出各算法在缩放 / 旋转 / 遮挡 / 光照 / 噪声五类合成场景下的命中率、坐标准确率与平均耗时对比。

## 开发规范

本项目有强制开发规范 **[.trae/skills/grapemobile-dev-standard/SKILL.md](.trae/skills/grapemobile-dev-standard/SKILL.md)**:目录职责、步骤 JSON 格式、线程与回调约定、GUI 暗色视觉规范、验证要求。**做任何改动前先读它**;核心红线包括:`src/` 不 import Tkinter、GUI 不直接拼 adb 命令、事件回调固定 `(idx, total, msg)` 签名、中文路径图像读写一律走 `cvio`。

## 路线图与待修问题

- 待修问题(CR-01 ~ CR-08,含当前状态):[docs/CODE_REVIEW.md](docs/CODE_REVIEW.md)
- 功能路线(RM-01 ~ RM-11,含断言步骤、滑动手势、失败诊断、分层自愈等):[docs/ROADMAP.md](docs/ROADMAP.md)

当前建议实施顺序:`CR-06 → CR-08`;功能上 `RM-01 断言 → RM-02 滑动手势 → RM-03 失败诊断 → RM-11 自愈 → RM-04 报告 → RM-05 无头 CLI → RM-06 中文输入`。

## License

[MIT](LICENSE)
