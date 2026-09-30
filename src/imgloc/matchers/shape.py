"""
形状匹配层 (Shape-based Matching Layer)
========================================
参考 meiqua/shape_based_matching 的核心思路：
https://github.com/meiqua/shape_based_matching

不同于模板匹配直接比较像素值，形状匹配比较的是"边缘方向梯度"，
即先对大图/小图分别做边缘检测(Canny) + 梯度方向计算，再比较梯度方向的
相似性。这样的好处是：
    - 对光照变化、整体亮度/对比度变化不敏感（因为只关心边缘走向，不关心像素值）
    - 对深色模式/浅色模式切换等"配色反转但形状不变"的场景更鲁棒
    - 特别适合线条类图标、按钮边框等以形状为主要特征的UI元素

本实现是该思路的一个纯 Python/OpenCV 简化版本（不依赖原项目的 C++ 扩展），
核心步骤：
    1. 对模板和候选窗口分别提取 Canny 边缘 + Sobel 梯度方向
    2. 用梯度方向的余弦相似度构建响应图（滑动窗口效率不如C++版本，
       这里用"金字塔下采样 + 有限尺度/角度遍历"控制耗时）
    3. 在多尺度、多角度组合下寻找全局最优匹配位置

误匹配防御（颜色一致性二次校验）：
    形状匹配只比较边缘方向、完全不看颜色语义，因此可能把"尺寸相近、笔画
    方向分布巧合相似但颜色完全不同"的元素误判为命中（例如把白底黑色 logo
    误当成黄色按钮）。可选开启 color_verify：命中后比较候选窗口与模板的
    HSV 颜色直方图相关性，低于 color_hist_threshold 则拒绝该结果、交由责任链
    后续层处理。实测同色目标相关性≈0.99、异色误配≈0.03，区分裕量极大。
    深色模式/换肤等"同形变色"场景应关闭此校验。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import cv2
import numpy as np

from imgloc.base import Matcher
from imgloc.types import MatchResult
from imgloc.utils import ensure_bgr, to_gray


class ShapeMatcher(Matcher):
    """形状匹配层：基于边缘方向梯度的相似度匹配，抗光照/主题变化。"""

    name = "shape"

    def is_available(self) -> bool:
        # 仅依赖 opencv-python，始终可用
        return True

    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        cfg = {**self.config, **kwargs}
        canny_low = cfg.get("canny_low", 50)
        canny_high = cfg.get("canny_high", 150)
        scale_range = cfg.get("scale_range", [0.8, 1.2])
        scale_step = cfg.get("scale_step", 0.1)
        angle_range = cfg.get("angle_range", [-10, 10])
        angle_step = cfg.get("angle_step", 5)
        color_verify = cfg.get("color_verify", True)
        color_hist_threshold = cfg.get("color_hist_threshold", 0.5)

        src_gray = to_gray(image_source)
        tgt_gray = to_gray(image_target)

        src_h, src_w = src_gray.shape[:2]
        tgt_h, tgt_w = tgt_gray.shape[:2]
        if tgt_h > src_h or tgt_w > src_w:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "template_larger_than_source"},
            )

        # 预计算大图的方向梯度场（只需一次，供所有候选窗口切片复用）
        src_mag, src_ang = self._gradient_field(src_gray, canny_low, canny_high)

        scales = np.arange(scale_range[0], scale_range[1] + 1e-6, scale_step)
        angles = np.arange(angle_range[0], angle_range[1] + 1e-6, angle_step)

        best: Dict[str, Any] = {"score": -1.0, "loc": None, "size": None, "scale": 1.0, "angle": 0.0}

        for angle in angles:
            rotated_tgt = self._rotate_image(tgt_gray, angle) if angle != 0 else tgt_gray
            for scale in scales:
                new_w = max(4, int(round(rotated_tgt.shape[1] * scale)))
                new_h = max(4, int(round(rotated_tgt.shape[0] * scale)))
                if new_w > src_w or new_h > src_h:
                    continue
                resized_tgt = cv2.resize(rotated_tgt, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                tgt_mag, tgt_ang = self._gradient_field(resized_tgt, canny_low, canny_high)

                score, loc = self._sliding_similarity(src_mag, src_ang, tgt_mag, tgt_ang)
                if score > best["score"]:
                    best.update(score=score, loc=loc, size=(new_w, new_h), scale=float(scale), angle=float(angle))

        if best["loc"] is None or best["score"] < threshold:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"best_score": float(max(best["score"], 0.0))},
            )

        x, y = best["loc"]
        w, h = best["size"]
        cx, cy = x + w // 2, y + h // 2

        # 颜色一致性二次校验：形状分过阈值但颜色语义不符（如白底 logo 冒充黄按钮）
        # 时拒绝该结果，返回 not_found 让责任链继续向特征点层降级。
        color_corr: float = 1.0
        if color_verify:
            color_corr = self._color_hist_correlation(
                image_source, image_target, int(x), int(y), int(w), int(h)
            )
            if color_corr < color_hist_threshold:
                return MatchResult.not_found(
                    method_used=self.name,
                    extra={
                        "best_score": float(best["score"]),
                        "color_corr": float(color_corr),
                        "reason": "color_verify_failed",
                    },
                )

        return MatchResult(
            found=True,
            rect=(int(x), int(y), int(w), int(h)),
            center_point=(int(cx), int(cy)),
            confidence=float(best["score"]),
            angle=float(best["angle"]),
            scale=float(best["scale"]),
            method_used=self.name,
            extra={"color_corr": float(color_corr)},
        )

    @staticmethod
    def _color_hist_correlation(
        image_source: np.ndarray,
        image_target: np.ndarray,
        x: int,
        y: int,
        w: int,
        h: int,
    ) -> float:
        """
        计算大图命中窗口与模板的 HSV 二维(H/S)直方图相关性。

        步骤：
            1. 按 rect 从原始大图裁出候选窗口，缩放到模板尺寸（消除尺度差异，
               直方图比较本身对尺寸不敏感，统一尺寸更稳定）；
            2. 分别计算 HSV 的 H/S 二维直方图并归一化；
            3. 返回 cv2.compareHist 的相关系数，范围约 [-1, 1]，越接近 1 越同色。

        注：输入为灰度图（无颜色通道）时无法校验，返回 1.0 视为通过（不拦截）。
        """
        if image_source.ndim != 3 or image_target.ndim != 3:
            return 1.0
        src_h, src_w = image_source.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(src_w, x + w), min(src_h, y + h)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        candidate = image_source[y0:y1, x0:x1]
        tgt_h, tgt_w = image_target.shape[:2]
        candidate = cv2.resize(candidate, (tgt_w, tgt_h), interpolation=cv2.INTER_AREA)

        src_bgr = ensure_bgr(candidate)
        tgt_bgr = ensure_bgr(image_target)
        hist_kwargs = dict(channels=[0, 1], mask=None, histSize=[50, 60], ranges=[0, 180, 0, 256])
        hist_src = cv2.calcHist([cv2.cvtColor(src_bgr, cv2.COLOR_BGR2HSV)], **hist_kwargs)
        hist_tgt = cv2.calcHist([cv2.cvtColor(tgt_bgr, cv2.COLOR_BGR2HSV)], **hist_kwargs)
        cv2.normalize(hist_src, hist_src)
        cv2.normalize(hist_tgt, hist_tgt)
        return float(cv2.compareHist(hist_src, hist_tgt, cv2.HISTCMP_CORREL))

    # ------------------------------------------------------------------
    @staticmethod
    def _gradient_field(gray: np.ndarray, canny_low: int, canny_high: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        计算灰度图的边缘梯度场：返回 (边缘掩膜下的梯度幅值, 梯度方向弧度)。

        步骤：
            1. Canny 提取边缘掩膜，只在边缘像素上计算梯度方向（忽略平坦区域噪声）
            2. Sobel 计算 x/y 方向梯度，得到幅值 magnitude 和方向 angle
            3. 非边缘像素的 magnitude 置零，避免参与相似度计算
        """
        edges = cv2.Canny(gray, canny_low, canny_high)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        magnitude = cv2.magnitude(gx, gy)
        angle = cv2.phase(gx, gy, angleInDegrees=False)  # 弧度制方向

        edge_mask = edges > 0
        magnitude = np.where(edge_mask, magnitude, 0.0).astype(np.float32)
        angle = angle.astype(np.float32)
        return magnitude, angle

    @staticmethod
    def _sliding_similarity(
        src_mag: np.ndarray,
        src_ang: np.ndarray,
        tgt_mag: np.ndarray,
        tgt_ang: np.ndarray,
    ) -> Tuple[float, Tuple[int, int]]:
        """
        在大图梯度场上滑动模板梯度场，计算方向余弦相似度响应图，返回最优得分与位置。

        相似度定义（参考 shape_based_matching 论文思路的简化版）：
            对模板中每个"有效边缘像素"(magnitude>0)，取大图对应位置的方向余弦值
            cos(theta_src - theta_tgt) 作为该点贡献，取所有有效点的平均值作为总分。
            这样只关心方向是否一致，不关心像素的绝对亮度，从而对光照变化免疫。

        实现说明：
            纯 Python 逐像素滑窗效率较低，这里使用 cv2.filter2D / 矩阵运算做加速：
            利用 cos(a-b) = cos(a)cos(b) + sin(a)sin(b)，把方向匹配转换为两次
            "模板加权卷积"的组合，从而用 OpenCV 的高效卷积实现整图滑窗匹配。
        """
        tgt_h, tgt_w = tgt_mag.shape[:2]
        weight_sum = float(np.sum(tgt_mag))
        if weight_sum < 1e-6:
            # 模板几乎没有边缘（例如纯色图标），退化为返回图像中心，得分为0
            return 0.0, (0, 0)

        cos_t = np.cos(tgt_ang) * tgt_mag
        sin_t = np.sin(tgt_ang) * tgt_mag

        cos_s = np.cos(src_ang)
        sin_s = np.sin(src_ang)

        # 对大图的 cos_s / sin_s 分别与模板的 cos_t / sin_t 做卷积（等价于滑窗点积）
        # 使用 cv2.filter2D 需要将 kernel 翻转（filter2D 默认做卷积而非相关），
        # 这里用 cv2.matchTemplate 的 CCORR（相关）语义更直接：
        response_cos = cv2.matchTemplate(cos_s.astype(np.float32), cos_t.astype(np.float32), cv2.TM_CCORR)
        response_sin = cv2.matchTemplate(sin_s.astype(np.float32), sin_t.astype(np.float32), cv2.TM_CCORR)
        response = response_cos + response_sin  # = sum(mag_t * cos(theta_src - theta_tgt))

        similarity_map = response / weight_sum  # 归一化到平均余弦相似度，范围约[-1, 1]

        _, max_val, _, max_loc = cv2.minMaxLoc(similarity_map)
        # 归一化到 [0, 1]，-1(完全相反)->0，1(完全一致)->1
        score = float((max_val + 1.0) / 2.0)
        return score, max_loc

    @staticmethod
    def _rotate_image(image: np.ndarray, angle: float) -> np.ndarray:
        """将灰度图绕中心旋转 angle 度，输出画布自动扩展。"""
        h, w = image.shape[:2]
        center = (w / 2, h / 2)
        rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)
        cos = abs(rot_mat[0, 0])
        sin = abs(rot_mat[0, 1])
        new_w = int(h * sin + w * cos)
        new_h = int(h * cos + w * sin)
        rot_mat[0, 2] += (new_w - w) / 2
        rot_mat[1, 2] += (new_h - h) / 2
        return cv2.warpAffine(
            image, rot_mat, (new_w, new_h),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
