# imgloc — App自动化测试图像定位核心库

`imgloc` 是一个纯图像处理的目标定位引擎：输入一张"大图"（App截图）和一张
"小图"（待定位的模板/图标），输出目标在大图中的位置（坐标/置信度/角度/缩放比例）。

**只做图像识别底层能力，不涉及设备连接、adb、脚本调度等上层逻辑**，可在服务端、
CI、本地任意环境运行，零设备/系统依赖。

## 核心特性

- 🎯 **多算法路由（责任链模式）**：按 `模板匹配 → 形状匹配 → ORB特征匹配 →
  SIFT特征匹配 → LightGlue深度学习` 顺序自动尝试，先用最快的算法，
  置信度不足才降级到下一层，兼顾速度与鲁棒性。
- 🧩 **策略模式**：每种算法都是可互换的 `Matcher` 实现，统一输入输出，
  可自由增删、调整顺序、替换实现。
- ⚡ **轻量化核心**：核心库只依赖 `opencv-python` + `numpy` + `pyyaml`，
  深度学习兜底层（LightGlue）作为可选依赖，未安装时自动跳过，不影响核心功能。
- 🛡️ **应对常见挑战**：分辨率/DPI差异（多尺度金字塔）、轻微形变/抗锯齿
  （形状匹配对光照/主题不敏感）、部分遮挡、旋转、深色模式切换。
- 🧪 **完整测试与基准**：60+ 单元测试覆盖各算法核心逻辑和路由降级策略；
  独立的 benchmark 脚本对比各算法在缩放/旋转/遮挡/光照/噪声场景下的表现。

## 快速开始

### 安装

```bash
# 核心功能（推荐，轻量化）
pip install -e .

# 或者使用 requirements.txt
pip install -r requirements.txt

# 需要深度学习兜底层(LightGlue)时，额外安装：
pip install -r requirements-optional.txt
# 或者
pip install -e ".[deep]"

# 开发/测试依赖
pip install -r requirements-dev.txt
```

### 基本用法

```python
from imgloc import Locator

# strategy="auto" 表示走完整责任链，自动从快到慢降级尝试
locator = Locator(strategy="auto")

result = locator.find("screenshot.png", "icon_template.png", threshold=0.8)

if result.found:
    print(f"找到目标: {result.center_point}")
    print(f"置信度: {result.confidence:.3f}")
    print(f"命中算法: {result.method_used}")
    print(f"旋转角度: {result.angle:.1f}°  缩放比例: {result.scale:.3f}")
else:
    print("未找到目标")
```

### 只使用单一算法（不走责任链降级）

```python
# 只用模板匹配（最快，适合无形变场景）
locator = Locator(strategy="template")
result = locator.find(screenshot, template_icon)

# 只用 ORB 特征点匹配（速度快，抗旋转/遮挡）
locator = Locator(strategy="orb")
result = locator.find(screenshot, template_icon)
```

### 支持的图像输入类型

`image_source`（大图）和 `image_target`（小图）均支持三种输入方式：

```python
locator.find("path/to/screenshot.png", "path/to/icon.png")   # 文件路径
locator.find(screenshot_bytes, icon_bytes)                    # 图像字节流(bytes)
locator.find(screenshot_np_array, icon_np_array)               # numpy数组(BGR, cv2默认格式)
```

### 自定义配置

```python
# 方式1：传入 dict，只需覆盖关心的字段，未覆盖字段使用内置默认值
locator = Locator(strategy="auto", config={
    "template": {"threshold": 0.9, "scale_range": [0.5, 2.0]},
    "router": {"order": ["template", "orb", "sift"]},  # 自定义责任链顺序，跳过shape/lightglue
})

# 方式2：传入 YAML 配置文件路径
locator = Locator(strategy="auto", config="my_config.yaml")
```

YAML 配置文件示例见 `config.example.yaml`。

## 项目架构

```
GrapeMobile/
├── src/imgloc/                     # 核心库源码
│   ├── __init__.py                 # 对外导出入口
│   ├── types.py                    # MatchResult 统一数据结构
│   ├── base.py                     # Matcher 抽象基类（策略模式）
│   ├── locator.py                  # MultiStrategyLocator 责任链路由器 + Locator 对外API
│   ├── config.py                   # 配置加载与深度合并
│   ├── utils.py                    # 图像加载/格式转换工具函数
│   ├── exceptions.py               # 自定义异常体系
│   └── matchers/                   # 各算法层实现
│       ├── template.py             # 模板匹配层
│       ├── shape.py                # 形状匹配层
│       ├── orb.py                  # ORB特征点匹配层
│       ├── sift.py                 # SIFT特征点匹配层
│       ├── lightglue_matcher.py    # LightGlue深度学习兜底层（可选依赖）
│       └── _feature_common.py      # ORB/SIFT 共享的特征匹配公共逻辑
├── tests/                          # 单元测试（pytest）
├── benchmark/                      # 性能与准确率基准测试
│   ├── scenarios.py                # 合成测试场景生成器
│   └── run_benchmark.py            # 基准测试主脚本
├── examples/                       # 使用示例脚本
├── docs/                           # 补充文档（SIMD加速思路等）
├── pyproject.toml / requirements*.txt
└── config.example.yaml             # 配置文件示例
```

### 设计模式

- **策略模式（Strategy）**：`Matcher` 抽象基类定义统一接口
  `(image_source, image_target) -> MatchResult`，每个算法层
  （`TemplateMatcher`/`ShapeMatcher`/`OrbMatcher`/`SiftMatcher`/`LightGlueMatcher`）
  都是一种可互换的策略实现。
- **责任链模式（Chain of Responsibility）**：`MultiStrategyLocator` 按配置的
  `router.order` 顺序依次调用各 Matcher，前一层置信度不足或不可用时自动
  传递给下一层，直到命中或全部尝试完毕。

## 各算法层说明

| 层级 | 算法 | 速度 | 鲁棒性 | 适用场景 |
|---|---|---|---|---|
| 1 | 模板匹配 (`template`) | 最快 | 中 | 无形变、仅缩放差异 |
| 2 | 形状匹配 (`shape`) | 中 | 抗光照/主题变化 | 线条类图标、深色模式切换 |
| 3 | ORB特征匹配 (`orb`) | 快 | 抗旋转/部分遮挡 | 通用场景首选，无专利问题 |
| 4 | SIFT特征匹配 (`sift`) | 中慢 | 精度更高 | ORB失败后的高精度备选 |
| 5 | LightGlue (`lightglue`) | 慢(需GPU更快) | 最强 | 大形变/跨主题/弱纹理复杂场景 |

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## 运行基准测试

```bash
python benchmark/run_benchmark.py --output benchmark/results/report.csv
```

会输出各算法在 **缩放(scale)/旋转(rotation)/遮挡(occlusion)/光照(lighting)/噪声(noise)**
五大类合成场景下的命中率、坐标准确率、平均耗时对比报表，并导出详细CSV。

## 性能加速方向（可扩展）

- **SIMD加速模板匹配**：可参考 [Fastest_Image_Pattern_Matching](https://github.com/DennisLiu1993/Fastest_Image_Pattern_Matching)
  的 C++/SIMD 实现思路，将 `TemplateMatcher` 的核心循环用 C++扩展重写，
  大幅提升多尺度/旋转不变匹配的吞吐量。详见 `docs/simd_acceleration.md`。
- **C++形状匹配**：可参考 [shape_based_matching](https://github.com/meiqua/shape_based_matching)
  的 Line2D 算法完整实现替换当前的简化版 Python/OpenCV 实现。

## License

MIT
