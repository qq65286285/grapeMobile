"""
runner/capture.py - adb 截图执行入口
====================================
职责:用 device_id 连接设备 + 截图到固定位置 runner/tmp/image/screenshot.png(对接 main.py 的大图)。

本文件是"直接执行入口":内置 DEVICE_ID 变量,改完直接运行,无需命令行参数。
真正的执行类在 src/executor.py(AdbExecutor),本文件只做"配置 + 调用 + 输出"。

用法(两种方式任选):
    1. 修改下方 DEVICE_ID 为你的设备地址,直接运行:
           python runner/capture.py
    2. 命令行传参(覆盖 DEVICE_ID):
           python runner/capture.py <device_id>
       例如:
           python runner/capture.py emulator-5554
           python runner/capture.py 192.168.1.100:5555

流程位置(完整链路):
    python runner/capture.py   # 1. 连接 + 截图 + 生成网格图 -> tmp/image/grid_screenshot.png
    python runner/grid.py A3   # 2. 打开网格图报格子编号 -> 输出坐标 + adb 命令
"""

from __future__ import annotations

import json
import os
import sys

import cv2

# 将 src/ 加入 sys.path,使 executor / adbtools / grid 可被导入(与 runner/main.py 一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from executor import AdbExecutor  # noqa: E402  (sys.path 调整后才能导入)
from adbtools import AdbError    # noqa: E402
from grid import GridMarker, col_name  # noqa: E402

# ============================================================
# 运行配置:直接修改下方变量,然后执行本文件即可
#   DEVICE_ID: 设备地址
#     - 网络调试:形如 "ip:port",例如 "192.168.1.100:5555"
#     - USB 直连:填设备序列号(adb devices 查到的那串)
#     - 模拟器:  形如 "emulator-5554"
#   CELL_SIZE: 网格格子边长(像素) 60=中  30=细(默认)  20=超细
# ============================================================
DEVICE_ID = "emulator-5554"
CELL_SIZE = 30


def main() -> int:
    """
    直接执行入口:完成"连接设备 + 截图到固定位置"。

    设备 id 取值优先级:命令行参数 > 文件顶部 DEVICE_ID 变量。
    设备形态自动识别:含 ":" 走 adb connect(网络设备);
    否则走挂载校验(模拟器 / USB,无需网络连接)。

    Returns:
        0: 成功完成连接与截图;
        1: adb 执行失败(设备未连接/离线/超时/命令报错等)。
    """
    device_id = sys.argv[1] if len(sys.argv) >= 2 else DEVICE_ID
    executor = AdbExecutor(device_id)
    try:
        path = executor.run()
    except AdbError as exc:
        print(f"[ERROR] adb 执行失败: {exc}")
        return 1

    # 记录本次截图所用设备 id,供 grid.py / main.py 输出完整 adb 命令(带 -s)时读取
    runner_dir = os.path.dirname(os.path.abspath(__file__))
    device_file = os.path.join(runner_dir, "device_id.txt")
    with open(device_file, "w", encoding="utf-8") as f:
        f.write(device_id)

    # 截图后直接生成网格标注图 + sidecar 元数据(供 grid.py 查询使用)
    img = cv2.imread(path)
    if img is None:
        print(f"[WARN] 截图读取失败,跳过网格图生成: {path}")
        return 1
    marker = GridMarker(cell_size=CELL_SIZE)
    image_dir = os.path.join(runner_dir, "tmp", "image")
    os.makedirs(image_dir, exist_ok=True)
    grid_image = os.path.join(image_dir, "grid_screenshot.png")
    cv2.imwrite(grid_image, marker.draw(img))
    cols, rows = marker.grid_dims(img.shape)
    with open(os.path.join(image_dir, "grid_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"cell_size": CELL_SIZE,
                   "image_width": int(img.shape[1]),
                   "image_height": int(img.shape[0]),
                   "cols": cols, "rows": rows}, f, ensure_ascii=False, indent=2)

    print("=" * 56)
    print("连接成功 + 截图完成 + 网格图已生成")
    print(f"设备: {device_id}")
    print(f"原图: {path} ({img.shape[1]}x{img.shape[0]})")
    print(f"网格图: {grid_image}")
    print(f"网格规模: {cols} 列(A~{col_name(cols - 1)}) x {rows} 行(1~{rows})  格子: {CELL_SIZE}px")
    print("-" * 56)
    print("下一步: 打开网格图找到目标格子编号,然后运行:")
    print("        python runner/grid.py <编号>   例如: python runner/grid.py A3")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
