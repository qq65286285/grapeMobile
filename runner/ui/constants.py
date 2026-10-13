"""
runner/ui/constants.py - 路径与布局常量(自 gui_steps.py 拆出,CR-08)
==========================================================================
脚本统一存放在 runner/scripts/,每个脚本一个同名文件夹,JSON 与图片分置:
    runner/scripts/进入游戏/进入游戏.json
    runner/scripts/进入游戏/images/0001.png
未保存的新脚本先录制到 scripts/untitled/images/,"保存"时随脚本名整体迁移。

执行模式结果根目录:每次执行勾选用例生成一个测试计划子目录
result/<plan-id>/,内含 plan.json(计划汇总)+ 每条用例一个子目录。
"""

from __future__ import annotations

import os

# ui/ 包位于 runner/ui/,常量基准目录回退一级到 runner/
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IMAGE_DIR = os.path.join(_BASE_DIR, "tmp", "image")
GRID_IMAGE = os.path.join(_IMAGE_DIR, "grid_screenshot.png")
GRID_META = os.path.join(_IMAGE_DIR, "grid_meta.json")
RAW_IMAGE = os.path.join(_IMAGE_DIR, "screenshot.png")
DEVICE_ID_FILE = os.path.join(_BASE_DIR, "device_id.txt")
SCRIPTS_DIR = os.path.join(_BASE_DIR, "scripts")
RESULT_DIR = os.path.join(_BASE_DIR, "result")
# 新建脚本未保存前使用的临时脚本名(锚点先存到 scripts/untitled/images/,保存时迁移)
DRAFT_SCRIPT_NAME = "untitled"

# 实时截图生成网格时的默认格子边长(像素):从 config.yaml 的 grid.cell_size 读取
# (单一来源,与 AI 定位管线一致);读取失败回退 30
try:
    from locate.config import load_config as _load_cell_config
    DEFAULT_CELL_SIZE = _load_cell_config().grid.cell_size
except Exception:
    DEFAULT_CELL_SIZE = 30

MARGIN = 40  # 与 GridMarker 默认边距一致(网格图在原图四周外扩 40px)
MAX_DISPLAY_WIDTH = 288   # 设备画面(左栏,宽320)网格图显示区域最大宽度(像素)
MAX_DISPLAY_HEIGHT = 480  # 最大高度(像素),竖屏截图按三栏600高缩放适配
# 右侧"预期画面"缩略图尺寸(锚点是全屏截图,before/after 各一张)
EXPECT_IMG_W = 168
EXPECT_IMG_H = 150
