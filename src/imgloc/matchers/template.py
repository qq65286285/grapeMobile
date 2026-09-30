"""
模板匹配层 (Template Matching Layer)
=====================================
基于 cv2.matchTemplate 实现，是整个责任链中速度最快的一层，
适合处理"目标未发生形变、仅存在缩放/轻微位置差异"的常见场景。

核心能力：
    1. 基础模板匹配：cv2.matchTemplate(TM_CCOEFF_NORMED)
    2. 多尺度金字塔匹配：解决大图/小图分辨率(DPI)不同导致的缩放问题
    3. RGB 三通道加权匹配：比灰度匹配更能区分色彩相近但明度不同的图标
    4. 可选旋转不变匹配：小角度范围内遍历旋转角度，兼容轻微旋转场景

算法说明：
    默认采用"两级尺度搜索"(two_pass=True)兼顾速度与跨分辨率鲁棒性：
      第1级：仅以原始尺度 1.0 匹配一次——同分辨率截图(最常见场景)直接命中返回；
      第2级：未达阈值时，先在 [fallback_scale_range] 内以粗步长扫描定位最优
             尺度峰，再在该峰 ±1 个粗步长内以 refine_scale_step 精搜。
    粗搜阶段只选最优尺度、不做阈值裁决(尖锐的尺度峰可能落在两个粗网格点之间，
    两点分数都不达标，但精搜可命中),最终是否命中统一由 threshold 裁决。
    坐标链路保持单一真源：matchTemplate 始终在原始大图上进行、只缩放模板，
    因此返回的左上角坐标天然属于大图坐标系,矩形宽高使用"本次实际使用的缩放后
    模板尺寸",不做任何额外的坐标回映射。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from imgloc.base import Matcher
from imgloc.types import MatchResult
from imgloc.utils import ensure_bgr, to_gray

# cv2 匹配方法名 -> 常量的映射，方便配置文件里用字符串指定
_CV2_METHOD_MAP = {
    "TM_CCOEFF_NORMED": cv2.TM_CCOEFF_NORMED,
    "TM_CCORR_NORMED": cv2.TM_CCORR_NORMED,
    "TM_SQDIFF_NORMED": cv2.TM_SQDIFF_NORMED,
}

# 使用 SQDIFF 系列方法时，得分越低越好，需要做 1-score 转换以统一"越大越好"的语义
_LOWER_IS_BETTER_METHODS = {"TM_SQDIFF_NORMED", cv2.TM_SQDIFF_NORMED}


class TemplateMatcher(Matcher):
    """模板匹配层：cv2.matchTemplate + 多尺度金字塔 + RGB加权 + 可选旋转不变。"""

    name = "template"

    def is_available(self) -> bool:
        # 仅依赖 opencv-python（核心依赖），始终可用
        return True

    # ------------------------------------------------------------------
    # 核心匹配入口
    # ------------------------------------------------------------------
    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        cfg = {**self.config, **kwargs}
        method_name = cfg.get("method", "TM_CCOEFF_NORMED")
        cv2_method = _CV2_METHOD_MAP.get(method_name, cv2.TM_CCOEFF_NORMED)
        use_color = cfg.get("use_color", True)
        channel_weights = cfg.get("channel_weights", [0.34, 0.33, 0.33])
        two_pass = cfg.get("two_pass", True)
        scale_range = cfg.get("scale_range", [0.5, 1.5])
        scale_step = cfg.get("scale_step", 0.05)
        fallback_scale_range = cfg.get("fallback_scale_range", [0.5, 3.0])
        fallback_scale_step = cfg.get("fallback_scale_step", 0.1)
        refine_scale_step = cfg.get("refine_scale_step", 0.02)
        rotation_invariant = cfg.get("rotation_invariant", False)
        rotation_range = cfg.get("rotation_range", [-15, 15])
        rotation_step = cfg.get("rotation_step", 5)

        src_h, src_w = image_source.shape[:2]
        tgt_h, tgt_w = image_target.shape[:2]
        if tgt_h > src_h or tgt_w > src_w:
            # 模板比大图还大，直接判定为不可能匹配，避免 matchTemplate 抛异常
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "template_larger_than_source"},
            )

        angles: List[float] = [0.0]
        if rotation_invariant:
            lo, hi = rotation_range
            angles = list(np.arange(lo, hi + 1e-6, rotation_step))
            if 0.0 not in angles:
                angles.append(0.0)

        match_args = (cv2_method, use_color, channel_weights, method_name)
        best: Dict[str, Any] = {"score": -np.inf, "loc": None, "size": None, "scale": 1.0, "angle": 0.0}

        if two_pass:
            # ---------- 两级搜索 ----------
            # 第1级：每个旋转角下仅以原始尺度 1.0 快速尝试，命中立即返回
            for angle in angles:
                rotated_target = self._rotate_image(image_target, angle) if angle != 0.0 else image_target
                cand = self._scan_one_scale(image_source, rotated_target, 1.0, angle, *match_args)
                if cand is not None and cand["score"] > best["score"]:
                    best.update(cand)
                if best["loc"] is not None and best["score"] >= threshold:
                    return self._build_result(best, method_name, found=True)

            # 第2级：粗搜定位尺度峰（不做阈值裁决），再在峰邻域精搜
            coarse_scales = self._build_scales(
                fallback_scale_range[0], fallback_scale_range[1], fallback_scale_step
            )
            for angle in angles:
                rotated_target = self._rotate_image(image_target, angle) if angle != 0.0 else image_target
                coarse_best: Dict[str, Any] = {"score": -np.inf, "scale": 1.0}
                for scale in coarse_scales:
                    cand = self._scan_one_scale(image_source, rotated_target, scale, angle, *match_args)
                    if cand is not None and cand["score"] > coarse_best["score"]:
                        coarse_best.update(score=cand["score"], scale=scale)
                        best.update(cand)

                # 在最优粗尺度 ±1 个粗步长内精搜（含粗尺度本身），clamp 到配置范围内
                peak = float(coarse_best["scale"])
                lo = max(fallback_scale_range[0], peak - fallback_scale_step)
                hi = min(fallback_scale_range[1], peak + fallback_scale_step)
                refine_scales = self._build_scales(lo, hi, refine_scale_step)
                for scale in refine_scales:
                    cand = self._scan_one_scale(image_source, rotated_target, scale, angle, *match_args)
                    if cand is not None and cand["score"] > best["score"]:
                        best.update(cand)
                if best["loc"] is not None and best["score"] >= threshold:
                    return self._build_result(best, method_name, found=True)
        else:
            # ---------- 传统单遍遍历（向后兼容） ----------
            scales = self._build_scales(scale_range[0], scale_range[1], scale_step)
            for angle in angles:
                rotated_target = self._rotate_image(image_target, angle) if angle != 0.0 else image_target
                for scale in scales:
                    cand = self._scan_one_scale(image_source, rotated_target, scale, angle, *match_args)
                    if cand is not None and cand["score"] > best["score"]:
                        best.update(cand)

        # 统一裁决：所有尺度/角度尝试完毕后，只有过阈值才算命中
        if best["loc"] is None or best["score"] < threshold:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"best_score": float(max(best["score"], 0.0))},
            )
        return self._build_result(best, method_name, found=True)

    def _scan_one_scale(
        self,
        image_source: np.ndarray,
        rotated_target: np.ndarray,
        scale: float,
        angle: float,
        cv2_method: int,
        use_color: bool,
        channel_weights: List[float],
        method_name: str,
    ) -> Optional[Dict[str, Any]]:
        """
        在给定旋转角与尺度下执行一次模板匹配。

        坐标单一真源：本方法只缩放模板、大图始终为原始图像，因此返回的 loc
        直接就是大图坐标系下的左上角，size 为"本次实际使用的缩放后模板尺寸"。

        Returns:
            候选结果 dict(score/loc/size/scale/angle)；尺度非法（模板过小或
            超过大图）时返回 None（跳过，不参与最优竞争）。
        """
        src_h, src_w = image_source.shape[:2]
        new_w = max(1, int(round(rotated_target.shape[1] * scale)))
        new_h = max(1, int(round(rotated_target.shape[0] * scale)))
        if new_w > src_w or new_h > src_h or new_w < 4 or new_h < 4:
            return None
        resized = cv2.resize(rotated_target, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        score, loc = self._match_template_once(
            image_source, resized, cv2_method, use_color, channel_weights
        )
        if method_name in _LOWER_IS_BETTER_METHODS:
            score = 1.0 - score  # 统一为"越大越好"语义
        return {
            "score": score, "loc": loc, "size": (new_w, new_h),
            "scale": float(scale), "angle": float(angle),
        }

    @staticmethod
    def _build_scales(lo: float, hi: float, step: float) -> List[float]:
        """生成 [lo, hi] 闭区间、步长 step 的尺度列表（浮点容差 1e-6 含右端点）。"""
        return [float(s) for s in np.arange(lo, hi + 1e-6, step)]

    @staticmethod
    def _build_result(best: Dict[str, Any], method_name: str, found: bool) -> MatchResult:
        """根据 best 候选构造统一 MatchResult（中心点用实际缩放尺寸计算）。"""
        if not found or best["loc"] is None:
            return MatchResult.not_found(
                method_used="template",
                extra={"best_score": float(max(best["score"], 0.0))},
            )
        x, y = best["loc"]
        w, h = best["size"]
        cx, cy = x + w // 2, y + h // 2
        return MatchResult(
            found=True,
            rect=(int(x), int(y), int(w), int(h)),
            center_point=(int(cx), int(cy)),
            confidence=float(best["score"]),
            angle=float(best["angle"]),
            scale=float(best["scale"]),
            method_used="template",
            extra={"cv2_method": method_name},
        )

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _match_template_once(
        source: np.ndarray,
        target: np.ndarray,
        cv2_method: int,
        use_color: bool,
        channel_weights: List[float],
    ) -> Tuple[float, Tuple[int, int]]:
        """
        执行一次 matchTemplate 调用，返回 (最优得分, 最优位置左上角坐标)。

        当 use_color=True 时，分别对 B/G/R 三通道做 matchTemplate，
        再按 channel_weights 加权求和得到综合响应图，取全局最大值位置；
        这样比单纯转灰度图更能区分"色彩不同但亮度相近"的图标差异。
        """
        if use_color and source.ndim == 3 and target.ndim == 3:
            src_bgr = ensure_bgr(source)
            tgt_bgr = ensure_bgr(target)
            combined = None
            for ch in range(3):
                res = cv2.matchTemplate(
                    src_bgr[:, :, ch], tgt_bgr[:, :, ch], cv2_method
                )
                weighted = res * channel_weights[ch]
                combined = weighted if combined is None else combined + weighted
            result_map = combined
        else:
            src_gray = to_gray(source)
            tgt_gray = to_gray(target)
            result_map = cv2.matchTemplate(src_gray, tgt_gray, cv2_method)

        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result_map)
        if cv2_method in _LOWER_IS_BETTER_METHODS:
            return float(min_val), min_loc
        return float(max_val), max_loc

    @staticmethod
    def _rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
        """将图像绕中心旋转 angle 度，输出画布自动扩展以避免内容被裁切。"""
        h, w = image.shape[:2]
        center = (w / 2, h / 2)
        rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)

        # 计算旋转后需要的新画布尺寸，避免旋转后角落内容被裁掉
        cos = abs(rot_mat[0, 0])
        sin = abs(rot_mat[0, 1])
        new_w = int(h * sin + w * cos)
        new_h = int(h * cos + w * sin)
        rot_mat[0, 2] += (new_w - w) / 2
        rot_mat[1, 2] += (new_h - h) / 2

        border_value = (255, 255, 255) if image.ndim == 3 else 255
        return cv2.warpAffine(
            image, rot_mat, (new_w, new_h),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=border_value,
        )
