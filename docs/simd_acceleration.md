# SIMD / C++ 加速方向说明

本文档说明如何将 `imgloc` 的核心算法层进一步加速为 C++/SIMD 实现，
适合在高频轮询（如每秒数十次截图轮询）或大批量离线测试场景下使用。

## 为什么需要 SIMD 加速

当前 `TemplateMatcher` 的多尺度金字塔匹配依赖 `cv2.matchTemplate` 在
每个缩放尺度上分别执行一次匹配。OpenCV 本身的 `matchTemplate` 已经是
C++实现且做了一定程度的向量化，但当：

- 需要遍历较大的缩放范围（如 0.5x~2.0x，多个尺度）
- 同时开启彩色三通道加权（3次卷积）
- 同时开启旋转不变匹配（多个角度 × 多个尺度）

耗时会显著增加（在本项目 benchmark 中，模板匹配层的平均耗时可达
800ms~1600ms，是链路中最慢的一层）。若需要将这一层加速到毫秒级，
可参考以下方案。

## 方案一：接入 Fastest_Image_Pattern_Matching

参考项目：https://github.com/DennisLiu1993/Fastest_Image_Pattern_Matching

该项目的核心思路：

1. **金字塔粗筛 + 精配准两阶段**：先在下采样后的低分辨率图像上做快速匹配，
   定位大致候选区域，再仅在候选区域附近的原分辨率图像上做精确匹配，
   避免在整张大图上做全分辨率遍历。
2. **旋转不变性**：通过预先生成模板在不同角度下的多个版本（离线预计算），
   运行时只需要对每个角度分别做一次(粗筛+精配)组合，仍比暴力遍历快。
3. **SIMD向量化**：使用 SSE/AVX 指令集对相关系数计算的内层循环做向量化，
   相比朴素 C++ 实现有数倍加速。

### 集成建议

将该项目编译为动态库（.dll/.so），通过 Python 的 `ctypes` 或 `pybind11`
封装为 Python 可调用接口，替换 `TemplateMatcher._match_template_once`
的内部实现，对外接口（`Matcher.match()` / `MatchResult`）保持不变，
上层 `MultiStrategyLocator` 无需任何改动即可享受加速效果。

示例封装思路（伪代码）：

```python
# imgloc/matchers/template_simd.py（可扩展新增）
import ctypes

class TemplateMatcherSIMD(TemplateMatcher):
    """基于 Fastest_Image_Pattern_Matching 的 SIMD 加速版模板匹配器。"""

    name = "template_simd"

    def __init__(self, config=None):
        super().__init__(config)
        self._lib = ctypes.CDLL("./fastest_pattern_matching.dll")
        # ... 声明函数签名 argtypes/restype ...

    def _match(self, image_source, image_target, threshold, **kwargs):
        # 调用 self._lib 中的 C++ 函数，将结果转换为统一的 MatchResult
        ...
```

将其注册到 `MultiStrategyLocator._build_registry()` 中，即可作为责任链
的一个可选层（例如替换默认的 `template` 层，或作为更快的前置粗筛层）。

## 方案二：接入 shape_based_matching (Line2D) 的 C++ 实现

参考项目：https://github.com/meiqua/shape_based_matching

当前 `ShapeMatcher` 是该思路的纯 Python/OpenCV 简化版实现（用
`matchTemplate(TM_CCORR)` 模拟滑窗方向余弦相似度计算）。原项目提供了
基于 Line2D 算法的完整 C++ 实现，特点：

- 使用量化梯度方向（8方向量化）+ 位运算加速相似度计算
- 支持在图像金字塔上做由粗到细的搜索，进一步减少计算量
- 编译为 Python 扩展模块后，速度比当前简化版可提升一个量级以上

### 集成建议

同样通过 `pybind11` 将其编译为 Python 扩展模块（原项目已包含
Python 绑定示例），封装为新的 `ShapeMatcherNative` 类，接口保持与
`Matcher` 基类一致，插入责任链后可直接替换当前的 `ShapeMatcher`。

## 何时值得做这层优化

- 若基准测试（`benchmark/run_benchmark.py`）显示模板匹配/形状匹配层
  耗时已成为整体响应延迟的主要瓶颈，且业务场景对响应时间有严格要求
  （例如需要支持每秒10次以上的高频截图轮询）
- 若目标平台有明确的 CPU 指令集支持（AVX2等），且可以接受引入 C++
  编译工具链和跨平台编译的维护成本

对于大多数 App 自动化测试场景（每次操作间隔数百毫秒到几秒），
当前纯 Python/OpenCV 实现 + 责任链自动降级机制（简单场景走最快的
ORB层，仅几毫秒）已经足够，只有在压测/高频轮询等特殊场景下才需要
引入本文档描述的 C++/SIMD 加速方案。
