"""
imgloc 图像输入工具函数
=======================
统一处理"用户可能传入路径字符串 / bytes / numpy数组"三种情况，
所有 Matcher 在匹配前都应先调用 load_image 做标准化。
"""

from __future__ import annotations

import os
from typing import Union

import numpy as np
import cv2

from imgloc.exceptions import ImageLoadError

# 允许的图像输入类型：文件路径 / 字节流 / 已解码的 numpy 数组(BGR, cv2默认格式)
ImageInput = Union[str, bytes, np.ndarray]


def load_image(image: ImageInput, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """
    将任意支持的输入类型统一加载为 numpy 数组（BGR格式，与 cv2 默认一致）。

    Args:
        image: 文件路径(str) / 图像字节流(bytes) / 已解码的 numpy 数组。
        flags: cv2.imread 的读取模式，默认彩色图。传入 numpy 数组时忽略此参数。

    Returns:
        np.ndarray: BGR 格式的图像数组，shape 为 (H, W, 3) 或灰度图 (H, W)。

    Raises:
        ImageLoadError: 路径不存在、字节流损坏、或数组为空/维度非法时抛出。
    """
    if isinstance(image, np.ndarray):
        if image.size == 0:
            raise ImageLoadError("传入的图像数组为空 (size == 0)")
        return image

    if isinstance(image, bytes):
        arr = np.frombuffer(image, dtype=np.uint8)
        decoded = cv2.imdecode(arr, flags)
        if decoded is None:
            raise ImageLoadError("图像字节流解码失败，可能数据损坏或格式不支持")
        return decoded

    if isinstance(image, str):
        if not os.path.isfile(image):
            raise ImageLoadError(f"图像文件不存在: {image}")
        decoded = cv2.imread(image, flags)
        if decoded is None:
            raise ImageLoadError(f"图像文件读取失败(可能格式不支持或文件损坏): {image}")
        return decoded

    raise ImageLoadError(
        f"不支持的图像输入类型: {type(image)}，仅支持 str(路径) / bytes / numpy.ndarray"
    )


def to_gray(image: np.ndarray) -> np.ndarray:
    """将 BGR 或 BGRA 图像转换为灰度图；若已是灰度图则直接返回。"""
    if image.ndim == 2:
        return image
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def ensure_bgr(image: np.ndarray) -> np.ndarray:
    """确保图像为 3 通道 BGR；灰度图会被转换，BGRA 会去掉 alpha 通道。"""
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def clip_rect_to_bounds(x: int, y: int, w: int, h: int, img_w: int, img_h: int) -> tuple:
    """将矩形裁剪到图像边界内，避免越界坐标导致下游崩溃。"""
    x0 = max(0, min(x, img_w - 1))
    y0 = max(0, min(y, img_h - 1))
    x1 = max(0, min(x + w, img_w))
    y1 = max(0, min(y + h, img_h))
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)
