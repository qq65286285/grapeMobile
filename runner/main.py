"""
runner/main.py - AI 定位运行入口
============================================
职责:
    1. 连接设备,实时截图
    2. 生成网格标注图(列 A/B/C..., 行 1/2/3...)
    3. 调用 AI(agnes-3.0-flash vision) 分析网格图,识别所有可点击元素
    4. 输出元素列表,标注各自所在格子编号 + 原图像素坐标 + adb 命令

运行方式:
    cd e:/GrapeMobile
    python runner/main.py

前提: runner/device_id.txt 存在(python runner/capture.py 已跑过一次)。
旧版 imgloc 模板匹配方案备份于 runner/main_old.py。
"""

from __future__ import annotations

import json
import os
import re
import sys

# 将 src/ 加入 sys.path
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

import cv2  # noqa: E402
from aiclient import AIClient, AIError  # noqa: E402
from adbtools import AdbClient, AdbError  # noqa: E402
from grid import GridMarker, col_name  # noqa: E402

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_IMAGE_DIR = os.path.join(_BASE_DIR, "tmp", "image")
SCREENSHOT_PATH = os.path.join(_IMAGE_DIR, "screenshot.png")
GRID_IMAGE_PATH = os.path.join(_IMAGE_DIR, "grid_screenshot.png")
GRID_META_PATH = os.path.join(_IMAGE_DIR, "grid_meta.json")
DEVICE_ID_FILE = os.path.join(_BASE_DIR, "device_id.txt")
CELL_SIZE = 30  # 网格格子边长(与 capture.py 一致)

_SYSTEM_PROMPT = (
    "你是一个 Android 自动化助手。用户会给你一张带网格标注的设备截图。"
    "网格图中每列顶部/底部有黄色字母标签(A、B、C...Z、AA、AB...),"
    "每行左侧/右侧有黄色数字标签(1、2、3...)。"
    "坐标原点为图片左上角,x 轴向右,y 轴向下。"
    "你必须且只能以 JSON 格式回复,不要输出任何其他内容。"
)


def _read_device_id() -> str:
    """从 device_id.txt 读取设备 ID。"""
    if not os.path.isfile(DEVICE_ID_FILE):
        return ""
    with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def _adb_prefix(device_id: str) -> str:
    """返回带 -s 的 adb 命令前缀。"""
    return f"adb -s {device_id}" if device_id else "adb"


def _capture_and_grid(device_id: str) -> tuple[str, str, int, int, GridMarker] | None:
    """
    连接设备截图 → 生成网格标注图。
    返回 (截图路径, 网格图路径, 原始宽, 原始高, GridMarker),失败返回 None。
    """
    try:
        client = AdbClient()
        client.attach(device_id)
    except AdbError as exc:
        print(f"[FAIL] 设备连接失败: {exc}")
        return None

    try:
        path = client.screenshot(save_path=SCREENSHOT_PATH)
    except AdbError as exc:
        print(f"[FAIL] 截图失败: {exc}")
        return None

    img = cv2.imread(path)
    if img is None:
        print(f"[FAIL] 截图读取失败: {path}")
        return None

    h, w = img.shape[:2]
    print(f"[截图] {path}  分辨率: {w}x{h}")

    # 生成网格标注图
    marker = GridMarker(cell_size=CELL_SIZE)
    grid_img = marker.draw(img)
    cv2.imwrite(GRID_IMAGE_PATH, grid_img)
    cols, rows = marker.grid_dims(img.shape)
    with open(GRID_META_PATH, "w", encoding="utf-8") as f:
        json.dump({"cell_size": CELL_SIZE,
                   "image_width": w, "image_height": h,
                   "cols": cols, "rows": rows}, f, ensure_ascii=False, indent=2)

    print(f"[网格] {GRID_IMAGE_PATH}  {cols}列(A~{col_name(cols-1)}) x {rows}行(1~{rows})")
    return path, GRID_IMAGE_PATH, w, h, marker


def _parse_json_list(reply: str) -> list | None:
    """从 AI 回复中提取 JSON 数组。"""
    m = re.search(r"\[.*\]", reply, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def main() -> int:
    """
    主入口:截图 → 网格 → AI 分析 → 输出可点击元素列表(格子编号 + 像素坐标 + adb 命令)。

    Returns:
        0: 成功;
        1: 设备/截图问题;
        2: AI 调用或解析失败。
    """
    device_id = _read_device_id()
    if not device_id:
        print("[ERROR] 无设备记录(device_id.txt),请先运行 python runner/capture.py")
        return 1

    # ---- 1. 截图 + 生成网格 ----
    print(f"[设备] {device_id} 正在截图...")
    result = _capture_and_grid(device_id)
    if result is None:
        return 1
    screenshot_path, grid_path, orig_w, orig_h, marker = result
    img_shape = (orig_h, orig_w, 3)

    # ---- 2. AI 分析网格图 ----
    ai = AIClient()
    prompt = (
        "这是一张带网格标注的设备截图。"
        "请识别截图中所有可点击的按钮/图标区域,"
        "返回每个元素所在的格子编号(如 'C5')和简短描述(不超过10字)。"
        '格式: [{"cell": "C5", "desc": "登录按钮"}, ...]'
        "只返回 JSON 数组,不要输出其他文字。"
    )
    print("[AI] 正在分析网格图,识别可点击元素...")
    try:
        reply = ai.chat_with_image(
            prompt, grid_path,
            system=_SYSTEM_PROMPT, temperature=0.1,
        )
    except AIError as exc:
        print(f"[FAIL] AI 调用失败: {exc}")
        return 2

    # ---- 3. 解析 + 格子转像素坐标 ----
    items = _parse_json_list(reply)
    if not items:
        print(f"[FAIL] 无法解析 AI 返回的按钮列表")
        print(f"[AI 回复] {reply}")
        return 2

    adb = _adb_prefix(device_id)
    print("=" * 60)
    print(f"AI 识别到 {len(items)} 个可点击元素 "
          f"(原始分辨率 {orig_w}x{orig_h}):")
    print("-" * 60)
    for i, item in enumerate(items, 1):
        cell = str(item.get("cell", "")).strip().upper()
        desc = item.get("desc", "")
        if not cell:
            print(f"  {i:2d}. [{desc}]  (格子编号缺失,跳过)")
            continue
        try:
            x, y = marker.ref_to_point(cell, img_shape)
        except ValueError as exc:
            print(f"  {i:2d}. [{desc}]  格子 {cell} 无效: {exc}")
            continue
        print(f"  {i:2d}. [{desc}]  格子 {cell}  坐标 ({x}, {y})")
        print(f"      {adb} shell input tap {x} {y}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
