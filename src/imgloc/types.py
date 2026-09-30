"""
imgloc 统一数据结构定义
=======================
所有匹配器（Matcher）无论内部算法如何，最终都返回统一的 MatchResult，
方便上层调用方以一致的方式处理不同算法的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any


@dataclass
class MatchResult:
    """
    统一匹配结果数据类。

    Attributes:
        found: 是否找到满足阈值的匹配目标。
        rect: 目标在大图中的外接矩形 (x, y, w, h)，(x, y) 为左上角坐标。
              若目标存在旋转，该矩形为旋转后目标的轴对齐外接框（AABB）。
        center_point: 目标中心点坐标 (cx, cy)，即最终用于点击/操作的坐标。
        confidence: 匹配置信度，范围通常为 [0, 1]，不同算法含义略有差异：
              - 模板匹配：归一化相关系数 (TM_CCOEFF_NORMED 的结果)
              - 形状匹配：边缘方向梯度相似度
              - 特征点匹配：内点数 / 总匹配点数 的比例（RANSAC 后）
              - LightGlue：匹配置信度均值
        angle: 目标相对模板的旋转角度（度），无旋转估计能力的算法固定为 0.0。
        scale: 目标相对模板的缩放比例，1.0 表示等比例，无缩放估计能力的算法固定为 1.0。
        method_used: 实际命中的算法名称，例如 "template" / "shape" / "orb" /
              "sift" / "lightglue"。未命中时为最后一个尝试的算法名或 None。
        raw_corners: 可选，四个角点坐标（用于旋转目标的精确框，特征点匹配算法可提供），
              顺序为 [左上, 右上, 右下, 左下]，形状匹配/模板匹配通常为 None。
        extra: 可选，算法私有的额外调试信息（例如内点数、金字塔层级、耗时明细等），
              不同算法字段不同，仅供调试/日志使用，不建议上层业务强依赖其内部字段。
        elapsed_ms: 本次匹配耗时（毫秒），由 Matcher 基类自动计时填充。
    """

    found: bool
    rect: Optional[Tuple[int, int, int, int]] = None
    center_point: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    angle: float = 0.0
    scale: float = 1.0
    method_used: Optional[str] = None
    raw_corners: Optional[Tuple[
        Tuple[float, float], Tuple[float, float],
        Tuple[float, float], Tuple[float, float],
    ]] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @staticmethod
    def not_found(method_used: Optional[str] = None, elapsed_ms: float = 0.0,
                  extra: Optional[Dict[str, Any]] = None) -> "MatchResult":
        """构造一个"未找到"的标准结果，避免各算法各自拼裸字典。"""
        return MatchResult(
            found=False,
            method_used=method_used,
            elapsed_ms=elapsed_ms,
            extra=extra or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为普通 dict，便于日志打印/JSON序列化/写入报表。"""
        return {
            "found": self.found,
            "rect": self.rect,
            "center_point": self.center_point,
            "confidence": round(self.confidence, 4),
            "angle": round(self.angle, 2),
            "scale": round(self.scale, 4),
            "method_used": self.method_used,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "extra": self.extra,
        }

    def __repr__(self) -> str:  # 便于调试时直接 print(result)
        if self.found:
            return (
                f"MatchResult(found=True, center={self.center_point}, "
                f"confidence={self.confidence:.3f}, method={self.method_used!r}, "
                f"angle={self.angle:.1f}, scale={self.scale:.3f}, "
                f"elapsed={self.elapsed_ms:.1f}ms)"
            )
        return f"MatchResult(found=False, method={self.method_used!r}, elapsed={self.elapsed_ms:.1f}ms)"
