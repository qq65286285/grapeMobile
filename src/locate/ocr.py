"""
src/locate/ocr.py — Step B1: OCR 定位
======================================
用 RapidOCR 在截图中找文字,模糊匹配目标,推断图标位置。

RapidOCR 返回格式:result 是 list of [bbox(4 points), text, confidence]
bbox 是 4 个点的多边形 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],需要转成 (x1,y1,x2,y2) 矩形。
"""

from __future__ import annotations

import re
from typing import List, Tuple

import cv2
import numpy as np
from rapidfuzz import fuzz

import cvio  # noqa: F401  项目约定:图像读写统一走 cvio(兼容中文路径)

from .config import OCRConfig
from .types import Candidate, Intent


# --------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------
def _normalize_text(s: str) -> str:
    """归一化:小写、去空格和标点(保留字母数字与中文)。"""
    if not s:
        return ""
    return re.sub(r'[\s\W_]+', '', s.lower(), flags=re.UNICODE)


def _bbox_from_poly(poly) -> Tuple[int, int, int, int]:
    """RapidOCR 的 4 点多边形 bbox -> (x1,y1,x2,y2) 整数矩形。"""
    pts = np.asarray(poly, dtype=np.float32).reshape(-1, 2)
    x1, y1 = float(pts[:, 0].min()), float(pts[:, 1].min())
    x2, y2 = float(pts[:, 0].max()), float(pts[:, 1].max())
    return (int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2)))


# --------------------------------------------------------------------
# OcrLocator
# --------------------------------------------------------------------
class OcrLocator:
    """基于 RapidOCR 的元素定位器。"""

    def __init__(self, config: OCRConfig):
        self.config = config
        self._engine = None  # 惰性初始化

    # ------------------------------------------------------------------
    # 引擎
    # ------------------------------------------------------------------
    def _ensure_engine(self):
        """惰性初始化 RapidOCR 引擎。"""
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
        return self._engine

    def _run_ocr(self, image: np.ndarray):
        """
        跑 OCR。

        Returns:
            [(bbox_rect, text, confidence), ...]
        """
        engine = self._ensure_engine()
        result, _ = engine(image, use_det=True, use_cls=False, use_rec=True)
        items = []
        if not result:
            return items
        for entry in result:
            # entry: [poly, text, conf]
            if not entry or len(entry) < 3:
                continue
            poly, text, conf = entry[0], entry[1], entry[2]
            rect = _bbox_from_poly(poly)
            text = text or ""
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = 0.0
            items.append((rect, text, conf))
        return items

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    @staticmethod
    def _match_score(target_norm: str, text_norm: str) -> Tuple[float, bool, bool]:
        """
        计算模糊匹配得分。

        Returns:
            (score, is_exact, is_prefix)
            - 精确匹配(归一化后相等): score=1.0, is_exact=True
            - 前缀匹配(文字可能被截断): score=0.95, is_prefix=True
            - 模糊匹配: score = rapidfuzz 编辑距离相似度
        """
        if not target_norm or not text_norm:
            return 0.0, False, False
        if text_norm == target_norm:
            return 1.0, True, False
        # 前缀匹配(任一方是另一方的前缀)
        if text_norm.startswith(target_norm) or target_norm.startswith(text_norm):
            return 0.95, False, True
        # 编辑距离相似度(rapidfuzz.ratio 返回 0~100)
        ratio = fuzz.ratio(target_norm, text_norm) / 100.0
        return ratio, False, False

    # ------------------------------------------------------------------
    # 图标定位
    # ------------------------------------------------------------------
    def _find_icon(
        self,
        image: np.ndarray,
        text_rect: Tuple[int, int, int, int],
    ) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
        """
        在文字框正上方搜索图标主体,返回 (icon_bbox, icon_center)。

        流程:
            1. 取搜索区域:宽 = max(文字宽, 文字宽*1.3),高 = 文字高 * icon_search_ratio
            2. 在该区域上做 灰度→二值化→findContours,取最大连通块
            3. 连通块中心作为图标中心;找不到则走兜底坐标
        """
        x1, y1, x2, y2 = text_rect
        tw, th = x2 - x1, y2 - y1
        if tw <= 0 or th <= 0:
            return self._fallback_icon(text_rect)

        # 搜索区域尺寸
        sw = max(tw, int(tw * 1.3))
        sh = int(th * self.config.icon_search_ratio)
        if sh <= 0:
            return self._fallback_icon(text_rect)

        # 居中对齐到文字框,顶部向上偏移 sh
        sx = x1 + (tw - sw) // 2
        sy = y1 - sh

        # 裁剪到图像范围,且不越过文字框顶
        h, w = image.shape[:2]
        sx = max(0, sx)
        sy = max(0, sy)
        ex = min(w, sx + sw)
        ey = min(y1, sy + sh)  # 不越过文字框顶
        if ex <= sx or ey <= sy:
            return self._fallback_icon(text_rect)

        region = image[sy:ey, sx:ex]
        if region is None or region.size == 0:
            return self._fallback_icon(text_rect)

        # 灰度
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if region.ndim == 3 else region.copy()
        # OTSU 自适应二值化(图标主体通常比背景深,故用 INV)
        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        # 形态学闭运算,填补主体内部小孔洞
        kernel = np.ones((3, 3), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return self._fallback_icon(text_rect)

        # 取面积最大的连通块
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < 5:  # 太小视为噪声
            return self._fallback_icon(text_rect)

        # 还原到原图坐标
        rx, ry, rw, rh = cv2.boundingRect(c)
        ix1 = sx + rx
        iy1 = sy + ry
        ix2 = sx + rx + rw
        iy2 = sy + ry + rh
        icon_bbox = (ix1, iy1, ix2, iy2)
        icon_center = ((ix1 + ix2) // 2, (iy1 + iy2) // 2)
        return icon_bbox, icon_center

    @staticmethod
    def _fallback_icon(
        text_rect: Tuple[int, int, int, int],
    ) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
        """
        兜底:图标中心 = (文字框中心x, 文字框顶y - 0.9*文字高),
        bbox = 以文字宽为边长的正方形(中心对齐)。
        """
        x1, y1, x2, y2 = text_rect
        tw, th = x2 - x1, y2 - y1
        cx = (x1 + x2) // 2
        cy = int(y1 - 0.9 * th)
        half = tw // 2
        icon_bbox = (cx - half, cy - half, cx + half, cy + half)
        return icon_bbox, (cx, cy)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def locate(self, image: np.ndarray, intent: Intent) -> List[Candidate]:
        """
        在截图中定位目标。

        Args:
            image: BGR 格式的 numpy 数组。
            intent: 解析后的意图。

        Returns:
            候选列表(按 score 降序;无目标文字或无匹配时返回空列表)。
        """
        if image is None or image.size == 0:
            return []

        target_text = (intent.target_text or "").strip()
        target_norm = _normalize_text(target_text)
        # OCR 必须有目标文字才能模糊匹配(纯图标场景应由 VLM 处理)
        if not target_norm:
            return []

        items = self._run_ocr(image)
        if not items:
            return []

        threshold = self.config.fuzzy_threshold
        candidates: List[Candidate] = []

        for rect, text, conf in items:
            text_norm = _normalize_text(text)
            score, is_exact, is_prefix = self._match_score(target_norm, text_norm)
            # 精确 / 前缀匹配直接通过;模糊匹配需达到阈值
            if not (is_exact or is_prefix or score >= threshold):
                continue

            x1, y1, x2, y2 = rect
            text_center = ((x1 + x2) // 2, (y1 + y2) // 2)

            if intent.target_type == "icon_with_label":
                # 文字框在图标下方,推断图标位置
                icon_bbox, icon_point = self._find_icon(image, rect)
                cand = Candidate(
                    bbox=icon_bbox,
                    point=icon_point,
                    score=score,
                    source="ocr",
                    text=text,
                    confidence=conf,
                )
            else:
                # text / icon / other:用文字框本身
                cand = Candidate(
                    bbox=rect,
                    point=text_center,
                    score=score,
                    source="ocr",
                    text=text,
                    confidence=conf,
                )

            candidates.append(cand)

        # 按分数降序
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates
