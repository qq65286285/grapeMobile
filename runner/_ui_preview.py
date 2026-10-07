"""UI 离屏预览器(仅开发用,不参与正式链路)。

用途:跳过设备选择/截图闸门,用本地已有的 grid_screenshot.png + 假步骤/假 AI 消息,
把真实 Tk 窗口渲染出来并截图,便于在没有设备的环境里核对界面对齐情况。

用法:
    D:/Python/Python311/python.exe runner/_ui_preview.py [输出png路径] [场景]
场景: normal(默认,普通模式+步骤) | ai(AI 模式气泡对话) | exec(执行模式)
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, os.path.join(_HERE, "..", "_deps"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageGrab  # noqa: E402

import gui_steps as G  # noqa: E402


def _patch_startup() -> None:
    """绕过设备闸门:直接构造界面所需的状态,不连 adb。"""

    def fake_init(self) -> None:
        import tkinter as tk
        tk.Tk.__init__(self)
        self.title("GrapeMobile 步骤编排器")
        self.resizable(True, True)
        self._apply_dark_theme()
        self._bridge = G._UiBridge(self)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.steps = []
        self._running = False
        self.device_id = "emulator-5554"
        self._latest_raw = None
        self.script_name = G.DRAFT_SCRIPT_NAME
        self.script_path = None
        self._before_seq = 0
        self._after_seq = 0
        self._anchor_client = None
        self._ai_result = None
        self._ai_cand_idx = 0
        self._ai_busy = False
        self._ai_overlay_active = False
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._ai_instruction = ""
        self._startup_ok = True
        self._auto_busy = False
        self._auto_fail_logged = False

        # 用现成网格图当"设备画面"
        grid = cv2.imread(G.GRID_IMAGE)
        if grid is None:
            raise SystemExit(f"缺少截图产物: {G.GRID_IMAGE}(先跑 capture.py)")
        self._set_grid(grid, 30)
        self._build_ui()
        self._load_preview_steps()
        # Tk 会话已建立,再显示窗口(PhotoImage 需在 Tcl 解释器活着时创建)
        self.deiconify()
        self.update_idletasks()
        self.update()

    G.StepApp.__init__ = fake_init
    G.StepApp._load_preview_steps = _load_preview_steps


def _load_preview_steps(self) -> None:
    """注入三条形态各异的假步骤(点击/带 AI 坐标/系统操作)。"""
    self.steps = [
        {"type": "tap", "cell": "B14", "x": 412, "y": 860,
         "delay_before": 0, "wait_after": 2,
         "before_image": "", "after_image": "",
         "desc": "点击 kof 图标"},
        {"type": "tap", "cell": "E9", "x": 508, "y": 470,
         "delay_before": 1, "wait_after": 3,
         "before_image": "", "after_image": "",
         "desc": "点击 邮箱登录"},
        {"type": "app_start", "package": "com.tencent.tmgp.sgame",
         "delay_before": 0, "wait_after": 5,
         "before_image": "", "after_image": "", "desc": ""},
    ]
    self._refresh_list()
    self._select_and_preview(0)


def _shoot(out_png: str, tab: int, fill_ai: bool) -> None:
    app = G.StepApp()
    app.notebook.select(tab)
    if fill_ai:
        app._ai_chat_append("系统", "输入想做的操作(如「点击 kof 图标」),\n"
                                     "定位后确认即可存入脚本步骤。")
        app._ai_chat_append("用户", "点击 kof 图标")
        app._ai_chat_append("AI", "已定位候选,请在左侧画面确认十字位置(可拖动微调)。")
        app._ai_cand_info_var.set(
            "候选 1 / 3              置信度 0.91\n"
            "中心 (412, 860) · 格子 B14\n"
            "来源: ocr+vlm           ✓验证通过")
        app._show_cand_card(True)
        app._set_cand_btns(True, True, True)
        app._ai_log("[trace] POST /v1/chat -> 200 (1.2s)")
        app._ai_log("[locate] 候选已找到命中 -> 海量 OCR")
        app._ai_log("[locate] OCR 命中 32 对候选,置信度 0.91")
        app._ai_log("[locate] 候选选择 B14 (412,860) 已绘制")
    app.update_idletasks()
    app.update()
    time.sleep(0.6)
    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    img.save(out_png)
    print(f"[preview] {out_png}  {w}x{h}")
    app.destroy()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_HERE, "tmp", "ui_preview.png")
    scene = sys.argv[2] if len(sys.argv) > 2 else "normal"
    _patch_startup()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if scene == "ai":
        _shoot(out, 1, True)
    elif scene == "exec":
        _shoot(out, 2, False)
    else:
        _shoot(out, 0, False)
