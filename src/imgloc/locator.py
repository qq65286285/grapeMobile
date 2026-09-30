"""
多算法路由器 (MultiStrategyLocator) 与对外统一 API (Locator)
================================================================
设计模式：责任链模式（Chain of Responsibility）
    按 [模板匹配 -> 形状匹配 -> ORB特征匹配 -> SIFT特征匹配 -> LightGlue]
    的顺序依次尝试。每层若置信度达到该层配置的阈值，则立即返回结果（链路终止）；
    否则自动"降级"到下一层，直到找到满足阈值的结果或全部算法尝试完毕。

这样设计的收益：
    - 绝大多数简单场景（无形变、仅缩放）在最快的模板匹配层就能命中，
      平均耗时接近纯 cv2.matchTemplate 的水平
    - 只有复杂场景才会逐步"付出更多算力"去尝试更鲁棒但更慢的算法
    - 每层算法互相独立，可自由增删、调整顺序、替换实现，不影响其他层
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from imgloc.base import Matcher
from imgloc.config import load_config
from imgloc.exceptions import ConfigError, MatcherNotAvailableError, NoMatcherAvailableError
from imgloc.types import MatchResult
from imgloc.utils import ImageInput

logger = logging.getLogger("imgloc")

# 策略名 -> Matcher 子类的注册表。使用惰性导入避免在模块加载时就强依赖所有算法实现，
# 尤其是 lightglue_matcher 内部对 torch 的 import 都是方法级惰性导入，这里仍可放心提前导入类本身。
def _build_registry() -> Dict[str, Any]:
    from imgloc.matchers.template import TemplateMatcher
    from imgloc.matchers.shape import ShapeMatcher
    from imgloc.matchers.orb import OrbMatcher
    from imgloc.matchers.sift import SiftMatcher
    from imgloc.matchers.lightglue_matcher import LightGlueMatcher

    return {
        "template": TemplateMatcher,
        "shape": ShapeMatcher,
        "orb": OrbMatcher,
        "sift": SiftMatcher,
        "lightglue": LightGlueMatcher,
    }


class MultiStrategyLocator:
    """
    多算法路由器：责任链模式调度各 Matcher，自动降级直到命中或全部尝试完毕。

    Attributes:
        config: 完整的合并后配置字典（见 imgloc.config.load_config）。
        matchers: 按 router.order 顺序实例化的 Matcher 对象列表。
    """

    def __init__(self, config: Optional[Union[str, Dict[str, Any]]] = None):
        """
        Args:
            config: None / dict / YAML路径，参见 imgloc.config.load_config。
        """
        self.config: Dict[str, Any] = load_config(config)
        self._registry = _build_registry()
        self.matchers: List[Matcher] = self._build_chain()

    def _build_chain(self) -> List[Matcher]:
        order: List[str] = self.config["router"]["order"]
        chain: List[Matcher] = []
        for name in order:
            matcher_cls = self._registry.get(name)
            if matcher_cls is None:
                raise ConfigError(
                    f"未知的算法策略名: {name!r}，可选值: {list(self._registry.keys())}"
                )
            layer_cfg = self.config.get(name, {})
            chain.append(matcher_cls(config=layer_cfg))
        return chain

    def find(
        self,
        image_source: ImageInput,
        image_target: ImageInput,
        threshold: Optional[float] = None,
        strategy: Optional[str] = None,
        **kwargs: Any,
    ) -> MatchResult:
        """
        执行多算法路由匹配。

        Args:
            image_source: 大图（App截图），支持路径/bytes/numpy数组。
            image_target: 小图（待定位模板/图标），支持路径/bytes/numpy数组。
            threshold: 全局阈值覆盖；为 None 时各层使用自己配置中的阈值。
                       注意：若指定了该参数，会覆盖责任链中每一层的阈值判定。
            strategy: 若指定单一策略名（如 "template"/"orb"等），则只运行该层，
                      不走责任链降级逻辑；为 None 或 "auto" 时走完整责任链。
            **kwargs: 传递给命中算法层的额外参数（覆盖该层配置）。

        Returns:
            MatchResult: 责任链中第一个满足阈值的结果；若全部未命中，
                         返回最后一层的 not_found 结果（method_used 为最后尝试的算法名）。
        """
        if strategy and strategy != "auto":
            matcher = self._get_matcher_by_name(strategy)
            return matcher.match(image_source, image_target, threshold=threshold, **kwargs)

        last_result: Optional[MatchResult] = None
        skip_on_error = self.config["router"].get("skip_on_error", True)

        for matcher in self.matchers:
            if not matcher.is_available():
                logger.debug("[imgloc] 跳过不可用的算法层: %s (依赖未安装或环境不支持)", matcher.name)
                continue

            try:
                # kwargs 会传递给每一层，各算法层内部通过 cfg.get(key, 默认值) 的方式
                # 读取自己关心的字段，未知字段会被安全忽略，因此无需按层名过滤 kwargs
                result = matcher.match(image_source, image_target, threshold=threshold, **kwargs)
            except MatcherNotAvailableError as exc:
                logger.debug("[imgloc] 算法层 %s 不可用: %s", matcher.name, exc)
                if skip_on_error:
                    continue
                raise
            except Exception as exc:  # noqa: BLE001 - 单层异常不应中断整条责任链
                logger.warning("[imgloc] 算法层 %s 执行异常，自动跳过: %s", matcher.name, exc)
                if skip_on_error:
                    continue
                raise

            last_result = result
            logger.debug(
                "[imgloc] 算法层 %s 结果: found=%s confidence=%.3f elapsed=%.1fms",
                matcher.name, result.found, result.confidence, result.elapsed_ms,
            )
            if result.found:
                return result

        if last_result is None:
            raise NoMatcherAvailableError(
                "责任链中所有算法层均不可用（依赖缺失或全部异常），无法执行匹配。"
                "请检查是否至少安装了核心依赖 opencv-python。"
            )
        return last_result

    def _get_matcher_by_name(self, name: str) -> Matcher:
        for matcher in self.matchers:
            if matcher.name == name:
                return matcher
        raise ConfigError(
            f"未在责任链中找到策略: {name!r}，当前链路: {[m.name for m in self.matchers]}"
        )

    def __repr__(self) -> str:
        chain_desc = " -> ".join(f"{m.name}({'✓' if m.is_available() else '✗'})" for m in self.matchers)
        return f"<MultiStrategyLocator chain={chain_desc}>"


class Locator:
    """
    对外统一入口，封装 MultiStrategyLocator，提供简洁的调用方式。

    示例：
        >>> from imgloc import Locator
        >>> locator = Locator(strategy="auto")
        >>> result = locator.find(screenshot, template_icon, threshold=0.8)
        >>> if result.found:
        ...     print(result.center_point, result.confidence, result.method_used)
    """

    def __init__(
        self,
        strategy: str = "auto",
        config: Optional[Union[str, Dict[str, Any]]] = None,
    ):
        """
        Args:
            strategy: "auto" 表示走完整责任链自动降级；
                      也可指定单一算法名："template"/"shape"/"orb"/"sift"/"lightglue"，
                      此时只运行该层算法，不做自动降级。
            config: None / dict / YAML文件路径，用于覆盖默认参数配置。
        """
        self.strategy = strategy
        self._router = MultiStrategyLocator(config=config)

    def find(
        self,
        image_source: ImageInput,
        image_target: ImageInput,
        threshold: Optional[float] = None,
        **kwargs: Any,
    ) -> MatchResult:
        """
        在大图中定位小图。

        Args:
            image_source: 大图（App截图），支持路径/bytes/numpy数组。
            image_target: 小图（待定位模板/图标），支持路径/bytes/numpy数组。
            threshold: 置信度阈值；为 None 时使用各算法层的默认配置阈值。
            **kwargs: 传递给算法层的额外参数。

        Returns:
            MatchResult: 统一格式的匹配结果。
        """
        return self._router.find(
            image_source, image_target, threshold=threshold, strategy=self.strategy, **kwargs
        )

    def __repr__(self) -> str:
        return f"<Locator strategy={self.strategy!r} router={self._router!r}>"
