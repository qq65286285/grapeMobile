"""
runner/main.py - 定位运行入口
============================
职责:
    1. 读取 runner/ 目录下的两张图片
       - template.png: 小图(模板/图标),即待定位的目标
       - screenshot.png: 大图(App 截图),即在其中搜索
    2. 调用 src/service.py 的 LocateService 完成定位
    3. 将坐标输出到控制台,可直接用于 adb 命令点击定位

运行方式:
    cd e:/GrapeMobile
    python runner/main.py

注: template.png / screenshot.png 需由用户手动放入 runner/tmp/image/ 目录;
    脚本启动时会检查文件存在性,缺失时给出放置提示后退出。
"""

from __future__ import annotations

import os
import sys

# 将 src/ 加入 sys.path,使 service 模块可被导入(与 examples/basic_usage.py 一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from service import LocateService  # noqa: E402  (sys.path 调整后才能导入)

# 两张图片的固定路径(放在 runner/ 目录下)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_TEMPLATE = os.path.join(_BASE_DIR, "tmp", "image", "template.png")  # 小图/模板
IMAGE_SOURCE = os.path.join(_BASE_DIR, "tmp", "image", "screenshot.png")   # 大图/截图
DEVICE_ID_FILE = os.path.join(_BASE_DIR, "device_id.txt")  # capture.py 记录的设备 id


def _adb_prefix() -> str:
    """
    读取 capture.py 截图时记录的 device_id,返回带 -s 的 adb 命令前缀。
    无记录文件时退回不带 -s 的 "adb"。
    """
    if os.path.isfile(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
            device_id = f.read().strip()
        if device_id:
            return f"adb -s {device_id}"
    return "adb"


def _check_images_exist() -> bool:
    """检查两张图片是否都已放置,缺失时打印提示。"""
    missing = []
    if not os.path.isfile(IMAGE_TEMPLATE):
        missing.append(("template.png(小图/模板)", IMAGE_TEMPLATE))
    if not os.path.isfile(IMAGE_SOURCE):
        missing.append(("screenshot.png(大图/截图)", IMAGE_SOURCE))
    if missing:
        print("[ERROR] 缺少图片文件,请将以下图片放入 runner/ 目录:")
        for name, path in missing:
            print(f"  - {name}: {path}")
        return False
    return True


def main() -> int:
    """
    主入口:定位 image1 在 image2 中的坐标并输出到控制台。

    Returns:
        0: 成功定位并输出坐标;
        1: 缺少图片文件;
        2: 未找到目标(置信度不足或目标不在大图中)。
    """
    if not _check_images_exist():
        return 1

    # service 内部默认走 strategy="auto" 责任链,自动从快到慢降级
    service = LocateService(strategy="auto", threshold=0.8)
    coord = service.locate(IMAGE_TEMPLATE, IMAGE_SOURCE)

    if coord is None:
        print("[FAIL] 未在 screenshot.png 中找到 template.png")
        return 2

    x, y = coord
    print("=" * 50)
    print(f"定位成功: 坐标 = ({x}, {y})")
    print(f"adb 点击命令: {_adb_prefix()} shell input tap {x} {y}")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
