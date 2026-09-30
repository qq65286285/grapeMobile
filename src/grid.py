"""
src/grid.py - 网格坐标标注器
============================
在 App 截图上叠加"Excel 风格"的坐标网格：
    - 列标记 A、B、C …… Z、AA、AB（26 进制进位）
    - 行标记 1、2、3 ……
用户查看生成的网格图后报出格子编号（如 "A3"），本模块即可换算为该格子
在【原始截图】中的中心点像素坐标，直接用于 adb shell input tap。

坐标映射（单一真源）：
    标注图 = 原图四周外扩 margin 边距条带（用于写列/行标签），
    网格本身与原图严格 1:1，无任何缩放。因此格子 (col, row) 的中心在
    原图坐标系中为:
        x = col * cell_size + min(cell_size, W - col * cell_size) / 2
        y = row * cell_size + min(cell_size, H - row * cell_size) / 2
    最后一格若不足一个 cell_size（宽/高不能整除时），按实际剩余尺寸取中心。

典型用法:
    >>> import cv2
    >>> from grid import GridMarker
    >>> img = cv2.imread("runner/tmp/image/screenshot.png")
    >>> marker = GridMarker(cell_size=60)
    >>> annotated = marker.draw(img)            # 生成带网格的标注图
    >>> cv2.imwrite("runner/tmp/image/grid_screenshot.png", annotated)
    >>> marker.ref_to_point("A3", img.shape)    # -> (30, 150) 原图中心点像素
"""

from __future__ import annotations

import re
from typing import Tuple

import cv2
import numpy as np

# 格子编号：1~3 个字母（列）+ 行号（整数或带小数），如 A1 / BC23 / AP1.5
_CELL_REF_PATTERN = re.compile(r"^([A-Za-z]{1,3})(\d{1,3}(?:\.\d+)?)$")


def col_name(col_index: int) -> str:
    """
    0 基列序号 -> Excel 风格列名：0->A, 25->Z, 26->AA, 27->AB。

    Args:
        col_index: 从 0 开始的列序号，必须 >= 0。

    Returns:
        大写列名字符串。

    Raises:
        ValueError: 列序号为负数时抛出。
    """
    if col_index < 0:
        raise ValueError(f"列序号不能为负: {col_index}")
    chars = ""
    n = col_index
    while True:
        n, rem = divmod(n, 26)
        chars = chr(65 + rem) + chars
        if n == 0:
            break
        n -= 1  # Excel 进位没有"零列"，借位后偏移 1
    return chars


def col_index(col_name_str: str) -> int:
    """
    Excel 风格列名 -> 0 基列序号：A->0, Z->25, AA->26。

    Raises:
        ValueError: 列名含非字母字符或为空时抛出。
    """
    s = col_name_str.upper()
    if not s or not s.isalpha():
        raise ValueError(f"非法列名: {col_name_str!r}")
    idx = 0
    for ch in s:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def parse_ref(ref: str) -> Tuple[int, float]:
    """
    解析格子编号 -> (col_index, row_index)，列 0 基整数，行 0 基浮点数。

    行号支持小数（连续行坐标）：整数位选行，小数位表示格内偏移。
    例如 "AP1.5"：列 AP，行取第 1 行与第 2 行中间（即两行之间的网格线）。

    Args:
        ref: 格子编号，如 "A3"、"BC23"、"AP1.5"（大小写字母均可）。

    Returns:
        (列序号0基, 行序号0基浮点)，例如 "A3" -> (0, 2.0)，"AP1.5" -> (41, 0.5)。

    Raises:
        ValueError: 编号格式非法时抛出。
    """
    if not isinstance(ref, str):
        raise ValueError(f"格子编号必须是字符串，实际类型: {type(ref)}")
    m = _CELL_REF_PATTERN.match(ref.strip().upper())
    if m is None:
        raise ValueError(
            f"非法格子编号: {ref!r}，正确格式如 'A3' 或带小数 'AP1.5'（列字母+行号）"
        )
    row_no = float(m.group(2))
    if row_no < 1:
        raise ValueError(f"行号必须从 1 开始: {ref!r}")
    return col_index(m.group(1)), row_no - 1


class GridMarker:
    """
    网格坐标标注器：在截图上绘制带行列标签的网格，并提供格子编号->像素坐标换算。

    Args:
        cell_size: 每个网格格子的边长(像素，正方形格)。常用档位：
                   80=粗(约 9x16 格)、60=中(约 12x22 格，默认)、
                   40=细(约 18x32 格)；也支持任意自定义值。
        margin: 四周标签边距条带宽度(像素)，列字母/行数字绘制其中。
        major_every: 每 N 格画一条更醒目的主网格线（类似坐标纸刻度），0 表示关闭。
    """

    def __init__(self, cell_size: int = 60, margin: int = 40, major_every: int = 5):
        if cell_size < 20:
            raise ValueError(f"cell_size 过小会导致标签无法辨认: {cell_size}（最小 20）")
        self.cell_size = int(cell_size)
        self.margin = int(margin)
        self.major_every = int(major_every)

    # ------------------------------------------------------------------
    # 几何换算
    # ------------------------------------------------------------------
    def grid_dims(self, image_shape: Tuple[int, ...]) -> Tuple[int, int]:
        """返回 (列数, 行数)；image_shape 为 numpy 数组的 shape（H, W[, C]）。"""
        h, w = image_shape[0], image_shape[1]
        cols = int(np.ceil(w / self.cell_size))
        rows = int(np.ceil(h / self.cell_size))
        return cols, rows

    def cell_center(self, col: int, row: int, image_shape: Tuple[int, ...]) -> Tuple[int, int]:
        """
        格子 (col, row)（0 基）在原始截图坐标系中的中心点。

        最后一格宽度/高度不足 cell_size 时，按实际剩余尺寸取中心，
        保证点击点始终落在可见的最后一格区域正中。

        Raises:
            ValueError: 格子序号超出图片网格范围时抛出。
        """
        h, w = image_shape[0], image_shape[1]
        cols, rows = self.grid_dims(image_shape)
        if not (0 <= col < cols and 0 <= row < rows):
            raise ValueError(
                f"格子越界: {col_name(col)}{row + 1}，"
                f"当前网格共 {cols} 列({col_name(0)}~{col_name(cols - 1)}) x {rows} 行(1~{rows})"
            )
        x0 = col * self.cell_size
        y0 = row * self.cell_size
        cell_w = min(self.cell_size, w - x0)
        cell_h = min(self.cell_size, h - y0)
        return x0 + cell_w // 2, y0 + cell_h // 2

    def ref_to_point(self, ref: str, image_shape: Tuple[int, ...]) -> Tuple[int, int]:
        """
        格子编号 -> 原始截图像素坐标 (x, y)。

        - 整数行（如 'A3'）：格子中心（最后一格不足整格时按剩余尺寸取中心）。
        - 小数行（如 'AP1.5'）：x 取该列中心，y 按连续行坐标换算，
          y = (row_0基 + 0.5) * cell_size，即 1.5 正好落在第 1/2 行之间的网格线上；
          超出图片范围时钳制到图内。
        """
        col, row = parse_ref(ref)
        if float(row).is_integer():
            return self.cell_center(col, int(row), image_shape)

        h, w = image_shape[0], image_shape[1]
        cols, rows = self.grid_dims(image_shape)
        if not (0 <= col < cols and 0.0 <= row < rows):
            raise ValueError(
                f"格子越界: {ref!r}，"
                f"当前网格共 {cols} 列({col_name(0)}~{col_name(cols - 1)}) x {rows} 行(1~{rows})"
            )
        x0 = col * self.cell_size
        x = x0 + min(self.cell_size, w - x0) // 2
        y = int(round((row + 0.5) * self.cell_size))
        return x, max(0, min(y, h - 1))

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def draw(self, image: np.ndarray) -> np.ndarray:
        """
        在截图四周外扩边距并叠加网格与行列标签，返回标注图（不修改原图）。

        Returns:
            标注后的 BGR 图像，尺寸为 (H+2*margin, W+2*margin, 3)。
        """
        src = image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        h, w = src.shape[:2]
        m = self.margin
        cs = self.cell_size
        cols, rows = self.grid_dims(src.shape)

        # 深色边距底托 + 原图居中粘贴
        canvas = np.full((h + 2 * m, w + 2 * m, 3), (32, 32, 32), dtype=np.uint8)
        canvas[m:m + h, m:m + w] = src

        # ---- 半透明网格线（画在 overlay 上再融合，不遮挡截图可读性）----
        overlay = canvas.copy()
        # 竖线
        for i in range(cols + 1):
            x = m + i * cs
            if x > m + w:
                break
            is_major = self.major_every and (i % self.major_every == 0 or i == cols)
            color = (0, 255, 255) if is_major else (0, 200, 200)
            thick = 2 if is_major else 1
            cv2.line(overlay, (x, m), (x, m + h), color, thick, cv2.LINE_AA)
        # 横线
        for j in range(rows + 1):
            y = m + j * cs
            if y > m + h:
                break
            is_major = self.major_every and (j % self.major_every == 0 or j == rows)
            color = (0, 255, 255) if is_major else (0, 200, 200)
            thick = 2 if is_major else 1
            cv2.line(overlay, (m, y), (m + w, y), color, thick, cv2.LINE_AA)
        canvas = cv2.addWeighted(overlay, 0.45, canvas, 0.55, 0)

        # ---- 文字标签（顶/底列字母，左/右行数字，就近可读）----
        font = cv2.FONT_HERSHEY_SIMPLEX
        # 字号随边距/格宽自适应：保证不超出边距条带与格子宽度
        font_scale = max(0.4, min(m / 42.0, cs / 55.0))
        font_thickness = max(1, int(round(font_scale * 2)))

        for i in range(cols):
            x0 = m + i * cs
            cell_w = min(cs, w - i * cs)
            cx = x0 + cell_w // 2
            label = col_name(i)
            self._put_label(canvas, label, cx, m // 2, font, font_scale, font_thickness)
            self._put_label(canvas, label, cx, m + h + m // 2, font, font_scale, font_thickness)

        for j in range(rows):
            y0 = m + j * cs
            cell_h = min(cs, h - j * cs)
            cy = y0 + cell_h // 2
            label = str(j + 1)
            self._put_label(canvas, label, m // 2, cy, font, font_scale, font_thickness)
            self._put_label(canvas, label, m + w + m // 2, cy, font, font_scale, font_thickness)

        return canvas

    @staticmethod
    def _put_label(
        canvas: np.ndarray,
        text: str,
        cx: int,
        cy: int,
        font: int,
        font_scale: float,
        thickness: int,
    ) -> None:
        """以 (cx, cy) 为中心绘制黄色标签，带黑色描边保证在任意背景上可读。"""
        (tw, th), base = cv2.getTextSize(text, font, font_scale, thickness)
        origin = (cx - tw // 2, cy + th // 2)
        # 黑色描边（多次偏移绘制）+ 黄色主体
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            cv2.putText(canvas, text, (origin[0] + dx, origin[1] + dy),
                        font, font_scale, (0, 0, 0), thickness + 1, cv2.LINE_AA)
        cv2.putText(canvas, text, origin, font, font_scale, (0, 255, 255),
                    thickness, cv2.LINE_AA)


__all__ = ["GridMarker", "col_name", "col_index", "parse_ref"]
