"""
src/cvio.py - 兼容 Unicode 路径的图像读写
=========================================
Windows 上 cv2.imread / cv2.imwrite 底层走 ANSI API,路径含中文等非 ASCII
字符(如 runner/scripts/进入游戏/images/0001.png)时会静默失败:
imread 返回 None、imwrite 返回 False。

本模块用 numpy 中转实现等价能力,签名与 cv2.imread/imwrite 对齐:
    - imread(path, flags)  -> np.ndarray | None
    - imwrite(path, image) -> bool

项目内凡是读取/写入"路径可能含中文"的图片(典型:用户用中文命名的脚本锚点图),
都应使用本模块,不要直接调用 cv2.imread/imwrite。
纯英文临时路径(如 %TEMP%/xxx.png)用原 API 也无问题。
"""

from __future__ import annotations

import os
from typing import Optional

import cv2
import numpy as np


def imread(path: str, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """
    读取图片(支持中文等非 ASCII 路径)。

    Args:
        path: 图片路径。
        flags: cv2 读取标志,默认 IMREAD_COLOR(BGR 三通道);
               传 cv2.IMREAD_GRAYSCALE 读灰度。

    Returns:
        图像 ndarray;文件不存在/解码失败时返回 None(与 cv2.imread 行为一致)。
    """
    try:
        # np.fromfile 以二进制读取(路径编码由 Python/numpy 正确处理),
        # 再从内存缓冲区解码,绕开 cv2 的 ANSI 路径限制
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite(path: str, image: np.ndarray, ext: str = ".png") -> bool:
    """
    写入图片(支持中文等非 ASCII 路径)。

    Args:
        path: 目标路径;父目录不存在时会自动创建。
        image: 待写入图像(BGR 或灰度)。
        ext: 编码格式,按扩展名自动选择,默认 ".png"。

    Returns:
        True 写入成功;False 编码或写盘失败。
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # 先在内存编码,再用 tofile 落盘,绕开 cv2 的 ANSI 路径限制
    ok, buf = cv2.imencode(ext, image)
    if not ok:
        return False
    try:
        buf.tofile(path)
    except OSError:
        return False
    return True
