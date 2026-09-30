"""
imgloc 匹配器抽象基类
=====================
所有算法层（模板匹配/形状匹配/ORB/SIFT/LightGlue）都必须继承 Matcher，
实现统一接口，从而让 MultiStrategyLocator 能以完全一致的方式调度它们。

设计模式：策略模式（Strategy Pattern）
    每个 Matcher 子类是一种"可互换的算法策略"，对外表现一致，内部实现各异。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import numpy as np

from imgloc.types import MatchResult
from imgloc.utils import ImageInput, load_image


class Matcher(ABC):
    """
    匹配器抽象基类。

    子类必须实现：
        - name: 类属性，算法的唯一标识（如 "template"、"orb"）
        - is_available(): 判断当前环境是否满足运行条件（依赖是否安装等）
        - _match(image_source, image_target, threshold, **kwargs): 核心匹配逻辑

    基类负责：
        - 统一的输入图像加载与标准化（load_image）
        - 统一的计时与异常兜底（match() 方法包装 _match()）
        - 统一的配置管理
    """

    #: 算法唯一标识，子类必须覆盖，供路由器和日志识别使用
    name: str = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            config: 该算法层的参数字典（通常是 imgloc.config.load_config() 结果中
                对应算法的子字典，例如 cfg["template"]）。为 None 时使用空字典，
                子类应在内部对各参数做 .get(key, 合理默认值) 兼容处理。
        """
        self.config: Dict[str, Any] = config or {}

    @abstractmethod
    def is_available(self) -> bool:
        """
        判断该匹配器当前环境是否可用。

        对于核心算法（模板/形状/ORB/SIFT），应始终返回 True（仅依赖 opencv）。
        对于可选依赖算法（LightGlue），需要实际检测 torch/lightglue 是否已安装。
        """
        raise NotImplementedError

    @abstractmethod
    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        """
        核心匹配逻辑，由子类实现。传入的图像已经过 load_image 标准化为 numpy 数组。

        Args:
            image_source: 大图（BGR numpy 数组），即待搜索的截图。
            image_target: 小图（BGR numpy 数组），即待定位的模板/图标。
            threshold: 置信度阈值，低于此值应返回 found=False 的结果。
            **kwargs: 算法特定的额外参数（覆盖 self.config 中的对应项）。

        Returns:
            MatchResult: 该算法产出的匹配结果（method_used 应设为 self.name）。
        """
        raise NotImplementedError

    def match(
        self,
        image_source: ImageInput,
        image_target: ImageInput,
        threshold: Optional[float] = None,
        **kwargs: Any,
    ) -> MatchResult:
        """
        对外统一入口：加载图像 -> 调用 _match -> 自动计时 -> 异常兜底。

        Args:
            image_source: 大图，支持路径/bytes/numpy数组。
            image_target: 小图，支持路径/bytes/numpy数组。
            threshold: 置信度阈值，None 时使用 self.config["threshold"]，
                       仍无则使用 0.8。
            **kwargs: 传递给具体算法的额外参数。

        Returns:
            MatchResult: 统一格式的匹配结果，即使内部异常也会返回
                         found=False 的结果（不会抛出到调用方），
                         异常信息记录在 extra["error"] 中。
        """
        start = time.perf_counter()
        effective_threshold = (
            threshold if threshold is not None else self.config.get("threshold", 0.8)
        )
        try:
            src = load_image(image_source)
            tgt = load_image(image_target)
            result = self._match(src, tgt, effective_threshold, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 匹配器内部异常不应中断上层路由
            elapsed = (time.perf_counter() - start) * 1000
            return MatchResult.not_found(
                method_used=self.name,
                elapsed_ms=elapsed,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
        result.elapsed_ms = (time.perf_counter() - start) * 1000
        if result.method_used is None:
            result.method_used = self.name
        return result

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} available={self.is_available()}>"
