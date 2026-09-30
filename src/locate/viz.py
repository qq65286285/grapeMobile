"""
src/locate/viz.py — 定位可视化工具
====================================
在截图上画候选框、十字、标签,生成预览图供 GUI 展示。
所有图像读写走 cvio(兼容中文路径)。
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

import cv2
import numpy as np

import cvio

# 候选框颜色(按来源)
_SOURCE_COLORS = {
    "ocr": (0, 200, 0),       # 绿
    "vlm": (200, 0, 0),       # 蓝(BGR)
    "template": (0, 0, 200),  # 红
    "verified": (0, 200, 200),# 黄
    "selected": (200, 200, 0),# 青
}


def draw_candidates(
    image: np.ndarray,
    candidates: List,
    highlight_idx: Optional[int] = None,
) -> np.ndarray:
    """
    在图像副本上画所有候选框 + 标签。

    Args:
        image: 原始截图(BGR)。
        candidates: Candidate 列表。
        highlight_idx: 高亮(选中)的候选索引,画更粗的框 + 十字。

    Returns:
        标注后的图像副本。
    """
    canvas = image.copy()
    for i, c in enumerate(candidates):
        x1, y1, x2, y2 = c.bbox
        is_hl = (i == highlight_idx)
        color = _SOURCE_COLORS.get("selected" if is_hl else c.source, (128, 128, 128))
        thickness = 3 if is_hl else 1
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness)

        label = f"[{c.source}] {c.text}"
        if is_hl:
            label += " ★"
        # 标签背景
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(canvas, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), color, -1)
        cv2.putText(canvas, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)

        if is_hl:
            # 画十字
            cx, cy = c.point
            cv2.drawMarker(canvas, (cx, cy), color, cv2.MARKER_CROSS, 30, 2)
    return canvas


def draw_result(
    image: np.ndarray,
    bbox: Tuple[int, int, int, int],
    point: Tuple[int, int],
    confidence: float = 0.0,
    label: str = "",
) -> np.ndarray:
    """
    画最终定位结果:单个框 + 十字 + 置信度标签。

    Args:
        image: 原始截图(BGR)。
        bbox: 目标框。
        point: 点击中心。
        confidence: 置信度。
        label: 附加文字。

    Returns:
        标注后的图像副本。
    """
    canvas = image.copy()
    x1, y1, x2, y2 = bbox
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 200, 200), 3)
    cx, cy = point
    cv2.drawMarker(canvas, (cx, cy), (0, 200, 200), cv2.MARKER_CROSS, 40, 2)
    text = f"({confidence:.0%}) {label}".strip()
    if text:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(canvas, (10, 10), (20 + tw, 20 + th), (0, 200, 200), -1)
        cv2.putText(canvas, text, (15, 10 + th + 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
    return canvas


def save_debug_image(image: np.ndarray, path: str) -> str:
    """用 cvio 保存调试图(兼容中文路径),返回路径。"""
    cvio.imwrite(path, image)
    return path


def crop_and_pad(
    image: np.ndarray,
    bbox: Tuple[int, int, int, int],
    expand: float = 0.2,
    min_size: int = 256,
) -> np.ndarray:
    """
    裁剪 bbox 区域,外扩 expand 比例,放缩到至少 min_size 边长。

    Args:
        image: 原图(BGR)。
        bbox: 裁剪框。
        expand: 外扩比例(0.2 = 向外扩 20%)。
        min_size: 最小边长(不足则放大)。

    Returns:
        裁剪并放大后的图像。
    """
    h, w = image.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    ex, ey = int(bw * expand), int(bh * expand)
    x1 = max(0, x1 - ex)
    y1 = max(0, y1 - ey)
    x2 = min(w, x2 + ex)
    y2 = min(h, y2 + ey)
    crop = image[y1:y2, x1:x2]
    ch, cw = crop.shape[:2]
    if max(ch, cw) < min_size:
        scale = min_size / max(1, max(ch, cw))
        crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)),
                          interpolation=cv2.INTER_LANCZOS4)
    return crop
