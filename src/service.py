"""
src/service.py - 图像定位服务层
================================
位于 imgloc 核心库之上的薄服务层:封装 imgloc.Locator,对上层屏蔽"多算法路由 /
责任链降级"等内部细节,只暴露"输入两张图 -> 输出一个坐标"的简洁接口,
供 runner/main.py 等上层入口调用。

设计说明:
    - service 层与 imgloc 核心库分离(放在 src/ 顶层,而非 src/imgloc/ 内),
      保持核心库纯粹,业务编排逻辑集中在 service 层
    - 内部使用 strategy="auto" 责任链:模板 -> 形状 -> ORB -> SIFT -> LightGlue
      自动从快到慢降级,兼顾速度与鲁棒性,无需上层关心算法选择
    - 返回 (x, y) 中心点像素坐标,可直接用于 adb 命令的点击定位
    - 未找到目标或置信度不足时返回 None,由上层决定如何处理
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple, Union

from imgloc import Locator

logger = logging.getLogger("imgloc.service")


class LocateService:
    """
    图像定位服务:在截图(大图)中查找模板(小图),返回中心点坐标。

    Args:
        strategy:  匹配策略。"auto" 走完整责任链自动降级(推荐默认);
                   也可指定单一算法名: template / shape / orb / sift / lightglue。
        config:    imgloc 配置,可选。None 用默认配置;dict 覆盖默认字段;
                   str 为 YAML 配置文件路径。
        threshold: 全局置信度阈值。None 时各算法层使用各自配置的默认阈值。
                   传入后会对责任链每一层的命中判定生效。

    示例:
        >>> service = LocateService(strategy="auto", threshold=0.8)
        >>> coord = service.locate("template.png", "screenshot.png")
        >>> if coord is not None:
        ...     print(f"adb shell input tap {coord[0]} {coord[1]}")
    """

    def __init__(
        self,
        strategy: str = "auto",
        config: Optional[Union[str, dict]] = None,
        threshold: Optional[float] = None,
    ):
        self.strategy = strategy
        self.default_threshold = threshold
        self._locator = Locator(strategy=strategy, config=config)

    def locate(
        self,
        template_path: str,
        source_path: str,
        threshold: Optional[float] = None,
    ) -> Optional[Tuple[int, int]]:
        """
        在 source_path(大图/截图)中定位 template_path(小图/模板),返回中心点坐标。

        Args:
            template_path: 小图(模板/图标)路径,即待定位的目标。
            source_path:   大图(App 截图)路径,即在其中搜索的图。
            threshold:     本次调用的置信度阈值;None 时使用实例默认阈值,
                           仍为 None 时用各算法层配置的默认阈值。

        Returns:
            (x, y): 目标在大图中的中心点像素坐标;未找到或置信度不足时返回 None。
        """
        effective_threshold = threshold if threshold is not None else self.default_threshold
        logger.info(
            "[LocateService] 开始定位: template=%s source=%s threshold=%s strategy=%s",
            template_path, source_path, effective_threshold, self.strategy,
        )

        result = self._locator.find(
            image_source=source_path,
            image_target=template_path,
            threshold=effective_threshold,
        )

        if not result.found:
            logger.warning(
                "[LocateService] 未找到目标: 最后尝试算法=%s 置信度=%.3f 耗时=%.1fms",
                result.method_used, result.confidence, result.elapsed_ms,
            )
            return None

        center = result.center_point
        logger.info(
            "[LocateService] 命中: 坐标=%s 置信度=%.3f 算法=%s 耗时=%.1fms",
            center, result.confidence, result.method_used, result.elapsed_ms,
        )
        return center

    def locate_for_adb(
        self,
        template_path: str,
        source_path: str,
        threshold: Optional[float] = None,
    ) -> Optional[str]:
        """
        locate() 的 adb 友好版本:直接返回 "adb shell input tap <x> <y>" 命令字符串。

        未找到时返回 None。便于上层直接打印或拼接到自动化脚本中。
        """
        coord = self.locate(template_path, source_path, threshold=threshold)
        if coord is None:
            return None
        return f"adb shell input tap {coord[0]} {coord[1]}"


__all__ = ["LocateService"]
