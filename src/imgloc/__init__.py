"""
imgloc - App自动化测试图像定位核心库
=====================================

纯图像处理的目标定位引擎，输入"大图"(App截图)和"小图"(待定位模板/图标)，
输出目标在大图中的位置信息(坐标/置信度/角度/缩放比例)。

设计目标：
- 零设备/系统依赖，可在服务端、CI、本地任意环境运行
- 多算法路由：按速度从快到慢自动降级，兼顾速度与鲁棒性
- 策略模式 + 责任链模式，算法可插拔、可替换、可扩展

快速开始：
    >>> from imgloc import Locator
    >>> locator = Locator(strategy="auto")
    >>> result = locator.find(screenshot, template_icon, threshold=0.8)
    >>> if result.found:
    ...     print(result.center_point, result.confidence, result.method_used)
"""

from imgloc.types import MatchResult
from imgloc.base import Matcher
from imgloc.locator import Locator, MultiStrategyLocator
from imgloc.exceptions import (
    ImgLocError,
    ImageLoadError,
    MatcherNotAvailableError,
    ConfigError,
)

__version__ = "0.1.0"

__all__ = [
    "MatchResult",
    "Matcher",
    "Locator",
    "MultiStrategyLocator",
    "ImgLocError",
    "ImageLoadError",
    "MatcherNotAvailableError",
    "ConfigError",
    "__version__",
]
