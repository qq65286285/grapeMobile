"""
runner/grid.py - 网格坐标标注入口
==================================
在截图上生成"Excel 风格"坐标网格图（列 A/B/C…，行 1/2/3…），
用户看图报格子编号（如 A3），本脚本换算出原图像素坐标 + adb 点击命令。

完整流程:
    1. python runner/capture.py                 # 截图 -> 网格图 tmp/image/grid_screenshot.png
    2. 打开 grid_screenshot.png，找到目标格子编号，例如 A3
    3. python runner/grid.py A3                 # 输出坐标并直接执行 adb 点击
       python runner/grid.py A3 40              # 显式指定格子尺寸查询并点击

    其他用法:
    python runner/grid.py                       # 仅重新生成网格图(密度见下)
    python runner/grid.py 40                    # 按指定格子尺寸重新生成网格图

格子尺寸档位（以 720x1280 截图为例）:
    80  粗网格  约 9 列 x 16 行，标签最醒目，目标区域较大时用
    60  中网格  约 12 列 x 22 行（默认，精度与可读性平衡）
    40  细网格  约 18 列 x 32 行，格子中心最大偏差约 20px
    也支持任意自定义像素值（>=20）。
"""

from __future__ import annotations

import json
import os
import sys

import cv2

# 将 src/ 加入 sys.path（与 runner/main.py、capture.py 一致）
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from grid import GridMarker, col_name, parse_ref  # noqa: E402
from adbtools import AdbClient, AdbError  # noqa: E402

# ============================================================
# 默认配置：无参直接执行时使用的格子边长(像素)
#   60=中  30=细(默认)  20=超细
# ============================================================
CELL_SIZE = 30

# 固定文件位置
_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "image")
SOURCE_IMAGE = os.path.join(_IMAGE_DIR, "screenshot.png")
GRID_IMAGE = os.path.join(_IMAGE_DIR, "grid_screenshot.png")
GRID_META = os.path.join(_IMAGE_DIR, "grid_meta.json")
DEVICE_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_id.txt")


def _read_device_id() -> str:
    """读取 capture.py 截图时记录的 device_id;无记录时返回空字符串。"""
    if os.path.isfile(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _adb_prefix() -> str:
    """返回带 -s 的 adb 命令前缀(无设备记录时退回 "adb")。"""
    device_id = _read_device_id()
    return f"adb -s {device_id}" if device_id else "adb"


def _execute_tap(x: int, y: int) -> bool:
    """在截图所用设备上执行点击。无设备记录时跳过,返回 False。"""
    device_id = _read_device_id()
    if not device_id:
        print("[WARN] 未找到 device_id 记录(截图非经 capture.py 获得),仅输出命令未执行点击")
        return False
    try:
        client = AdbClient()
        client.attach(device_id)  # 校验在线并记录 device_id
        client.tap(x, y)
    except AdbError as exc:
        print(f"[ERROR] 点击执行失败: {exc}")
        return False
    print(f"已执行点击: adb -s {device_id} shell input tap {x} {y}")
    return True


def _generate(cell_size: int) -> int:
    """读取原图生成网格标注图，并写入 sidecar 元数据供查询时使用。"""
    if not os.path.isfile(SOURCE_IMAGE):
        print(f"[ERROR] 未找到原图: {SOURCE_IMAGE}")
        print("        请先运行: python runner/capture.py <device_id> 截图")
        return 1

    img = cv2.imread(SOURCE_IMAGE)
    if img is None:
        print(f"[ERROR] 图片读取失败（文件可能已损坏）: {SOURCE_IMAGE}")
        return 1

    marker = GridMarker(cell_size=cell_size)
    annotated = marker.draw(img)
    cv2.imwrite(GRID_IMAGE, annotated)

    cols, rows = marker.grid_dims(img.shape)
    meta = {
        "cell_size": cell_size,
        "image_width": int(img.shape[1]),
        "image_height": int(img.shape[0]),
        "cols": cols,
        "rows": rows,
    }
    with open(GRID_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("=" * 56)
    print(f"网格图已生成: {GRID_IMAGE}")
    print(f"原图尺寸: {img.shape[1]}x{img.shape[0]}  格子: {cell_size}x{cell_size}px")
    print(f"网格规模: {cols} 列(A~{col_name(cols - 1)}) x {rows} 行(1~{rows})")
    print("-" * 56)
    print("下一步: 打开网格图找到目标格子编号，然后运行:")
    print(f"        python runner/grid.py <编号>   例如: python runner/grid.py A3")
    print("=" * 56)
    return 0


def _query(ref: str, cell_size_override: int | None = None) -> int:
    """将格子编号换算为原图中心像素坐标并输出 adb 点击命令。"""
    img = cv2.imread(SOURCE_IMAGE)
    if img is None:
        print(f"[ERROR] 未找到可读原图: {SOURCE_IMAGE}")
        print("        请先运行: python runner/capture.py <device_id> 截图")
        return 1

    # 格子尺寸：显式参数 > sidecar 元数据 > 文件默认值
    cell_size = cell_size_override
    if cell_size is None and os.path.isfile(GRID_META):
        try:
            with open(GRID_META, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if (meta.get("image_width"), meta.get("image_height")) == (img.shape[1], img.shape[0]):
                cell_size = int(meta["cell_size"])
            else:
                print("[WARN] 网格元数据与当前原图尺寸不一致（截图可能已更新），"
                      f"请先重新生成网格图，或查询时显式指定格子尺寸: python runner/grid.py {ref} <尺寸>")
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"[WARN] 网格元数据读取失败({exc})，使用默认格子尺寸 {CELL_SIZE}")
    if cell_size is None:
        cell_size = CELL_SIZE

    try:
        col, row = parse_ref(ref)
        marker = GridMarker(cell_size=cell_size)
        x, y = marker.ref_to_point(ref, img.shape)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 1

    print("=" * 56)
    print(f"格子 {ref.upper()} (列 {col_name(col)}, 行 {row + 1:g})")
    print(f"格子边长: {cell_size}px | 原图: {img.shape[1]}x{img.shape[0]}")
    print(f"像素坐标: ({x}, {y})")
    print(f"adb 点击命令:   {_adb_prefix()} shell input tap {x} {y}")
    print("=" * 56)
    tapped = _execute_tap(x, y)
    return 0 if tapped or not _read_device_id() else 1


def main() -> int:
    """
    命令行入口。

    参数形态:
        无参               -> 用默认 CELL_SIZE 生成网格图
        <数字>             -> 用指定格子边长生成网格图（80/60/40 或任意 >=20 的值）
        <编号>             -> 查询格子坐标，如 A3
        <编号> <数字>      -> 按指定格子边长查询坐标（不依赖已生成的网格图）
    """
    args = sys.argv[1:]

    # 形态1：python runner/grid.py 80
    if len(args) == 1 and args[0].isdigit():
        return _generate(int(args[0]))

    # 形态2：python runner/grid.py A3 [40]
    if len(args) >= 1:
        ref = args[0]
        if not ref[0].isalpha():
            print(f"[ERROR] 无法识别的参数: {ref!r}")
            print("用法: python runner/grid.py [格子边长 | 格子编号 [格子边长]]")
            print("示例: python runner/grid.py        (生成网格图)")
            print("      python runner/grid.py 40     (生成 40px 细网格)")
            print("      python runner/grid.py A3     (查询 A3 坐标)")
            return 1
        override = None
        if len(args) >= 2:
            if not args[1].isdigit():
                print(f"[ERROR] 格子边长必须是数字: {args[1]!r}")
                return 1
            override = int(args[1])
        return _query(ref, override)

    # 形态0：无参 -> 生成
    return _generate(CELL_SIZE)


if __name__ == "__main__":
    sys.exit(main())
