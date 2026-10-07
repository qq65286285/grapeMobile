"""
runner/gui_steps.py - 步骤编排可视化界面(Tkinter)
==================================================
左侧显示网格图(启动时自动连接设备实时截图生成;"⟳ 刷新截图"可随时重截;
设备不可用时回退到静态文件 grid_screenshot.png):
    - 鼠标左键点击任意格子 -> 自动追加"点击该格子"步骤(无需手输编号),
      同时把"点击前的原始画面"存为该脚本的画面锚点
      (scripts/<脚本名>/NNNN.png,列表中以 🎯 标记)
右侧编排步骤:
    - 每步支持"执行前等待/执行后等待"秒数(点图时取输入框当前值,
      选中步骤可修改后点"应用到选中步骤")
    - 支持 删除选中 / 上移 / 下移 / 清空 / 保存脚本 / 加载脚本
    - "运行":按顺序执行;点完一步后若下一步有画面锚点,会轮询设备截图与锚点
      比对(全屏相似度),画面一致立即继续,wait_after 作为超时上限;
      无锚点则固定等待 wait_after 秒。底部日志实时显示进度与相似度

脚本统一存放在 runner/scripts/,每个脚本一个同名文件夹,JSON 与图片分置:
    runner/scripts/进入游戏/进入游戏.json
    runner/scripts/进入游戏/images/0001.png
未保存的新脚本先录制到 scripts/untitled/images/,"保存"时随脚本名整体迁移。

启动时自动执行 adb devices 弹出设备选择框:
    - 必须选择一台"在线"设备才能进入主界面(空列表/离线/未授权均不可进入),
      弹框内置"⟳ 刷新"按钮,可在连接设备/授权后重新检测;
    - 选中的设备 id 会写入 runner/device_id.txt(与 capture.py 产物布局一致,
      StepRunner / grid.py / main.py 可直接复用),无需先手动运行 capture.py。
启动:python runner/gui_steps.py
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk

# 将 src/ 加入 sys.path(与其他 runner 脚本一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)
# 项目依赖目录(rapidfuzz/cv2/PIL 等)加入 sys.path:
# 不设 PYTHONPATH 直接启动 GUI 时,locate 管线也能找到第三方包
_DEPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_deps")
if os.path.isdir(_DEPS_DIR):
    sys.path.insert(0, os.path.abspath(_DEPS_DIR))

import cvio  # noqa: E402  Unicode 路径兼容的图像读写(中文脚本名目录)
import aiclient  # noqa: E402  AI 调用客户端(trace 钩子用于展示 AI 思考过程)
from grid import GridMarker, col_name  # noqa: E402
from steps import (  # noqa: E402
    APP_OP_TYPES, KEYEVENT_LABELS, KEYEVENT_MAP,
    StepError, StepRunner, load_steps, save_steps,
)
from adbtools import AdbClient, AdbError  # noqa: E402

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_IMAGE_DIR = os.path.join(_BASE_DIR, "tmp", "image")
GRID_IMAGE = os.path.join(_IMAGE_DIR, "grid_screenshot.png")
GRID_META = os.path.join(_IMAGE_DIR, "grid_meta.json")
RAW_IMAGE = os.path.join(_IMAGE_DIR, "screenshot.png")
DEVICE_ID_FILE = os.path.join(_BASE_DIR, "device_id.txt")
# 脚本统一存放目录:每个脚本一个同名文件夹,JSON 与 images/ 在其内,如
#   runner/scripts/steps-1/steps-1.json
#   runner/scripts/steps-1/images/0001.png   (录制时保存的点击前画面)
SCRIPTS_DIR = os.path.join(_BASE_DIR, "scripts")
# 执行模式结果根目录:每次执行勾选用例生成一个测试计划子目录 result/<plan-id>/,
# 内含 plan.json(计划汇总)+ 每条用例一个子目录(recording.json / frames/ / replay.mp4)
RESULT_DIR = os.path.join(_BASE_DIR, "result")
# 新建脚本未保存前使用的临时脚本名(锚点先存到 scripts/untitled/images/,保存时迁移)
DRAFT_SCRIPT_NAME = "untitled"
DEFAULT_CELL_SIZE = 30  # 实时截图生成网格时的默认格子边长(像素)
MARGIN = 40  # 与 GridMarker 默认边距一致(网格图在原图四周外扩 40px)
MAX_DISPLAY_WIDTH = 288   # 设备画面(左栏,宽320)网格图显示区域最大宽度(像素)
MAX_DISPLAY_HEIGHT = 480  # 最大高度(像素),竖屏截图按三栏600高缩放适配
# 右侧"预期画面"缩略图尺寸(锚点是全屏截图,before/after 各一张)
EXPECT_IMG_W = 168
EXPECT_IMG_H = 150

# ---------------------------------------------------------------------------
# 暗色主题(克制版:单一葡萄紫强调色,无霓虹/渐变)
# 视觉定调参照 IDE / 开发者工具暗色(VS Code、GitHub Dark、DevTools),
# 长时间盯截图与日志不刺眼;状态色用中性工程色,图标保持线性/文字。
# ---------------------------------------------------------------------------
THEME = {
    "bg":        "#0f1115",  # 主背景
    "bg2":       "#161922",  # 次背景(顶栏/右栏/卡片底,对应 v3 --bg2)
    "surface":   "#1a1d24",  # 表面 / 面板
    "surface2":  "#21262d",  # 输入框 / 次级表面
    "border":    "#262b33",  # 弱边框
    "border2":   "#323842",  # 较强边框 / 描边
    "txt":       "#e6e9ef",  # 主文字
    "txt2":      "#9aa3b0",  # 次要文字
    "txt3":      "#6b7280",  # 最弱文字(区块小标题/占位说明)
    "accent":    "#7c6cd9",  # 葡萄紫强调色(唯一强调色)
    "accent_bg": "#272242",  # 强调底色(按钮 hover / 选中态)
    "accent_soft": "#322c52",  # 强调软底色(选中卡片底,对应 v3 accent-soft)
    "green":     "#3fb950",  # 在线 / 成功
    "red":       "#f85149",  # 错误 / 离线
    "term_bg":   "#0b0d10",  # 终端日志底
    "term_fg":   "#8b9586",  # 终端日志字
    "row_alt":   "#161920",  # 列表斑马纹(偶数行)
    "chat_ai":   "#8ab4f8",  # AI 对话消息(AI 侧)
    "chat_user": "#7dcf8a",  # AI 对话消息(用户侧)
    "bubble_ai":     "#2b303b",  # AI 气泡底(需与 bg 明显区分,否则截图上看不见)
    "bubble_user":   "#4a3f7d",  # 用户气泡底(紫调)
    "bubble_border": "#3d4450",  # 气泡描边
    "bubble_sys_bg": "#1a1d24",  # 系统提示条底
}

# 字体系统:UI 用微软雅黑(中文渲染干净),坐标/日志用 Consolas 等宽
# 字号对齐设计稿:正文 11pt / 小标题与列表 10pt(Windows 下 pt 偏小,比 CSS px 视觉更大)
FONT_UI = ("Microsoft YaHei UI", 11)
FONT_UI_BOLD = ("Microsoft YaHei UI", 11, "bold")
FONT_BTN = ("Microsoft YaHei UI", 10)          # 按钮/输入框(≈设计稿 13px)
FONT_BTN_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_CAPTION = ("Microsoft YaHei UI", 10)         # 区块小标题(次要色)
FONT_LIST = ("Microsoft YaHei UI", 10)            # 步骤列表行
FONT_MONO = ("Consolas", 10)                       # 日志
FONT_MONO_CHAT = ("Consolas", 10)                 # AI 对话
FONT_BUBBLE = ("Microsoft YaHei UI", 11)          # 对话气泡正文
FONT_BUBBLE_SM = ("Microsoft YaHei UI", 10)        # 系统提示/辅助说明
# Emoji 设备/图标字形(按 delegated 任务要求:设备卡与顶栏用 emoji/Unicode 字形)
FONT_EMOJI = ("Segoe UI Emoji", 13)
FONT_APPBAR_ICO = ("Segoe UI", 12)                  # 顶栏 Unicode 图标(⟳ 🌓)


class _ChatPane:
    """AI 对话气泡面板:AI 左灰底、用户右紫底、系统提示居中浅色。

    用 Canvas + 内层 Frame 模拟可滚动消息流(每条消息一个 Label),
    取代纯文本 Text 追加,呈现对齐设计稿的气泡式对话。
    """

    def __init__(self, parent: tk.Misc, width: int = 420,
                 height: int = 260) -> None:
        # 外层描边框:让对话区在深色底上明确成一个"面板"(对齐设计稿)
        self.frame = tk.Frame(parent, bg=THEME["border2"], bd=0)
        # 用 pack 而非 grid_propagate(False):frame 尺寸由 canvas 请求,
        # 气泡才能随内容增高(曾因 grid_propagate(False) 导致 frame 塌成 1x1)
        self.canvas = tk.Canvas(self.frame, bg=THEME["surface"], highlightthickness=1,
                                highlightbackground=THEME["border"],
                                highlightcolor=THEME["border"],
                                width=width, height=height, bd=0)
        self.scroll = ttk.Scrollbar(self.frame, orient="vertical",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.inner = tk.Frame(self.canvas, bg=THEME["surface"])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(
                            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        # 滚轮只在指针进入对话区时全局接管,离开即归还:避免与中栏步骤卡片
        # 同时滚动(此前用 bind_all 常驻,鼠标在任意位置都会带动对话区)
        self.canvas.bind("<Enter>",
                         lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.frame.bind("<Leave>",
                        lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_canvas_resize(self, event: tk.Event) -> None:
        """内层 Frame 宽度跟随画布,气泡才能贴边换行。"""
        self.canvas.itemconfigure(self._win, width=max(1, event.width - 2))

    def _on_wheel(self, event: tk.Event) -> None:
        if not self._scrollable():
            return
        first, last = self.canvas.yview()
        if event.delta < 0 and first <= 0.001:
            return
        if event.delta > 0 and last >= 0.999:
            return
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _scrollable(self) -> bool:
        bb = self.canvas.bbox("all")
        if not bb:
            return False
        return bb[3] > self.canvas.winfo_height()

    def _wrap(self, event: tk.Event) -> int:
        return max(200, event.width - 24)

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()

    def add_ai(self, text: str) -> None:
        self._bubble(text, bg=THEME["bubble_ai"], fg=THEME["txt"],
                     anchor="w", wrap_width=360)

    def add_user(self, text: str) -> None:
        self._bubble(text, bg=THEME["bubble_user"], fg="#ffffff",
                     anchor="e", wrap_width=320)

    def add_sys(self, text: str) -> None:
        """系统说明:居中浅色小字,用于欢迎语/操作提示。"""
        lbl = tk.Label(self.inner, text=text, bg=THEME["surface"], fg=THEME["txt2"],
                       font=FONT_BUBBLE_SM, justify="center", wraplength=340)
        lbl.pack(pady=(10, 14), padx=14, anchor="center")

    def _bubble(self, text: str, bg: str, fg: str, anchor: str,
                wrap_width: int) -> None:
        # 高 1px 的 relief 描边:让气泡在深色底上有清晰边界(纯色块在截图里会"消失")
        lbl = tk.Label(self.inner, text=text, bg=bg, fg=fg, font=FONT_BUBBLE,
                       justify="left", wraplength=wrap_width,
                       padx=12, pady=8, bd=1, relief="solid",
                       highlightthickness=0)
        lbl.configure(highlightbackground=THEME["bubble_border"],
                      highlightcolor=THEME["bubble_border"])
        holder = tk.Frame(self.inner, bg=THEME["surface"])
        lbl.pack(in_=holder, padx=10, pady=(0, 10), anchor=anchor)
        holder.pack(fill="x", anchor=anchor)
        self._to_bottom()

    def _to_bottom(self) -> None:
        self.frame.update_idletasks()
        if self._scrollable():
            self.canvas.yview_moveto(1.0)


class _UiBridge:
    """
    后台线程 -> 主线程的安全投递桥梁。

    背景:直接在后台线程里调用 widget.after()/任何 Tk 方法,
    一旦此刻主循环未运行(窗口关闭流程中 Tcl 解释器已销毁),
    _tkinter 会抛出 RuntimeError: main thread is not in main loop,
    且该跨线程 Tcl 调用可能卡住解释器退出,表现为"程序关不掉"。

    规则:
        - 后台线程只调用 post(fn),把可调用对象放进 queue(不触碰 Tk);
        - 主线程通过 after 定时器轮询队列并执行回调,所有 Tk 操作都在主线程;
        - close() 后停止轮询并丢弃后续回调,关闭路径完全闭环。
    """

    def __init__(self, widget: tk.Misc, interval_ms: int = 60) -> None:
        self._widget = widget
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._closed = False
        self._interval = interval_ms
        # 轮询定时器本身只在主线程创建/续期
        widget.after(interval_ms, self._pump)

    def post(self, fn) -> None:
        """后台线程调用:把回调投递到主线程执行。关闭后直接丢弃。"""
        if not self._closed:
            self._q.put(fn)

    def _pump(self) -> None:
        if self._closed:
            return
        try:
            while True:
                cb = self._q.get_nowait()
                try:
                    cb()
                except tk.TclError:
                    # 回调执行期间对应组件恰好被销毁,忽略即可
                    pass
        except queue.Empty:
            pass
        if self._closed:
            return
        try:
            self._widget.after(self._interval, self._pump)
        except (RuntimeError, tk.TclError):
            # 窗口/解释器已销毁,停止轮询
            self._closed = True

    def close(self) -> None:
        self._closed = True


class _AiLogHandler(logging.Handler):
    """把 locate 管线的运行日志(意图解析/OCR/VLM/融合/验证)转发到 AI 日志区。"""

    def __init__(self, app: "StepApp") -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        app = self._app
        app._bridge.post(lambda m=msg: app._ai_log(m))


class DeviceSelectDialog(tk.Toplevel):
    """
    启动时的模态设备选择框:枚举 adb devices,用户必须选中一台在线设备才能进入。

    交互规则:
        - 打开即自动检测一次;"⟳ 刷新"在后台线程重跑 adb devices(不卡 UI);
        - state == "device"(在线)的行可选,双击等价"确定";
        - offline/unauthorized 等状态灰显且不可选(但仍展示,避免用户误以为没检测到);
        - 列表为空 / adb 不可用:无法确定,停留在本框靠刷新重试;
        - 关闭窗口或点"取消":selected=None,主程序直接退出(不允许进入主界面)。

    用法:
        >>> dlg = DeviceSelectDialog(root)
        >>> root.wait_window(dlg)
        >>> device_id = dlg.selected   # None 表示用户放弃
    """

    # adb 设备状态 -> 中文展示
    STATE_LABELS = {
        "device": "在线",
        "offline": "离线",
        "unauthorized": "未授权(请在手机上点\"允许 USB 调试\")",
        "recovery": "恢复模式",
        "no device": "无设备",
    }

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.title("选择设备")
        self.resizable(False, False)
        self.configure(background=THEME["bg"])
        self.selected: Optional[str] = None
        self._busy = False
        # v3 卡片列表状态:最近一次枚举结果 + 当前点选的在线序列号
        self._devices: List[tuple] = []
        self._sel_serial: Optional[str] = None
        # 后台线程经 bridge 回主线程;对话框销毁时 close,杜绝跨线程 Tk 调用
        self._bridge = _UiBridge(self)
        self._build_ui()
        # 注意:不能调用 self.transient(parent) —— 主窗口(parent)此刻处于 withdrawn
        # 状态,Windows 下 transient 子窗口会被连带强制 withdrawn,表现为窗口完全
        # 不显示、进程却在跑。不设 transient 时对话框独立映射,并在任务栏有入口。
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # 尺寸自适应内容:曾硬编码 560x330,暗色主题下 Treeview 行高变大后
        # 内容超出固定高度,底部「刷新/确定/取消」被挤出可视区(用户实测截图问题)。
        self.update_idletasks()
        self.geometry(self._fit_geometry())
        self._center_on_screen()
        self._bring_to_front()
        self.grab_set()
        # 进入即自动检测一次
        self.after(50, self._refresh_devices)

    def _fit_geometry(self, min_h: int = 280, max_h: int = 560) -> str:
        """按内容实际高度计算窗口尺寸(v3 弹窗宽 480),避免固定几何裁切内容。"""
        self.update_idletasks()
        w = max(480, self.winfo_reqwidth())
        h = max(min_h, min(max_h, self.winfo_reqheight()))
        return f"{w}x{h}"

    def _center_on_screen(self) -> None:
        """把对话框(当前实际尺寸)居中到屏幕。"""
        self.update_idletasks()
        w = max(480, self.winfo_reqwidth())
        h = max(280, self.winfo_reqheight())
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _bring_to_front(self) -> None:
        """确保对话框显示在终端等其他窗口之上并拿到键盘焦点。"""
        self.deiconify()
        self.lift()
        try:
            # 短暂置顶抢焦点,300ms 后解除,避免一直压住其他窗口
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass
        self.focus_force()

    # ------------------------------------------------------------------
    # UI(v3 卡片样式,对齐 design_proposal_v3.html .modal/.devcard)
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # 整窗即一张 modal:surface 底 + 较强 1px 描边(圆角/阴影 Tk 无原生支持)
        modal = tk.Frame(self, bg=THEME["surface"], highlightthickness=1,
                         highlightbackground=THEME["border2"])
        modal.pack(fill="both", expand=True)

        # 头部:标题 + 右侧 ✕(= 取消退出)
        hd = tk.Frame(modal, bg=THEME["surface"])
        hd.pack(fill="x", padx=18, pady=(15, 0))
        tk.Label(hd, text="连接设备", bg=THEME["surface"], fg=THEME["txt"],
                 font=FONT_UI_BOLD).pack(side="left")
        x_lbl = tk.Label(hd, text="✕", bg=THEME["surface"], fg=THEME["txt3"],
                         font=FONT_UI)
        x_lbl.pack(side="right")
        x_lbl.bind("<Button-1>", lambda _e: self._cancel())

        # 主体:引导语 + 设备卡片容器 + 状态行
        bd = tk.Frame(modal, bg=THEME["surface"])
        bd.pack(fill="both", expand=True, padx=18, pady=(14, 0))
        tk.Label(bd, text="选择一台在线设备以进入控制台",
                 bg=THEME["surface"], fg=THEME["txt2"], font=FONT_CAPTION,
                 anchor="w").pack(fill="x")
        self._dev_list_frame = tk.Frame(bd, bg=THEME["surface"])
        self._dev_list_frame.pack(fill="both", expand=True, pady=(13, 0))

        self.status_var = tk.StringVar(value="正在检测设备…")
        self.status_label = tk.Label(bd, textvariable=self.status_var,
                                     bg=THEME["surface"], fg=THEME["txt2"],
                                     font=FONT_CAPTION, wraplength=440,
                                     justify="left", anchor="w")
        self.status_label.pack(fill="x", pady=(12, 16))

        # 底部:顶边分隔 + 「⟳ 刷新」 + 主按钮「进入控制台」
        ft = tk.Frame(modal, bg=THEME["surface"], highlightthickness=1,
                      highlightbackground=THEME["border"],
                      highlightcolor=THEME["border"])
        ft.pack(fill="x", side="bottom")
        self.refresh_btn = _RoundedButton(ft, text="↻ 刷新",
                                          command=self._refresh_devices)
        self.refresh_btn.pack(side="left", padx=18, pady=12)
        self.ok_btn = _RoundedButton(ft, text="进入控制台", command=self._confirm,
                                     style="primary", state="disabled")
        self.ok_btn.pack(side="right", padx=18, pady=12)

    # ------------------------------------------------------------------
    # 设备枚举(后台线程)
    # ------------------------------------------------------------------
    def _refresh_devices(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.refresh_btn.configure(state="disabled")
        self.ok_btn.configure(state="disabled")
        self.status_var.set("正在执行 adb devices 检测设备…")
        # 清空旧设备卡片(容器在,子控件销毁)
        for w in self._dev_list_frame.winfo_children():
            w.destroy()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        """后台线程执行 adb devices,结果经队列回主线程渲染(不触碰 Tk)。"""
        error, devices = None, []
        try:
            devices = AdbClient().list_devices()
        except Exception as exc:  # adb 未找到/超时/返回非零,均允许刷新重试
            error = exc
        # 窗口可能已在此期间被关闭:bridge 关闭后投递被直接丢弃
        self._bridge.post(lambda: self._refresh_done(error, devices))

    def _refresh_done(self, error, devices) -> None:
        self._busy = False
        if not self.winfo_exists():
            return
        self.refresh_btn.configure(state="normal")

        if error is not None:
            self.status_var.set(
                f"设备检测失败: {error}\n请确认 adb 已安装并加入 PATH,然后点「⟳ 刷新」重试。")
            self.status_label.configure(fg=THEME["red"])
            self.geometry(self._fit_geometry())
            return

        self._devices = devices
        online_serials = [serial for serial, state in devices
                          if state == "device"]
        # 重建设备卡片(清空旧卡)
        self._render_dev_cards()

        if online_serials:
            self.status_var.set(
                f"共 {len(devices)} 台设备,其中 {len(online_serials)} 台在线。"
                "双击设备或选中后点「进入控制台」。")
            # 只有一台在线设备时自动选中,省一次点击
            if len(online_serials) == 1:
                self._select_card(online_serials[0])
        elif devices:
            self._sel_serial = None
            self.status_var.set(
                "检测到设备但均不可用(离线/未授权)。请在手机上允许 USB 调试"
                "或等待设备上线,然后点「⟳ 刷新」。")
        else:
            self._sel_serial = None
            self.status_var.set(
                "未检测到设备。请连接手机(开启 USB 调试)或启动模拟器,"
                "然后点「⟳ 刷新」。")
        self.status_label.configure(fg=THEME["txt2"])
        # 设备数量变化会改变内容高度,重新贴合一次避免裁切
        self.geometry(self._fit_geometry())
        self._sync_ok_state()

    # ------------------------------------------------------------------
    # 设备卡片渲染(v3 .devcard)
    # ------------------------------------------------------------------
    # 非在线状态 -> 徽章短文案(STATE_LABELS 的完整文案留给状态行)
    _BADGE_SHORT = {
        "offline": "离线",
        "unauthorized": "未授权",
        "recovery": "恢复模式",
    }

    def _render_dev_cards(self) -> None:
        """按 self._devices 重建全部设备卡片。"""
        for w in self._dev_list_frame.winfo_children():
            w.destroy()
        for serial, state in self._devices:
            self._make_dev_card(serial, state)

    def _make_dev_card(self, serial: str, state: str) -> None:
        """构造一台设备的卡片(在线可点选,离线灰显不可选)。"""
        online = state == "device"
        selected = self._sel_serial == serial
        card_bg = THEME["accent_soft"] if selected else THEME["bg2"]
        card_hl = THEME["accent"] if selected else THEME["border2"]
        card = tk.Frame(self._dev_list_frame, bg=card_bg,
                        highlightthickness=1, highlightbackground=card_hl)
        card.pack(fill="x", pady=(0, 10))

        # 左:设备图标块(网络设备 🖥,其余 📱)
        icon_txt = "🖥" if ":" in serial else "📱"
        icon = tk.Label(card, text=icon_txt, bg=THEME["surface2"],
                        fg=THEME["accent"] if selected else THEME["txt2"],
                        font=FONT_EMOJI, padx=7, pady=5)
        icon.pack(side="left", padx=(11, 0), pady=11)

        # 中:mono 序列号 + meta 文案
        info = tk.Frame(card, bg=card_bg)
        info.pack(side="left", fill="x", expand=True, padx=13, pady=11)
        nm = tk.Label(info, text=serial, bg=card_bg, fg=THEME["txt"],
                      font=FONT_MONO, anchor="w")
        nm.pack(fill="x")
        meta_txt = self._device_meta(serial)
        meta_lbl = tk.Label(info, text=meta_txt, bg=card_bg, fg=THEME["txt3"],
                            font=FONT_CAPTION, anchor="w")
        meta_lbl.pack(fill="x", pady=(3, 0))

        # 右:在线绿/离线红中性徽章(不发光)
        if online:
            badge_txt, badge_fg = "● 在线", THEME["green"]
        else:
            badge_txt = "● " + self._BADGE_SHORT.get(
                state, self.STATE_LABELS.get(state, state))
            badge_fg = THEME["red"]
        badge = tk.Label(card, text=badge_txt, bg=card_bg, fg=badge_fg,
                         font=FONT_CAPTION)
        badge.pack(side="right", padx=13)

        # 仅在线卡片可点:单击选中、双击确认(默认参数捕获序列号,避开闭包问题)
        if online:
            clickables = (card, icon, info, nm, meta_lbl, badge)
            for w in clickables:
                w.bind("<Button-1>",
                       lambda _e, s=serial: self._select_card(s))
                w.bind("<Double-Button-1>",
                       lambda _e, s=serial: self._confirm())

    @staticmethod
    def _device_meta(serial: str) -> str:
        """设备卡片 meta 文案:网络/模拟器/USB。"""
        if ":" in serial:
            return "Network"
        if serial.startswith("emulator"):
            return "Android Emulator"
        return "USB 设备"

    def _select_card(self, serial: str) -> None:
        """点选一张在线设备卡:记录、重渲染选中态、同步主按钮。"""
        self._sel_serial = serial
        self._render_dev_cards()
        self._sync_ok_state()

    # ------------------------------------------------------------------
    # 选择确认
    # ------------------------------------------------------------------
    def _selected_serial(self) -> Optional[str]:
        # 选中态只可能来自在线卡片点击(离线卡片不绑定选择),直接返回即可
        return self._sel_serial

    def _sync_ok_state(self) -> None:
        state = "normal" if self._selected_serial() else "disabled"
        self.ok_btn.configure(state=state)

    def _confirm(self) -> None:
        serial = self._selected_serial()
        if not serial:
            return
        self.selected = serial
        self._teardown()

    def _cancel(self) -> None:
        self.selected = None
        self._teardown()

    def _teardown(self) -> None:
        """关闭对话框的唯一出口:停轮询、释放模态抓取、销毁。"""
        self._bridge.close()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class _RoundedBadge(tk.Label):
    """圆角数字徽章:用 PIL 绘制圆角矩形底图(非交互,纯展示)。"""

    _img_cache: Dict[tuple, ImageTk.PhotoImage] = {}

    @classmethod
    def _make_bg(cls, w: int, h: int, r: int, fill: str) -> ImageTk.PhotoImage:
        key = (w, h, r, fill)
        if key not in cls._img_cache:
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill)
            cls._img_cache[key] = ImageTk.PhotoImage(img)
        return cls._img_cache[key]

    def __init__(self, parent, text: str, size: int = 22, radius: int = 5,
                 fill: str = THEME["accent"], fg: str = "#fff",
                 font=FONT_CAPTION, **kwargs):
        self._img = self._make_bg(size, size, radius, fill)
        try:
            _parent_bg = parent.cget("bg") or THEME["bg2"]
        except tk.TclError:
            _parent_bg = THEME["bg2"]
        super().__init__(parent, image=self._img, compound="center",
                         text=text, fg=fg, font=font, bg=_parent_bg,
                         bd=0, padx=0, pady=0, **kwargs)


class _RoundedButton(tk.Label):
    """圆角按钮:用 PIL 绘制圆角矩形底图,支持 normal/primary/ghost + hover。

    对齐设计稿 .btn:font 13px、圆角 6px、内边距 7px 14px、1px border2 描边。
    """

    _img_cache: Dict[tuple, ImageTk.PhotoImage] = {}

    @classmethod
    def _make_bg(cls, w: int, h: int, r: int, fill: str,
                 outline: Optional[str] = None) -> ImageTk.PhotoImage:
        key = (w, h, r, fill, outline)
        if key not in cls._img_cache:
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if outline:
                d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill, outline=outline, width=1)
            else:
                d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill)
            cls._img_cache[key] = ImageTk.PhotoImage(img)
        return cls._img_cache[key]

    def __init__(self, parent, text: str = "", command=None, style: str = "normal",
                 width: Optional[int] = None, height: int = 30,
                 radius: int = 6, icon: str = "", **kwargs):
        self.command = command
        self.style = style
        self._enabled = True
        palette = {
            "normal":  (THEME["surface2"], THEME["border2"], THEME["txt"], THEME["accent_bg"]),
            "primary": (THEME["accent"],   THEME["accent"],  "#ffffff",     "#8b7ce0"),
            "ghost":   (THEME["surface"],  THEME["border2"], THEME["txt2"], THEME["surface2"]),
        }
        self._fill, self._bd, self._fg, self._fill_hover = palette.get(style, palette["normal"])
        self._text = (f"{icon}  " if icon else "") + text
        self._height = height
        self._radius = radius
        # 估算文字宽度(等宽不够精确,用 tkFont 实测)
        from tkinter import font as _tkfont
        f = _tkfont.Font(family=FONT_BTN[0], size=FONT_BTN[1])
        text_w = f.measure(self._text)
        self._width = width or max(56, text_w + 28)  # 左右各 14px 内边距
        self._build_images()
        # 取父容器背景色(圆角图的透明角需与父容器同色才无缝)
        try:
            _parent_bg = parent.cget("bg") or THEME["surface"]
        except tk.TclError:
            _parent_bg = THEME["surface"]
        super().__init__(parent, image=self._img_normal, compound="center",
                         text=self._text, fg=self._fg, font=FONT_BTN,
                         bg=_parent_bg,
                         bd=0, padx=0, pady=0, cursor="hand2")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _build_images(self) -> None:
        self._img_normal = self._make_bg(self._width, self._height, self._radius,
                                         self._fill, self._bd)
        self._img_hover = self._make_bg(self._width, self._height, self._radius,
                                        self._fill_hover, self._bd)
        self._img_disabled = self._make_bg(self._width, self._height, self._radius,
                                           THEME["surface2"], THEME["border"])

    def _on_enter(self, _e):
        if self._enabled:
            self.configure(image=self._img_hover)

    def _on_leave(self, _e):
        if self._enabled:
            self.configure(image=self._img_normal)

    def _on_click(self, _e):
        if self._enabled and self.command:
            self.command()

    def configure(self, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            self.configure(image=self._img_disabled if not self._enabled else self._img_normal)
            self.configure(cursor="" if not self._enabled else "hand2")
        if kw:
            super().configure(**kw)

    def config(self, **kw):
        self.configure(**kw)


class StepApp(tk.Tk):
    """步骤编排主窗口:网格图选格 + 步骤列表 + 执行控制。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("GrapeMobile 步骤编排器")
        # 可缩放:固定尺寸会把右栏 Spinbox/按钮裁掉(实测截图问题)
        self.resizable(True, True)
        self.minsize(1200, 720)
        # 暗色克制主题(单一葡萄紫强调色,无霓虹/渐变);须在任意控件创建前应用
        self._apply_dark_theme()
        # 主窗口在设备选择/首次截图完成前保持隐藏,避免弹出主窗口后又因失败退出
        self.withdraw()
        self._startup_ok = False
        # 所有后台线程经此桥接回主线程,禁止子线程直接调用 after()/触碰组件
        self._bridge = _UiBridge(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.steps: List[Dict[str, Any]] = []
        self._running = False
        self.device_id = ""
        self._latest_raw = None  # 最近一次设备截图的原始画面(无网格叠加),仅静态回退时用
        # 当前脚本名(锚点文件夹名):未保存前为 untitled,保存/加载后与 JSON 同名
        self.script_name = DRAFT_SCRIPT_NAME
        self.script_path: Optional[str] = None
        # before/after 图片各自连续编号(扫描 images/ 接续,防跨会话覆盖)
        self._before_seq = 0
        self._after_seq = 0
        self._anchor_client: Optional[AdbClient] = None  # 录制锚点用的惰性 adb 客户端
        # AI 模式状态(新定位管线 pipeline.locate)
        self._ai_result = None        # LocateResult
        self._ai_cand_idx = 0         # 当前展示的候选索引
        self._ai_busy = False         # 定位线程运行中
        self._ai_overlay_active = False  # 画布正显示候选框(屏蔽普通点格录入)
        self._ai_adjusted = False     # 当前候选被用户手动微调过
        self._ai_drag_last = None     # 拖动微调上一次位置
        self._ai_instruction = ""     # 当前指令文本(确认后随步骤保存为 desc)

        # ---- 启动闸门 1:弹窗枚举 adb devices,必须选中一台在线设备 ----
        dlg = DeviceSelectDialog(self)
        self.wait_window(dlg)
        device_id = dlg.selected
        if not device_id:
            # 用户取消/关窗:无设备不允许进入,直接退出
            self._quit_startup()
            return
        self.device_id = device_id
        # 写 device_id.txt:与 capture.py 产物布局一致,
        # StepRunner.from_capture_artifacts / grid.py / main.py 可直接复用
        try:
            with open(DEVICE_ID_FILE, "w", encoding="utf-8") as f:
                f.write(device_id)
        except OSError as exc:
            messagebox.showerror("初始化失败", f"写入 device_id.txt 失败: {exc}")
            self._quit_startup()
            return

        # ---- 启动闸门 2:连设备实时截图生成网格;失败则提示并退出,不再静默回退静态图 ----
        self._startup_msg = ""
        try:
            self._capture_from_device()
            self._startup_msg = f"已连接设备 {self.device_id},当前为实时截图"
        except Exception as exc:
            messagebox.showerror(
                "设备截图失败",
                f"连接设备 {device_id} 或截图失败:\n{exc}\n\n"
                "请确认设备在线且 USB 调试可用,然后重新启动本程序。",
            )
            self._quit_startup()
            return

        self.deiconify()
        self.lift()
        self.focus_force()
        self._build_ui()
        self._startup_ok = True
        self._log(self._startup_msg)
        # AI 思考过程展示:aiclient 每次请求/回复 + locate 管线各阶段日志 → AI 日志区
        aiclient.trace = self._ai_trace
        locate_logger = logging.getLogger("imgloc.locate")
        locate_logger.setLevel(logging.INFO)
        if not any(isinstance(h, _AiLogHandler) for h in locate_logger.handlers):
            locate_logger.addHandler(_AiLogHandler(self))
        # 默认打开 AI 模式 Tab(右栏第 0 页;用户主要工作区)
        self._switch_tab(0)
        # AI 模式欢迎语(系统提示样式,居中浅色)
        self._ai_chat_append("系统", "输入想做的操作(如「点击 kof 图标」),\n"
                                     "定位后确认即可存入脚本步骤。\n"
                                     "也可以切到普通模式直接点格子添加步骤。")

        # ---- 伪实时:每秒自动重截(后台线程生产,主线程应用) ----
        self._auto_var = tk.BooleanVar(value=False)  # 自动刷新开关
        self._auto_busy = False        # 是否有截图线程在跑(防止重叠)
        self._auto_fail_logged = False # 连续失败只记一次日志,避免刷屏
        self.after(1000, self._auto_tick)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def _quit_startup(self) -> None:
        """启动阶段(设备选择/首次截图失败或取消)退出:先断桥接再销毁。"""
        self._bridge.close()
        self.destroy()

    def _on_close(self) -> None:
        """主窗口关闭:停止后台->主线程投递,后台线程为 daemon,随进程退出。"""
        aiclient.trace = None
        self._bridge.close()
        self.destroy()

    # ------------------------------------------------------------------
    # 自动刷新(伪实时)
    # ------------------------------------------------------------------
    def _auto_tick(self) -> None:
        """每秒触发一次:条件满足时启动后台截图线程。"""
        if not self.winfo_exists():
            return
        if (self._auto_var.get() and not self._running
                and not self._auto_busy and self.device_id
                # AI 定位中/候选待确认时暂停:候选坐标基于当前帧,
                # 画面被自动刷新换掉后坐标即失效,确认按钮也会被 _set_grid 清掉
                and not self._ai_busy and not self._ai_overlay_active):
            self._auto_busy = True
            threading.Thread(target=self._auto_worker, daemon=True).start()
        self.after(1000, self._auto_tick)

    def _auto_worker(self) -> None:
        """后台线程:只做 adb 截图与网格生成,不触碰 UI。"""
        error, payload = None, None
        try:
            payload = self._produce_grid_from_device()
        except Exception as exc:
            error = exc
        self._bridge.post(lambda: self._auto_apply(error, payload))

    def _auto_apply(self, error, payload) -> None:
        """主线程:应用自动刷新结果。"""
        self._auto_busy = False
        # 截图期间用户点了"运行":丢弃本次刷新,避免半写入的 screenshot.png 影响执行,
        # 也避免执行过程中画面被替换
        if self._running:
            return
        if error is not None:
            if not self._auto_fail_logged:
                self._log(f"[自动刷新] 失败: {error}")
                self._auto_fail_logged = True
            return
        self._auto_fail_logged = False
        device_id, grid, cell_size, raw = payload
        self.device_id = device_id
        self._latest_raw = raw
        self._set_grid(grid, cell_size)

    # ------------------------------------------------------------------
    # 网格图来源:设备实时截图 / 静态文件
    # ------------------------------------------------------------------
    def _produce_grid_from_device(self):
        """
        连接设备 -> 实时截图 -> 生成网格图(并落盘保持与 CLI 产物一致)。
        纯数据生产,不触碰任何 UI 组件,可在后台线程调用。

        Returns:
            (device_id, grid_bgr, cell_size, raw_bgr)

        Raises:
            StepError / AdbError: 未选择设备、设备离线或截图失败时抛出。
        """
        device_id = self.device_id
        if not device_id:
            raise StepError("尚未选择设备,请重新启动并在设备列表中选择一台在线设备")

        client = AdbClient()
        # 与 executor.AdbExecutor 一致:含 ":" 视为网络设备走 adb connect,
        # 模拟器/USB 序列号走 attach 在线校验
        if ":" in device_id:
            client.connect(device_id)
        else:
            client.attach(device_id)
        image_path = client.screenshot()  # 保存到 runner/tmp/image/screenshot.png
        img = cv2.imread(image_path)
        if img is None:
            raise StepError(f"截图读取失败: {image_path}")

        cell_size = self._read_cell_size()
        marker = GridMarker(cell_size=cell_size)
        grid = marker.draw(img)
        cv2.imwrite(GRID_IMAGE, grid)
        # 同步 sidecar 元数据,供 grid.py CLI 查询使用
        import json as _json

        cols, rows = marker.grid_dims(img.shape)
        with open(GRID_META, "w", encoding="utf-8") as f:
            _json.dump({"cell_size": cell_size,
                        "image_width": int(img.shape[1]),
                        "image_height": int(img.shape[0]),
                        "cols": cols, "rows": rows}, f, ensure_ascii=False, indent=2)
        return device_id, grid, cell_size, img

    def _capture_from_device(self) -> None:
        """生产网格并应用到界面(主线程调用)。"""
        device_id, grid, cell_size, raw = self._produce_grid_from_device()
        self.device_id = device_id
        self._latest_raw = raw
        self._set_grid(grid, cell_size)

    def _read_cell_size(self) -> int:
        """优先沿用现有网格元数据中的格子尺寸,否则用默认值。"""
        import json as _json

        if os.path.isfile(GRID_META):
            try:
                with open(GRID_META, "r", encoding="utf-8") as f:
                    return int(_json.load(f)["cell_size"])
            except (_json.JSONDecodeError, KeyError, ValueError):
                pass
        return DEFAULT_CELL_SIZE

    def _set_grid(self, grid_bgr, cell_size: int) -> None:
        """设置当前网格图并重算显示参数(画布存在时同步刷新)。"""
        self.grid_bgr = grid_bgr
        self.cell_size = cell_size
        gh, gw = grid_bgr.shape[:2]
        self.scale = min(1.0, MAX_DISPLAY_WIDTH / gw, MAX_DISPLAY_HEIGHT / gh)
        self.disp_w, self.disp_h = int(gw * self.scale), int(gh * self.scale)
        # Tkinter PhotoImage 不支持任意比例缩放,先用 cv2 缩放后写临时文件再加载
        disp = cv2.resize(grid_bgr, (self.disp_w, self.disp_h), interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(), "grapemobile_grid_display.png")
        cv2.imwrite(self._tmp, disp)
        new_photo = tk.PhotoImage(file=self._tmp)
        old_photo = getattr(self, "photo", None)
        self.photo = new_photo
        self._photo_ref = old_photo  # 保持引用,避免旧图被 GC 导致画布闪黑
        if hasattr(self, "canvas"):
            self.canvas.configure(width=self.disp_w, height=self.disp_h)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.title(f"GrapeMobile 步骤编排器 - {self.device_id or '静态网格图'}")
        # 新网格到达后,旧候选的像素坐标失效,复位 AI 预览态
        if hasattr(self, "_ai_overlay_active"):
            self._ai_overlay_active = False
            self._ai_result = None
            self._ai_cand_idx = 0
            self._ai_adjusted = False
            self._ai_drag_last = None
            if hasattr(self, "_ai_confirm_btn"):
                self._show_cand_card(False)
                self._ai_cand_info_var.set("")

    # ------------------------------------------------------------------
    # 暗色主题
    # ------------------------------------------------------------------
    def _apply_dark_theme(self) -> None:
        """应用暗色克制主题:全局 option_add(tk 原生控件)+ ttk.Style(ttk 控件)。

        仅在 __init__ 早期、任意控件创建前调用一次;不改动任何业务/布局逻辑。
        视觉定调:单一葡萄紫强调色、无霓虹渐变、中性工程状态色。
        """
        T = THEME
        # ---- tk 原生控件:Text / Listbox / Canvas / Toplevel 等 ----
        # 兜底:*background / *foreground 覆盖未显式指定底色的 tk 控件(含窗口本身)
        self.option_add("*background", T["bg"])
        self.option_add("*foreground", T["txt"])
        self.option_add("*Toplevel.background", T["bg"])
        self.option_add("*highlightBackground", T["border2"])
        self.option_add("*highlightColor", T["accent"])
        self.option_add("*Text.background", T["term_bg"])
        self.option_add("*Text.foreground", T["term_fg"])
        self.option_add("*Text.selectBackground", T["accent"])
        self.option_add("*Text.selectForeground", "#ffffff")
        self.option_add("*Listbox.background", T["surface2"])
        self.option_add("*Listbox.foreground", T["txt"])
        self.option_add("*Listbox.selectBackground", T["accent"])
        self.option_add("*Listbox.selectForeground", "#ffffff")
        self.option_add("*Canvas.background", T["bg"])
        self.option_add("*Entry.background", T["surface2"])
        self.option_add("*Entry.foreground", T["txt"])

        # ---- ttk 控件:基于 clam 主题重配暗色(原生 Windows 主题无法整体压暗) ----
        try:
            style = ttk.Style(self)
            style.theme_use("clam")
        except Exception:
            style = ttk.Style(self)
        style.configure(".", background=T["bg"], foreground=T["txt"],
                        bordercolor=T["border"], darkcolor=T["surface2"],
                        lightcolor=T["surface"], troughcolor=T["surface2"],
                        font=FONT_UI)
        style.configure("TFrame", background=T["bg"])
        style.configure("TLabel", background=T["bg"], foreground=T["txt"],
                        font=FONT_UI)
        # 区块小标题:小一号、次要色(对齐设计稿的 section caption 层级)
        style.configure("Caption.TLabel", background=T["bg"], foreground=T["txt2"],
                        font=FONT_CAPTION)
        # 顶栏:品牌名(粗体)与设备状态
        style.configure("Brand.TLabel", background=T["bg"], foreground=T["txt"],
                        font=FONT_UI_BOLD)
        style.configure("TButton", background=T["surface2"], foreground=T["txt"],
                        bordercolor=T["border2"], relief="flat", padding=(12, 6),
                        font=FONT_BTN)
        style.map("TButton",
                  background=[("active", T["accent_bg"]), ("pressed", T["accent_bg"])],
                  foreground=[("active", T["txt"])])
        style.configure("Accent.TButton", background=T["accent"], foreground="#ffffff",
                        bordercolor=T["accent"], relief="flat", padding=(12, 6),
                        font=FONT_BTN_BOLD)
        style.map("Accent.TButton",
                  background=[("active", "#8b7ce0"), ("pressed", "#8b7ce0")],
                  foreground=[("active", "#ffffff"), ("disabled", "#ffffff")])
        style.configure("TEntry", fieldbackground=T["surface2"], foreground=T["txt"],
                        bordercolor=T["border2"], insertcolor=T["txt"],
                        padding=(11, 10), font=FONT_BTN)
        style.configure("TCombobox", fieldbackground=T["surface2"], foreground=T["txt"],
                        bordercolor=T["border2"], arrowcolor=T["txt2"], font=FONT_BTN)
        style.map("TCombobox", fieldbackground=[("readonly", T["surface2"])])
        style.configure("TSpinbox", fieldbackground=T["surface2"], foreground=T["txt"],
                        bordercolor=T["border2"], arrowcolor=T["txt2"], font=FONT_BTN,
                        padding=(8, 6))
        style.configure("TCheckbutton", background=T["bg"], foreground=T["txt"])
        style.configure("TNotebook", background=T["bg"], bordercolor=T["border"])
        style.configure("TNotebook.Tab", background=T["surface"], foreground=T["txt2"],
                        padding=(16, 7), font=FONT_UI)
        style.map("TNotebook.Tab", background=[("selected", T["bg"])],
                  foreground=[("selected", T["accent"])])
        style.configure("TScrollbar", background=T["surface2"], troughcolor=T["bg"],
                        bordercolor=T["border"], arrowcolor=T["txt2"])
        style.configure("Treeview", background=T["surface"], foreground=T["txt"],
                        fieldbackground=T["surface"], bordercolor=T["border"],
                        rowheight=30, font=FONT_LIST)
        style.configure("Treeview.Heading", background=T["surface2"], foreground=T["txt2"],
                        bordercolor=T["border"], relief="flat", font=FONT_CAPTION)
        style.map("Treeview", background=[("selected", T["accent_bg"])],
                  foreground=[("selected", T["txt"])])
        style.configure("Separator", background=T["border"])

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=0)
        root.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=0)   # 左栏:设备画面(固定宽)
        root.columnconfigure(1, weight=1)   # 中栏:步骤实时预览(弹性)
        root.columnconfigure(2, weight=1)   # 右栏:三 Tab(弹性)
        root.rowconfigure(1, weight=1)      # 主体行
        root.rowconfigure(2, weight=0)      # 底部 Console(固定高)

        # ==================================================================
        # 顶栏 appbar:品牌 + 设备状态 pill + 右侧图标按钮(对齐设计稿)
        # ==================================================================
        appbar = tk.Frame(root, bg=THEME["bg2"], height=44)
        appbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        appbar.grid_propagate(False)
        # 品牌
        tk.Label(appbar, text="▣", fg=THEME["accent"], bg=THEME["bg2"],
                 font=("Segoe UI", 13)).pack(side="left", padx=(14, 4), pady=8)
        tk.Label(appbar, text="GrapeMobile", fg=THEME["txt"], bg=THEME["bg2"],
                 font=FONT_UI_BOLD).pack(side="left", pady=8)
        # 设备状态 pill
        pill = tk.Frame(appbar, bg=THEME["surface2"], highlightthickness=1,
                        highlightbackground=THEME["border2"])
        tk.Label(pill, text="●", fg=THEME["green"], bg=THEME["surface2"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(8, 4), pady=4)
        tk.Label(pill, text=f"{self.device_id} · 在线", fg=THEME["txt2"],
                 bg=THEME["surface2"], font=FONT_CAPTION).pack(side="left",
                                                                padx=(0, 10), pady=4)
        pill.pack(side="left", padx=(16, 0), pady=8)
        # 右侧图标按钮(圆角小方块)
        for tip, glyph, cmd in (
            ("刷新截图", "↻", self._refresh),
            ("截取锚点", "📷", lambda: self._recapture_anchor("before_image")),
            ("网格", "⊞", self._toggle_grid),
        ):
            ib = _RoundedButton(appbar, text=glyph, command=cmd, style="ghost",
                                width=30)
            ib.pack(side="right", padx=2, pady=7)

        ttk.Separator(root, orient="horizontal").grid(
            row=0, column=0, columnspan=3, sticky="sew", pady=(43, 0))
        # 三栏列权重:左固定300,中/右平分剩余
        root.grid_columnconfigure(0, weight=0)
        root.grid_columnconfigure(1, weight=1)
        root.grid_columnconfigure(2, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # ==================================================================
        # 左栏:设备画面(画布 + 工具条 + 提示)
        # ==================================================================
        left = tk.Frame(root, bg=THEME["bg"], width=280)
        left.grid(row=1, column=0, sticky="ns", padx=(10, 0), pady=8)
        left.grid_propagate(False)
        tk.Label(left, text="设备画面", fg=THEME["txt3"], bg=THEME["bg"],
                 font=FONT_CAPTION).pack(anchor="w", pady=(0, 6))
        self.canvas = tk.Canvas(left, width=self.disp_w, height=self.disp_h,
                                highlightthickness=1,
                                highlightbackground=THEME["border2"], bg=THEME["bg2"])
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        # 工具条
        tools = tk.Frame(left, bg=THEME["bg"])
        tools.pack(fill="x", pady=(8, 0))
        self._grid_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="网格", variable=self._grid_on,
                        command=self._toggle_grid).pack(side="left", padx=(0, 6))
        _RoundedButton(tools, text="－", width=28,
                       command=lambda: self._zoom(1 / 1.25)).pack(side="left", padx=1)
        self._zoom_label = tk.Label(tools, text="100%", width=5, anchor="center",
                                    bg=THEME["bg"], fg=THEME["txt2"], font=FONT_CAPTION)
        self._zoom_label.pack(side="left", padx=1)
        _RoundedButton(tools, text="＋", width=28,
                       command=lambda: self._zoom(1.25)).pack(side="left", padx=1)
        _RoundedButton(tools, text="1:1", width=36,
                       command=self._zoom_reset).pack(side="left", padx=(6, 1))
        tk.Label(left, text="点画面任意处 = 追加一步点击(自动定位格子)",
                 fg=THEME["txt3"], bg=THEME["bg"], font=FONT_CAPTION,
                 wraplength=280, justify="left").pack(anchor="w", pady=(8, 0))

        # ==================================================================
        # 中栏:步骤 · 实时预览(脚本名 + 步骤卡片列表 + 运行条 + 步骤详情/锚点)
        # ==================================================================
        center = tk.Frame(root, bg=THEME["surface"])
        center.grid(row=1, column=1, sticky="nsew", padx=10, pady=8)
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=1)
        tk.Label(center, text="步骤 · 实时预览", fg=THEME["txt3"],
                 bg=THEME["surface"], font=FONT_CAPTION).grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        # 脚本名行
        sname = tk.Frame(center, bg=THEME["surface"])
        sname.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        sname.columnconfigure(0, weight=1)
        self.script_name_var = tk.StringVar(value="")
        tk.Entry(sname, textvariable=self.script_name_var,
                 bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                 bd=0, relief="flat", insertbackground=THEME["txt"],
                 highlightthickness=1,
                 highlightbackground=THEME["border2"],
                 highlightcolor=THEME["border2"]).grid(
            row=0, column=0, sticky="ew", padx=(0, 6), ipady=8)
        _RoundedButton(sname, text="保存", command=self._save).grid(row=0, column=1, padx=2)
        _RoundedButton(sname, text="加载", command=self._load).grid(row=0, column=2, padx=2)
        # 步骤卡片滚动区
        self._step_cards_wrap = tk.Frame(center, bg=THEME["surface"])
        self._step_cards_wrap.grid(row=2, column=0, sticky="nsew")
        self._step_cards_wrap.grid_columnconfigure(0, weight=1)
        self._step_cards_wrap.grid_rowconfigure(0, weight=1)
        self._cards_canvas = tk.Canvas(self._step_cards_wrap, bg=THEME["surface"],
                                       highlightthickness=0)
        cards_scroll = ttk.Scrollbar(self._step_cards_wrap, orient="vertical",
                                     command=self._cards_canvas.yview)
        self._cards_canvas.configure(yscrollcommand=cards_scroll.set)
        self._cards_canvas.grid(row=0, column=0, sticky="nsew")
        cards_scroll.grid(row=0, column=1, sticky="ns")
        self._step_cards_inner = tk.Frame(self._cards_canvas, bg=THEME["surface"])
        self._cards_win = self._cards_canvas.create_window(
            (0, 0), window=self._step_cards_inner, anchor="nw")
        self._step_cards_inner.bind("<Configure>",
            lambda _e: self._cards_canvas.configure(
                scrollregion=self._cards_canvas.bbox("all")))
        self._cards_canvas.bind("<Configure>",
            lambda e: self._cards_canvas.itemconfigure(self._cards_win, width=e.width))
        # 运行条(居中紧凑排列)
        runbar = tk.Frame(center, bg=THEME["surface"])
        runbar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        runbar_btns = tk.Frame(runbar, bg=THEME["surface"])
        runbar_btns.pack()
        self.run_btn = _RoundedButton(runbar_btns, text="▶ 运行", command=self._run,
                                      style="primary", width=96)
        self.run_btn.pack(side="left", padx=3)
        self.full_run_btn = _RoundedButton(runbar_btns, text="完整运行", command=self._full_run,
                                           width=96)
        self.full_run_btn.pack(side="left", padx=3)
        _RoundedButton(runbar_btns, text="清空", command=self._clear, style="ghost",
                       width=96).pack(side="left", padx=3)
        # 步骤详情 + 锚点(复用原 preview 控件,置于中栏卡片下方)
        self.preview_frame = tk.Frame(center, bg=THEME["bg2"],
                                      highlightthickness=1,
                                      highlightbackground=THEME["border"])
        self.preview_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.preview_frame.grid_columnconfigure(0, weight=1)
        self.preview_frame.grid_columnconfigure(1, weight=1)
        self._preview_index: Optional[int] = None
        ttk.Label(self.preview_frame, text="步骤详情 / 锚点", style="Caption.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        self._detail_info_var = tk.StringVar(value="未选中步骤")
        ttk.Label(self.preview_frame, textvariable=self._detail_info_var,
                  foreground=THEME["txt2"], wraplength=340, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 0))
        waits = ttk.Frame(self.preview_frame)
        waits.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 0))
        self._detail_before_var = tk.StringVar(value="0")
        self._detail_after_var = tk.StringVar(value="0")
        ttk.Label(waits, text="前等待(s)", style="Caption.TLabel").grid(row=0, column=0)
        ttk.Spinbox(waits, from_=0, to=600, width=6,
                    textvariable=self._detail_before_var).grid(row=0, column=1, padx=(2, 10))
        ttk.Label(waits, text="后等待(s)", style="Caption.TLabel").grid(row=0, column=2)
        ttk.Spinbox(waits, from_=0, to=600, width=6,
                    textvariable=self._detail_after_var).grid(row=0, column=3, padx=(2, 10))
        _RoundedButton(waits, text="保存等待",
                       command=self._apply_step_detail).grid(row=0, column=4, padx=(8, 0))
        # 锚点图横向排列
        anchors = ttk.Frame(self.preview_frame)
        anchors.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 6))
        anchors.columnconfigure(0, weight=1)
        anchors.columnconfigure(1, weight=1)
        ttk.Label(anchors, text="执行前(开始条件)", style="Caption.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(anchors, text="执行后(完成条件)", style="Caption.TLabel").grid(
            row=0, column=1, sticky="w")
        self._before_photo = self._make_placeholder_image()
        self.before_img_label = tk.Label(
            anchors, image=self._before_photo, width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground=THEME["border2"],
            bg=THEME["surface2"])
        self.before_img_label.grid(row=1, column=0, sticky="w", pady=2)
        self._after_photo = self._make_placeholder_image()
        self.after_img_label = tk.Label(
            anchors, image=self._after_photo, width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground=THEME["border2"],
            bg=THEME["surface2"])
        self.after_img_label.grid(row=1, column=1, sticky="w", pady=2)
        self.before_status_var = tk.StringVar(value="未选中步骤")
        self.after_status_var = tk.StringVar(value="未选中步骤")
        ttk.Label(anchors, textvariable=self.before_status_var, style="Caption.TLabel",
                  wraplength=EXPECT_IMG_W, justify="left").grid(row=2, column=0, sticky="w")
        ttk.Label(anchors, textvariable=self.after_status_var, style="Caption.TLabel",
                  wraplength=EXPECT_IMG_W, justify="left").grid(row=2, column=1, sticky="w")
        anchor_btns = tk.Frame(self.preview_frame, bg=THEME["bg2"])
        anchor_btns.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        _RoundedButton(anchor_btns, text="重拍执行前图",
                       command=lambda: self._recapture_anchor("before_image")).pack(
            side="left", padx=(0, 6))
        _RoundedButton(anchor_btns, text="截执行后图",
                   command=lambda: self._recapture_anchor("after_image")).pack(side="left")

        # ==================================================================
        # 右栏:三 Tab 笔记本(AI / 普通 / 执行)
        # ==================================================================
        right = tk.Frame(root, bg=THEME["bg2"])
        right.grid(row=1, column=2, sticky="nsew", padx=(10, 10), pady=8)
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        # ---- 自定义 Tab 条(高亮药丸式,带图标;active=surface底+accent下划线) ----
        self._tab_idx = 0
        self._tab_panels: List[tk.Frame] = []
        self._tab_buttons: List[tuple] = []
        tab_bar = tk.Frame(right, bg=THEME["bg2"], height=38)
        tab_bar.grid(row=0, column=0, sticky="ew")
        tab_bar.grid_propagate(False)
        # 顺序与设计一致:AI / 普通 / 执行
        tab_defs = [("✦", "AI 模式"), ("≡", "普通模式"), ("▶", "执行模式")]
        for i, (icon, label) in enumerate(tab_defs):
            tf = tk.Frame(tab_bar, bg=THEME["bg2"])
            tf.pack(side="left", fill="both", expand=True)
            lbl = tk.Label(tf, text=f"{icon} {label}", bg=THEME["bg2"],
                           fg=THEME["txt2"], font=FONT_UI, pady=10)
            lbl.pack(fill="both", expand=True)
            uline = tk.Frame(tf, bg=THEME["bg2"], height=2)
            uline.pack(fill="x", side="bottom")
            for w in (tf, lbl, uline):
                w.bind("<Button-1>", lambda _e, idx=i: self._switch_tab(idx))
            self._tab_buttons.append((tf, lbl, uline))
        # 三个面板(同格叠放,grid/grid_remove 切换)
        for i in range(3):
            p = tk.Frame(right, bg=THEME["surface"])
            p.grid(row=1, column=0, sticky="nsew")
            self._tab_panels.append(p)
        ai_tab = self._tab_panels[0]
        normal = self._tab_panels[1]
        exec_tab = self._tab_panels[2]

        # ---- 普通模式:脚本名 + 紧凑步骤卡片(nstep) + 底部操作 + 日志 ----
        normal.columnconfigure(0, weight=1)
        normal.rowconfigure(1, weight=1)
        ns = tk.Frame(normal, bg=THEME["surface"])
        ns.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        ns.columnconfigure(0, weight=1)
        tk.Entry(ns, textvariable=self.script_name_var,
                 bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                 bd=0, relief="flat", insertbackground=THEME["txt"],
                 highlightthickness=1,
                 highlightbackground=THEME["border2"],
                 highlightcolor=THEME["border2"]).grid(
            row=0, column=0, sticky="ew", padx=(0, 6), ipady=8)
        _RoundedButton(ns, text="保存脚本", command=self._save).grid(row=0, column=1, padx=2)
        _RoundedButton(ns, text="加载脚本", command=self._load).grid(row=0, column=2, padx=2)
        # 紧凑卡片容器(Canvas 滚动)
        nc_wrap = tk.Frame(normal, bg=THEME["surface"])
        nc_wrap.grid(row=1, column=0, sticky="nsew", padx=10)
        nc_wrap.columnconfigure(0, weight=1)
        nc_wrap.rowconfigure(0, weight=1)
        self._normal_canvas = tk.Canvas(nc_wrap, bg=THEME["surface"],
                                        highlightthickness=0)
        self._normal_canvas.grid(row=0, column=0, sticky="nsew")
        nc_sb = ttk.Scrollbar(nc_wrap, orient="vertical",
                              command=self._normal_canvas.yview)
        nc_sb.grid(row=0, column=1, sticky="ns")
        self._normal_canvas.configure(yscrollcommand=nc_sb.set)
        self._normal_inner = tk.Frame(self._normal_canvas, bg=THEME["surface"])
        self._normal_win = self._normal_canvas.create_window(
            (0, 0), window=self._normal_inner, anchor="nw")
        self._normal_inner.bind("<Configure>", lambda _e: self._normal_canvas.configure(
            scrollregion=self._normal_canvas.bbox("all")))
        self._normal_canvas.bind("<Configure>", lambda e: self._normal_canvas.itemconfigure(
            self._normal_win, width=e.width))
        # 隐藏的 Treeview 作为选中状态模型(不显示)
        self.listbox = ttk.Treeview(
            normal, columns=("no", "title", "meta", "waits"), show="headings",
            selectmode="browse", height=0)
        self.listbox.bind("<<TreeviewSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", self._on_step_double_click)
        # 底部按钮:清空 / 运行(主) / 完整运行(居中紧凑排列,不拉满全宽)
        nfoot = tk.Frame(normal, bg=THEME["surface"])
        nfoot.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 6))
        btns = tk.Frame(nfoot, bg=THEME["surface"])
        btns.pack()
        _RoundedButton(btns, text="✕ 清空", command=self._clear, style="ghost",
                       width=88).pack(side="left", padx=3)
        _RoundedButton(btns, text="▶ 运行", command=self._run, style="primary",
                       width=88).pack(side="left", padx=3)
        _RoundedButton(btns, text="完整运行", command=self._full_run,
                       width=88).pack(side="left", padx=3)
        _RoundedButton(nfoot, text="＋ 系统操作", command=self._add_system_step,
                       width=200).pack(pady=(8, 0))
        # 普通模式日志区(adb 操作等)
        log_wrap = tk.Frame(normal, bg=THEME["surface"])
        log_wrap.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 8))
        tk.Label(log_wrap, text="操作日志", bg=THEME["surface"], fg=THEME["txt2"],
                 font=FONT_CAPTION).pack(anchor="w", pady=(0, 2))
        self._normal_log_text = tk.Text(log_wrap, height=5, font=FONT_MONO, state="disabled",
                                        bg=THEME["term_bg"], fg=THEME["term_fg"],
                                        wrap="word", relief="flat", bd=0,
                                        highlightthickness=1,
                                        highlightbackground=THEME["border2"])
        self._normal_log_text.pack(fill="x")

        # ---- AI 模式 Tab ----
        ai_tab.rowconfigure(1, weight=1)
        ai_tab.columnconfigure(0, weight=1)
        ttk.Label(ai_tab, text="智能定位", style="Caption.TLabel").grid(row=0, column=0, sticky="w")
        self._ai_chat = _ChatPane(ai_tab, width=360, height=180)
        self._ai_chat.frame.grid(row=1, column=0, sticky="nsew", pady=(2, 4))
        # 候选卡片
        self._ai_cand_frame = tk.Frame(ai_tab, bg=THEME["surface"])
        self._ai_cand_info_var = tk.StringVar(value="")
        tk.Label(self._ai_cand_frame, textvariable=self._ai_cand_info_var,
                 bg=THEME["surface"], fg=THEME["txt"], font=FONT_MONO,
                 justify="left", anchor="w").pack(fill="x", padx=8, pady=(6, 2))
        cand_btns = tk.Frame(self._ai_cand_frame, bg=THEME["surface"])
        cand_btns.pack(fill="x", padx=6, pady=(0, 4))
        self._ai_confirm_btn = _RoundedButton(cand_btns, text="保存为步骤",
                                              command=self._ai_confirm, style="primary")
        self._ai_confirm_btn.pack(side="left", padx=2)
        self._ai_deny_btn = _RoundedButton(cand_btns, text="不是",
                                           command=lambda: self._ai_cycle_candidate(1))
        self._ai_deny_btn.pack(side="left", padx=2)
        self._ai_next_btn = _RoundedButton(cand_btns, text="换候选",
                                           command=lambda: self._ai_cycle_candidate(1))
        self._ai_next_btn.pack(side="left", padx=2)
        # 输入行
        self._ai_input_row = tk.Frame(ai_tab, bg=THEME["surface"])
        self._ai_input_row.grid(row=2, column=0, sticky="ew", pady=(2, 2))
        self._ai_input_row.columnconfigure(0, weight=1)
        self._ai_input_var = tk.StringVar()
        self._ai_entry = tk.Entry(self._ai_input_row, textvariable=self._ai_input_var,
                                  bg=THEME["surface2"], fg=THEME["txt"], font=FONT_BTN,
                                  bd=0, relief="flat", insertbackground=THEME["txt"],
                                  highlightthickness=1,
                                  highlightbackground=THEME["border2"],
                                  highlightcolor=THEME["border2"])
        self._ai_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4), ipady=8)
        self._ai_entry.bind("<Return>", lambda e: self._ai_send())
        self._ai_mode_var = tk.StringVar(value="全流程")
        ttk.Combobox(self._ai_input_row, textvariable=self._ai_mode_var,
                     values=["全流程", "仅OCR", "仅VLM"], width=8,
                     state="readonly").grid(row=0, column=1, padx=(0, 4))
        _RoundedButton(self._ai_input_row, text="发送", command=self._ai_send,
                       style="primary").grid(row=0, column=2)
        # AI 日志
        ttk.Label(ai_tab, text="AI 日志 · 思考过程", style="Caption.TLabel").grid(
            row=3, column=0, sticky="w", pady=(6, 2))
        aibox = ttk.Frame(ai_tab)
        aibox.grid(row=4, column=0, sticky="ew")
        aibox.columnconfigure(0, weight=1)
        self._ai_log_text = tk.Text(aibox, height=6, font=FONT_MONO, state="disabled",
                                    bg=THEME["term_bg"], fg=THEME["term_fg"],
                                    wrap="word", relief="flat", bd=0)
        self._ai_log_text.grid(row=0, column=0, sticky="ew")
        ai_ls = ttk.Scrollbar(aibox, orient="vertical", command=self._ai_log_text.yview)
        ai_ls.grid(row=0, column=1, sticky="ns")
        self._ai_log_text.configure(yscrollcommand=ai_ls.set)

        # ---- 执行模式 Tab ----
        exec_tab.columnconfigure(0, weight=1)
        exec_tab.rowconfigure(2, weight=1)
        ttk.Label(exec_tab, text="勾选测试用例执行(每个对应一个录制脚本)",
                  style="Caption.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        eh = tk.Frame(exec_tab, bg=THEME["surface"])
        eh.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        eh.columnconfigure(0, weight=1)
        _RoundedButton(eh, text="↻ 刷新", command=self._exec_refresh).pack(side="left")
        _RoundedButton(eh, text="全选", command=lambda: self._exec_set_all(True)).pack(side="left", padx=4)
        _RoundedButton(eh, text="全不选", command=lambda: self._exec_set_all(False)).pack(side="left")
        self._exec_tree = ttk.Treeview(
            exec_tab, columns=("sel", "name", "status"),
            show="headings", height=10, selectmode="none")
        self._exec_tree.heading("sel", text="选")
        self._exec_tree.heading("name", text="用例")
        self._exec_tree.heading("status", text="状态")
        self._exec_tree.column("sel", width=36, anchor="center", stretch=False)
        self._exec_tree.column("name", width=180, anchor="w")
        self._exec_tree.column("status", width=70, anchor="center", stretch=False)
        self._exec_tree.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        es = ttk.Scrollbar(exec_tab, orient="vertical", command=self._exec_tree.yview)
        es.grid(row=2, column=1, sticky="ns")
        self._exec_tree.configure(yscrollcommand=es.set)
        self._exec_tree.bind("<Button-1>", self._on_exec_tree_click)
        self._exec_cases: Dict[str, Dict[str, Any]] = {}
        # 用例详情(点击用例时填充)
        self._exec_detail_frame = ttk.Frame(exec_tab)
        self._exec_detail_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self._exec_detail_frame.grid_remove()
        self._exec_detail_iid: Optional[str] = None
        self._exec_detail_info = tk.StringVar(value="")
        ttk.Label(self._exec_detail_frame, textvariable=self._exec_detail_info,
                  style="Caption.TLabel", wraplength=300, justify="left").pack(anchor="w")
        self._exec_detail_steps = tk.Text(self._exec_detail_frame, height=4, font=FONT_MONO,
                                          state="disabled", wrap="none", relief="flat", bd=0)
        self._exec_detail_steps.pack(fill="x", pady=(2, 2))
        _RoundedButton(self._exec_detail_frame, text="← 加载到普通模式",
                       command=self._exec_load_to_normal).pack(anchor="w")
        # 执行按钮行
        efoot = tk.Frame(exec_tab, bg=THEME["surface"])
        efoot.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        efoot.columnconfigure(0, weight=1)
        efoot.columnconfigure(1, weight=1)
        self._exec_btn = _RoundedButton(efoot, text="▶ 执行选中用例",
                                        command=self._exec_checked, style="primary", width=130)
        self._exec_btn.grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._exec_open_btn = _RoundedButton(efoot, text="打开报告目录",
                                             command=self._exec_open_report, width=120)
        self._exec_open_btn.grid(row=0, column=1, sticky="e", padx=(4, 0))
        # 执行模式日志
        self._exec_log_text = tk.Text(exec_tab, height=5, font=FONT_MONO, state="disabled",
                                      bg=THEME["term_bg"], fg=THEME["term_fg"], wrap="word",
                                      relief="flat", bd=0)
        self._exec_log_text.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        self._exec_busy = False
        self._last_plan_dir: Optional[str] = None
        self._exec_refresh()

        # ==================================================================
        # 底部 Console:常驻执行日志(全宽)
        # ==================================================================
        console = tk.Frame(root, bg=THEME["term_bg"], height=130,
                           highlightthickness=1, highlightbackground=THEME["border"])
        console.grid(row=2, column=0, columnspan=3, sticky="ew")
        console.grid_propagate(False)
        ch = tk.Frame(console, bg=THEME["term_bg"])
        ch.pack(fill="x", padx=12, pady=(4, 2))
        tk.Label(ch, text="●", fg=THEME["green"], bg=THEME["term_bg"],
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(ch, text=" 执行日志 · Console", fg=THEME["txt2"],
                 bg=THEME["term_bg"], font=FONT_CAPTION).pack(side="left")
        self.log_text = tk.Text(console, height=6, font=FONT_MONO, state="disabled",
                                bg=THEME["term_bg"], fg=THEME["term_fg"], wrap="word",
                                relief="flat", bd=0)
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # 默认步骤等待时间(画布点击追加步骤时使用)
        self.before_var = tk.StringVar(value="0")
        self.after_var = tk.StringVar(value="10")

    # ------------------------------------------------------------------
    # 画布显示:网格显隐 / 缩放
    # ------------------------------------------------------------------
    def _toggle_grid(self) -> None:
        """网格显隐:关掉时显示无网格的原始截图(点击换算不受影响)。"""
        if self._grid_on.get():
            self._render_canvas(self.grid_bgr)
        elif self._latest_raw is not None:
            self._render_canvas(self._latest_raw)
        else:
            self._log("[提示] 暂无无网格原图,保持当前显示")

    def _zoom(self, factor: float) -> None:
        """缩放画布显示。self.scale 同时用于点击坐标换算,故缩放后点击依旧准确。"""
        base_h, base_w = self.grid_bgr.shape[:2]
        new_scale = min(2.0, max(0.2, self.scale * factor))
        self.scale = new_scale
        self._render_canvas(self.grid_bgr if self._grid_on.get() else self._latest_raw)
        self._zoom_label.configure(text=f"{int(round(new_scale * 100))}%")

    def _zoom_reset(self) -> None:
        """回到 1:1(按当前网格图原始像素 1:1 显示,超出部分由画布裁切)。"""
        self.scale = 1.0
        self._render_canvas(self.grid_bgr if self._grid_on.get() else self._latest_raw)
        self._zoom_label.configure(text="100%")

    def _render_canvas(self, bgr) -> None:
        """按当前 self.scale 把 bgr 渲染到画布(PhotoImage 不支持任意缩放,先落盘再加载)。

        注意:先 create_image 再赋 self.photo,并保留旧引用,
        否则旧 PhotoImage 被 GC 时 Tcl 侧图片同步销毁,画布会闪黑图(实测踩过)。
        """
        import tempfile
        if bgr is None:
            return
        h, w = bgr.shape[:2]
        self.disp_w, self.disp_h = max(1, int(w * self.scale)), max(1, int(h * self.scale))
        disp = cv2.resize(bgr, (self.disp_w, self.disp_h), interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(), "grapemobile_grid_display.png")
        cv2.imwrite(self._tmp, disp)
        new_photo = tk.PhotoImage(file=self._tmp)
        old_photo = getattr(self, "photo", None)
        self.canvas.configure(width=self.disp_w, height=self.disp_h)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=new_photo)
        self.photo = new_photo
        self._photo_ref = old_photo  # 保持引用,避免画布闪黑

    # ------------------------------------------------------------------
    # 步骤编辑
    # ------------------------------------------------------------------
    def _on_canvas_click(self, event: tk.Event) -> None:
        """画布点击 -> 换算为格子编号 -> 实时截"执行前图" -> 追加 tap 步骤。"""
        if self._running:
            return
        # AI 候选预览态:点击 = 把候选点跳到光标处(微调)
        if self._ai_overlay_active:
            self._ai_adjust_candidate(self._event_to_raw(event), jump=True)
            return
        orig_x = event.x / self.scale - MARGIN
        orig_y = event.y / self.scale - MARGIN
        meta_w = self.grid_bgr.shape[1] - 2 * MARGIN
        meta_h = self.grid_bgr.shape[0] - 2 * MARGIN
        if not (0 <= orig_x < meta_w and 0 <= orig_y < meta_h):
            return  # 点在边距标签区,忽略
        col = int(orig_x // self.cell_size)
        row = int(orig_y // self.cell_size)
        cell = f"{col_name(col)}{row + 1}"
        step = {
            "type": "tap", "cell": cell,
            "delay_before": self._spin_value(self.before_var),
            "wait_after": self._spin_value(self.after_var),
            "before_image": "",
            "after_image": "",
        }
        # 关键:点击瞬间立即从设备实时截图作为"执行前标准图",
        # 不用自动刷新缓存(缓存最坏滞后 1 秒,可能录到跳转中间帧)
        before_rel = self._capture_anchor_now("before")
        if before_rel:
            step["before_image"] = before_rel
        else:
            self._log("[提示] 未能实时截图,本步无执行前标准图(执行时跳过开始条件校验)")
        self.steps.append(step)
        self._refresh_list()
        self._select_and_preview(len(self.steps) - 1)
        self._list_see_end()

    def _anchor_dir(self) -> str:
        """当前脚本的标准图目录:scripts/<脚本名>/images/。"""
        return os.path.join(SCRIPTS_DIR, self.script_name, "images")

    def _max_existing_anchor_seq(self, prefix: str) -> int:
        """扫描 images/ 中指定前缀(before_/after_)图片的最大编号,无则 0。"""
        anchor_dir = self._anchor_dir()
        if not os.path.isdir(anchor_dir):
            return 0
        seq = 0
        for n in os.listdir(anchor_dir):
            stem = os.path.splitext(n)[0]
            if stem.startswith(prefix):
                tail = stem[len(prefix):]
                if tail.isdigit():
                    seq = max(seq, int(tail))
        return seq

    def _capture_anchor_now(self, kind: str) -> str:
        """
        立即从设备实时截图,存为当前脚本的标准图。

        Args:
            kind: "before" -> images/before_NNNN.png;
                  "after"  -> images/after_NNNN.png。

        Returns:
            相对脚本 JSON 目录的路径(如 "images/before_0001.png");失败返回 ""。
            设备不可用(静态图模式)时 before 回退为最近缓存/screenshot.png,
            after 不允许回退(执行后图必须是真实跳转后的画面)。
        """
        anchor_dir = self._anchor_dir()
        os.makedirs(anchor_dir, exist_ok=True)
        if kind == "before":
            self._before_seq = self._max_existing_anchor_seq("before_")
        else:
            self._after_seq = self._max_existing_anchor_seq("after_")
        seq_attr = "_before_seq" if kind == "before" else "_after_seq"
        setattr(self, seq_attr, getattr(self, seq_attr) + 1)
        name = f"{kind}_{getattr(self, seq_attr):04d}.png"
        final_path = os.path.join(anchor_dir, name)

        captured = False
        if self.device_id:
            try:
                self.config(cursor="watch")
                self.update_idletasks()
                if self._anchor_client is None:
                    self._anchor_client = AdbClient()
                self._anchor_client.attach(self.device_id)
                self._anchor_client.screenshot(save_path=final_path)
                captured = os.path.isfile(final_path) and os.path.getsize(final_path) > 0
            except AdbError as exc:
                self._log(f"[提示] 实时截图失败: {exc}")
            finally:
                self.config(cursor="")

        if not captured and kind == "before":
            # 静态模式回退:用最近缓存帧(无设备场景仍可手工编排)
            raw = self._latest_raw
            if raw is None and os.path.isfile(RAW_IMAGE):
                raw = cvio.imread(RAW_IMAGE)
            if raw is not None:
                captured = cvio.imwrite(final_path, raw)

        if not captured:
            setattr(self, seq_attr, getattr(self, seq_attr) - 1)
            return ""
        return f"images/{name}"

    def _recapture_anchor(self, field: str) -> None:
        """
        为当前选中步骤重拍标准图(before_image / after_image)。

        典型用途:页面跳转稳定后选中上一步,点"📷 截执行后图"标记完成条件;
        或录制时机不对时重拍执行前图。
        """
        if self._running:
            return
        sel = self._list_sel()
        if len(sel) != 1:
            messagebox.showinfo("请选择步骤", "请先在步骤列表中选中一个步骤。")
            return
        kind = "before" if field == "before_image" else "after"
        rel = self._capture_anchor_now(kind)
        if rel:
            self.steps[sel[0]][field] = rel
            self._refresh_list()
            self._select_and_preview(sel[0])
            self._log(f"第 {sel[0] + 1} 步 {self.steps[sel[0]]['cell']} "
                      f"{'执行前' if kind == 'before' else '执行后'}标准图已更新: {rel}")
        else:
            messagebox.showwarning("截图失败", "未能从设备截图,请确认设备连接正常。")

    # ------------------------------------------------------------------
    # 标准图预览(before/after)
    # ------------------------------------------------------------------
    def _current_script_dir(self) -> str:
        """标准图基准目录:已保存脚本取其 JSON 所在目录;未保存草稿取 scripts/untitled/。"""
        if self.script_path:
            return os.path.dirname(os.path.abspath(self.script_path))
        return os.path.join(SCRIPTS_DIR, DRAFT_SCRIPT_NAME)

    def _make_placeholder_image(self) -> "tk.PhotoImage":
        """生成深灰占位图(保持预览区尺寸稳定,无标准图时显示)。"""
        img = np.full((EXPECT_IMG_H, EXPECT_IMG_W, 3), 40, dtype=np.uint8)
        tmp = os.path.join(tempfile.gettempdir(), "grapemobile_expect_placeholder.png")
        cv2.imwrite(tmp, img)
        return tk.PhotoImage(file=tmp)

    def _set_preview_image(self, which: str, img: Optional[np.ndarray]) -> None:
        """把图像等比缩放居中贴入固定预览框(which: "before"/"after")。"""
        photo_attr = f"_{which}_photo"
        label = self.before_img_label if which == "before" else self.after_img_label
        if img is None:
            photo = self._make_placeholder_image()
        else:
            h, w = img.shape[:2]
            scale = min(EXPECT_IMG_W / w, EXPECT_IMG_H / h)
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            disp = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            canvas_img = np.full((EXPECT_IMG_H, EXPECT_IMG_W, 3), 34, dtype=np.uint8)
            y0 = (EXPECT_IMG_H - new_h) // 2
            x0 = (EXPECT_IMG_W - new_w) // 2
            canvas_img[y0:y0 + new_h, x0:x0 + new_w] = disp
            tmp = os.path.join(tempfile.gettempdir(),
                               f"grapemobile_{which}_display.png")
            cv2.imwrite(tmp, canvas_img)
            # 必须保留 PhotoImage 引用,否则会被 Tk 垃圾回收导致不显示
            photo = tk.PhotoImage(file=tmp)
        setattr(self, photo_attr, photo)
        label.configure(image=photo)

    # 系统操作选项:显示文案 -> (类型, 附加参数)
    _SYSOP_OPTIONS = (
        ("关闭 App(force-stop)", ("app_stop", "")),
        ("启动 App", ("app_start", "")),
        ("清除 App 数据(pm clear)", ("app_clear", "")),
        ("Home 键", ("keyevent", "home")),
        ("返回键", ("keyevent", "back")),
        ("最近任务键", ("keyevent", "recents")),
    )

    def _add_system_step(self) -> None:
        """弹窗选择系统操作(App 生命周期/系统按键)并追加为步骤。"""
        if self._running:
            return
        dlg = tk.Toplevel(self)
        dlg.title("添加系统操作")
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())

        ttk.Label(dlg, text="操作:").grid(row=0, column=0, padx=(12, 4),
                                          pady=(12, 4), sticky="e")
        labels = [o[0] for o in self._SYSOP_OPTIONS]
        op_var = tk.StringVar(value=labels[0])
        ttk.Combobox(dlg, textvariable=op_var, values=labels,
                     state="readonly", width=24).grid(
            row=0, column=1, columnspan=2, padx=(0, 12), pady=(12, 4))

        pkg_frm = ttk.Frame(dlg)
        pkg_frm.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=4)
        ttk.Label(pkg_frm, text="包名(留空=执行时\n自动识别前台App):").pack(side="left")
        pkg_var = tk.StringVar()
        ttk.Entry(pkg_frm, textvariable=pkg_var, width=22).pack(
            side="left", padx=4)

        def _detect_pkg() -> None:
            if not self.device_id:
                messagebox.showinfo("无设备", "当前未连接设备,无法识别包名", parent=dlg)
                return
            try:
                c = AdbClient()
                c.attach(self.device_id)
                pkg_var.set(c.current_package())
            except Exception as exc:
                messagebox.showerror("识别失败", str(exc), parent=dlg)

        ttk.Button(pkg_frm, text="识别当前", command=_detect_pkg).pack(side="left")

        # keyevent 选项隐藏包名行,app 操作显示
        def _sync_pkg_row(_event=None) -> None:
            choice = dict((o[0], o[1]) for o in self._SYSOP_OPTIONS)[op_var.get()]
            if choice[0] == "keyevent":
                pkg_frm.grid_remove()
            else:
                pkg_frm.grid()

        dlg.bind("<<ComboboxSelected>>", _sync_pkg_row)

        def _ok() -> None:
            stype, arg = dict((o[0], o[1]) for o in self._SYSOP_OPTIONS)[op_var.get()]
            if stype == "keyevent":
                step = {"type": "keyevent", "key": arg}
            else:
                step = {"type": stype, "package": pkg_var.get().strip()}
            step.update({
                "cell": "", "x": None, "y": None,
                "delay_before": 0,
                "wait_after": 2 if stype in APP_OP_TYPES else 0,
                "before_image": "", "after_image": "", "desc": "",
            })
            self.steps.append(step)
            self._refresh_list()
            self._select_and_preview(len(self.steps) - 1)
            self._list_see_end()
            self._log(f"[添加] {self._step_action_text(step)}")
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=2, column=0, columnspan=3, pady=(8, 12))
        ttk.Button(btns, text="添加", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: _ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()

    @staticmethod
    def _step_action_text(step: Dict[str, Any]) -> str:
        """步骤的人类可读动作文案(日志/列表共用)。"""
        stype = step.get("type")
        if stype == "keyevent":
            return f"系统按键 {KEYEVENT_LABELS.get(step.get('key'), step.get('key'))}"
        if stype in APP_OP_TYPES:
            action = {"app_stop": "关闭 App", "app_start": "启动 App",
                      "app_clear": "清除 App 数据"}[stype]
            pkg = step.get("package") or "自动识别前台 App"
            return f"{action}: {pkg}"
        return f"{stype} {step.get('cell')}"

    def _show_expect_preview(self, index: Optional[int]) -> None:
        """
        显示指定步骤的执行前/执行后标准图。

        执行前:本步 before_image;
        执行后:本步 after_image;缺省时提示"默认=下一步执行前图"(引擎自动链接)。
        """
        if index is None or not (0 <= index < len(self.steps)):
            self._preview_index = None
            self._detail_info_var.set("未选中步骤")
            self._set_preview_image("before", None)
            self._set_preview_image("after", None)
            self.before_status_var.set("未选中步骤")
            self.after_status_var.set("未选中步骤")
            return

        self._preview_index = index
        step = self.steps[index]
        stype = step.get("type")

        # ---- 非 UI 步骤:无格子/标准图,详情展示动作与包名/按键 ----
        if stype in APP_OP_TYPES or stype == "keyevent":
            title = f"第 {index + 1} 步"
            self._detail_info_var.set(f"{title}  {self._step_action_text(step)}")
            self._detail_before_var.set(f"{step.get('delay_before', 0):g}")
            self._detail_after_var.set(f"{step.get('wait_after', 0):g}")
            self._set_preview_image("before", None)
            self._set_preview_image("after", None)
            self.before_status_var.set(f"{title}\n系统操作,无标准图")
            self.after_status_var.set(f"{title}\n系统操作,无标准图")
            return

        title = f"第 {index + 1} 步 {step['cell']}"

        # ---- 步骤详情(可编辑前/后等待) ----
        is_input = stype == "input"
        kind = f"输入 \"{step.get('text', '')}\"" if is_input else "点击"
        coord = (f"({step['x']}, {step['y']})"
                 if step.get("x") is not None and step.get("y") is not None else "-")
        self._detail_info_var.set(
            f"第 {index + 1} 步  {kind}  {step['cell']}  {coord}")
        self._detail_before_var.set(f"{step.get('delay_before', 0):g}")
        self._detail_after_var.set(f"{step.get('wait_after', 0):g}")

        # 执行前
        before_rel = step.get("before_image", "")
        if not before_rel:
            self._set_preview_image("before", None)
            self.before_status_var.set(f"{title}\n无执行前标准图")
        else:
            img = cvio.imread(os.path.join(self._current_script_dir(), before_rel)) \
                if not os.path.isabs(before_rel) else cvio.imread(before_rel)
            self._set_preview_image("before", img)
            self.before_status_var.set(
                f"{title}\n{before_rel}" if img is not None
                else f"{title}\n执行前图缺失:\n{before_rel}")

        # 执行后
        after_rel = step.get("after_image", "")
        if after_rel:
            img = cvio.imread(os.path.join(self._current_script_dir(), after_rel)) \
                if not os.path.isabs(after_rel) else cvio.imread(after_rel)
            self._set_preview_image("after", img)
            self.after_status_var.set(
                f"{title}\n{after_rel}" if img is not None
                else f"{title}\n执行后图缺失:\n{after_rel}")
        else:
            # 缺省时引擎自动用"下一步执行前图"作为本步完成条件
            self._set_preview_image("after", None)
            if index + 1 < len(self.steps):
                nxt = self.steps[index + 1]
                self.after_status_var.set(
                    f"{title}\n默认=第 {index + 2} 步 {nxt['cell']} 的执行前图")
            else:
                self.after_status_var.set(f"{title}\n无执行后标准图(末步点完即结束)")

    @staticmethod
    def _spin_value(var: tk.StringVar) -> float:
        """读取 Spinbox 数值,非法时按 0 处理。"""
        try:
            return max(0.0, float(var.get()))
        except ValueError:
            return 0.0

    def _list_sel(self) -> List[int]:
        """当前选中步骤下标列表(Treeview 版 curselection)。"""
        return [int(iid) for iid in self.listbox.selection()]

    def _list_select(self, i: int) -> None:
        """程序化选中第 i 步并滚动到可见。"""
        self.listbox.selection_set(str(i))
        self.listbox.see(str(i))

    def _list_see_end(self) -> None:
        """滚动到列表末行(新增步骤后调用)。"""
        kids = self.listbox.get_children()
        if kids:
            self.listbox.see(kids[-1])

    def _on_select(self, _event: tk.Event) -> None:
        """选中步骤时显示其预期画面。"""
        sel = self._list_sel()
        if len(sel) == 1:
            self._show_expect_preview(sel[0])

    def _apply_step_detail(self) -> None:
        """把右侧详情面板中的前/后等待写回当前选中步骤。"""
        i = self._preview_index
        if i is None or not (0 <= i < len(self.steps)):
            messagebox.showinfo("未选中", "请先在列表中选中一个步骤")
            return
        # 强制焦点提交 Spinbox 正在编辑的值
        self.focus_set()
        self.update_idletasks()
        db = self._spin_value(self._detail_before_var)
        da = self._spin_value(self._detail_after_var)
        self.steps[i]["delay_before"] = db
        self.steps[i]["wait_after"] = da
        self._refresh_list()
        self._select_and_preview(i)
        self._log(f"[修改] 第 {i + 1} 步等待: 前 {db:g}s / 后 {da:g}s")

    def _on_step_double_click(self, event: tk.Event) -> None:
        """双击列表行:弹窗直接修改该步骤的前/后等待时间。"""
        # 以双击位置为准确定行(双击时选中事件可能尚未更新)
        iid = self.listbox.identify_row(event.y)
        if not iid:
            return
        i = int(iid)
        if not (0 <= i < len(self.steps)):
            return
        self.listbox.selection_set(iid)
        self._edit_step_waits(i)

    def _edit_step_waits(self, i: int) -> None:
        """小弹窗编辑第 i 步:系统操作步骤可改包名/按键,所有步骤可改前/后等待。"""
        step = self.steps[i]
        stype = step.get("type")
        is_sysop = stype in APP_OP_TYPES or stype == "keyevent"
        dlg = tk.Toplevel(self)
        dlg.title(f"第 {i + 1} 步 - {self._step_action_text(step)}")
        dlg.resizable(False, False)
        dlg.transient(self.winfo_toplevel())

        row = 0
        if is_sysop:
            if stype == "keyevent":
                ttk.Label(dlg, text="按键:").grid(row=row, column=0, padx=10,
                                                   pady=(12, 4), sticky="e")
                key_var = tk.StringVar(value=step.get("key", "home"))
                ttk.Combobox(dlg, textvariable=key_var,
                             values=list(KEYEVENT_MAP), state="readonly",
                             width=10).grid(row=row, column=1, padx=10,
                                            pady=(12, 4), sticky="w")
            else:
                ttk.Label(dlg, text="包名(留空=\n自动识别):").grid(
                    row=row, column=0, padx=10, pady=(12, 4), sticky="e")
                pkg_var = tk.StringVar(value=step.get("package", ""))
                ttk.Entry(dlg, textvariable=pkg_var, width=20).grid(
                    row=row, column=1, padx=10, pady=(12, 4), sticky="w")
            row += 1

        ttk.Label(dlg, text="执行前等待(秒):").grid(
            row=row, column=0, padx=10, pady=(12, 4) if row == 0 else 4,
            sticky="e")
        before_var = tk.StringVar(value=f"{step.get('delay_before', 0):g}")
        ttk.Spinbox(dlg, from_=0, to=600, width=8, textvariable=before_var).grid(
            row=row, column=1, padx=10, pady=(12, 4) if row == 0 else 4,
            sticky="w")
        row += 1
        ttk.Label(dlg, text="执行后等待(秒):").grid(row=row, column=0, padx=10,
                                                    pady=4, sticky="e")
        after_var = tk.StringVar(value=f"{step.get('wait_after', 0):g}")
        ttk.Spinbox(dlg, from_=0, to=600, width=8, textvariable=after_var).grid(
            row=row, column=1, padx=10, pady=4, sticky="w")
        row += 1

        btns = ttk.Frame(dlg)
        btns.grid(row=row, column=0, columnspan=2, pady=(8, 12))

        def _ok() -> None:
            # 强制焦点提交 Spinbox 正在编辑的值
            dlg.focus_set()
            dlg.update_idletasks()
            if is_sysop:
                if stype == "keyevent":
                    step["key"] = key_var.get()
                else:
                    step["package"] = pkg_var.get().strip()
            step["delay_before"] = self._spin_value(before_var)
            step["wait_after"] = self._spin_value(after_var)
            self._refresh_list()
            self._list_select(i)
            self._show_expect_preview(i)
            self._log(f"[修改] 第 {i + 1} 步 {self._step_action_text(step)}:"
                      f" 前 {step['delay_before']:g}s / 后 {step['wait_after']:g}s")
            dlg.destroy()

        ttk.Button(btns, text="确定", command=_ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: _ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.grab_set()  # 模态

    def _select_and_preview(self, index: Optional[int]) -> None:
        """程序化选中某步并刷新预期画面(selection_set 不触发 <<TreeviewSelect>>)。"""
        self.listbox.selection_remove(self.listbox.selection())
        if index is not None and 0 <= index < len(self.steps):
            self._list_select(index)
            self._show_expect_preview(index)
        else:
            self._show_expect_preview(None)

    def _select_card(self, index: int) -> None:
        """点击步骤卡片:选中并显示预览,同步高亮两处卡片。"""
        self._select_and_preview(index)
        self._refresh_step_cards()
        self._refresh_normal_cards()

    def _delete_step(self, index: int) -> None:
        """删除指定步骤(卡片右上角 ✕)。"""
        if not (0 <= index < len(self.steps)):
            return
        del self.steps[index]
        self._refresh_list()
        nxt = min(index, len(self.steps) - 1) if self.steps else None
        self._select_and_preview(nxt)

    def _delete_selected(self) -> None:
        sel = self._list_sel()
        for i in reversed(sel):
            del self.steps[i]
        self._refresh_list()
        # 删除后选中相邻步骤(优先同位置),没有则清空预览
        nxt = min(sel[0], len(self.steps) - 1) if self.steps and sel else None
        self._select_and_preview(nxt)

    def _move(self, delta: int) -> None:
        sel = self._list_sel()
        if len(sel) != 1:
            return
        i = sel[0]
        j = i + delta
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self._refresh_list()
            self._select_and_preview(j)

    def _clear(self) -> None:
        self.steps.clear()
        self._refresh_list()
        self._select_and_preview(None)

    def _refresh_list(self) -> None:
        """重建步骤列表(Treeview 紧凑行:编号/操作/目标/等待)与中栏步骤卡片。"""
        self.listbox.delete(*self.listbox.get_children())
        for i, step in enumerate(self.steps):
            before_wait = step.get("delay_before", 0)
            after_wait = step.get("wait_after", 0)
            waits = f"前{before_wait:g}/后{after_wait:g}"
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            tags = ["odd" if i % 2 else "even"]
            if stype == "input":
                title = desc or "输入文本"
                meta = f"{step['cell']} \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                title = self._step_action_text(step)
                meta = "系统操作"
                tags.append("sysop")
            else:
                title = desc or "点击"
                cell = step.get("cell", "")
                x, y = step.get("x"), step.get("y")
                meta = f"{cell}" + (f"({x},{y})" if x is not None and y is not None else "")
            self.listbox.insert("", "end", iid=str(i), tags=tags,
                                 values=(i + 1, title, meta, waits))
        # 同步刷新中栏步骤卡片与右栏普通模式紧凑卡片
        self._refresh_step_cards()
        self._refresh_normal_cards()

    def _refresh_step_cards(self) -> None:
        """重建中栏步骤卡片列表(Canvas 滚动区 + 每张卡片一个 Frame)。"""
        for child in self._step_cards_inner.winfo_children():
            child.destroy()
        if not self.steps:
            tk.Label(self._step_cards_inner, text="(暂无步骤,点击左侧画面添加)",
                     fg=THEME["txt3"], bg=THEME["surface"],
                     font=FONT_CAPTION).pack(pady=20)
            return
        for i, step in enumerate(self.steps):
            card = tk.Frame(self._step_cards_inner, bg=THEME["bg2"],
                            highlightthickness=1, highlightbackground=THEME["border"])
            card.pack(fill="x", pady=(0, 6), padx=1)
            # 选中态高亮
            if self._preview_index == i:
                card.configure(highlightbackground=THEME["accent"],
                               highlightthickness=2)
            # 编号徽章(圆角)
            num = _RoundedBadge(card, text=str(i + 1), size=22, radius=5)
            num.grid(row=0, column=0, rowspan=2, padx=6, pady=6, sticky="n")
            # 主体
            body = tk.Frame(card, bg=THEME["bg2"])
            body.grid(row=0, column=1, sticky="ew", pady=(6, 0))
            card.grid_columnconfigure(1, weight=1)
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            if stype == "input":
                ttl = desc or f"输入 \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                ttl = self._step_action_text(step)
            else:
                ttl = desc or f"点击 {step.get('cell', '')}"
            tk.Label(body, text=ttl, fg=THEME["txt"], bg=THEME["bg2"],
                     font=FONT_UI, anchor="w").pack(side="left", fill="x", expand=True)
            # 锚点标记
            has_anchor = step.get("before_image") or step.get("after_image")
            if has_anchor:
                tk.Label(body, text="◎锚点", fg=THEME["accent"], bg=THEME["bg2"],
                         font=FONT_CAPTION).pack(side="left", padx=(4, 0))
            # 副标题(格子/坐标/系统操作)
            sub = tk.Frame(card, bg=THEME["bg2"])
            sub.grid(row=1, column=1, sticky="w", padx=(0, 6), pady=(2, 0))
            if stype == "input":
                sub_text = f"格子 {step.get('cell', '')}"
            elif stype in APP_OP_TYPES or stype == "keyevent":
                sub_text = "系统操作"
            else:
                x, y = step.get("x"), step.get("y")
                sub_text = f"格子 {step.get('cell', '')}" + (
                    f" · ({x},{y})" if x is not None and y is not None else "")
            tk.Label(sub, text=sub_text, fg=THEME["txt2"], bg=THEME["bg2"],
                     font=FONT_MONO, anchor="w").pack(side="left")
            # 等待 chip
            meta = tk.Frame(card, bg=THEME["bg2"])
            meta.grid(row=2, column=1, sticky="w", padx=(0, 6), pady=(4, 6))
            tk.Label(meta, text=f"前{step.get('delay_before', 0):g}s",
                     fg=THEME["txt2"], bg=THEME["surface2"], font=FONT_MONO,
                     padx=4, pady=1).pack(side="left", padx=(0, 4))
            tk.Label(meta, text=f"后{step.get('wait_after', 0):g}s",
                     fg=THEME["txt2"], bg=THEME["surface2"], font=FONT_MONO,
                     padx=4, pady=1).pack(side="left")
            # 操作按钮(上移/下移/删除):24x24 圆角小方块
            acts = tk.Frame(card, bg=THEME["bg2"])
            acts.grid(row=0, column=2, rowspan=3, padx=(0, 6), pady=6, sticky="ne")
            for icon, cmd in [
                ("△", lambda idx=i: self._move(-1)),
                ("▽", lambda idx=i: self._move(1)),
                ("✕", lambda idx=i: self._delete_step(idx)),
            ]:
                _RoundedButton(acts, text=icon, command=cmd,
                               width=24, height=24, radius=5).pack(side="left", padx=1)
            # 交互:点击卡片选中;双击改等待
            for w in (card, body, sub, meta, num):
                w.bind("<Button-1>", lambda _e, idx=i: self._select_card(idx))
                w.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_step_waits(idx))

    def _refresh_normal_cards(self) -> None:
        """重建右栏普通模式的紧凑步骤卡片(nstep:编号+标题+副标题+上移/下移/删除)。"""
        for child in self._normal_inner.winfo_children():
            child.destroy()
        if not self.steps:
            tk.Label(self._normal_inner, text="(暂无步骤)",
                     fg=THEME["txt3"], bg=THEME["surface"],
                     font=FONT_CAPTION).pack(pady=16)
            return
        for i, step in enumerate(self.steps):
            stype = step.get("type")
            desc = (step.get("desc") or "").strip()
            if stype == "input":
                ttl = desc or f"输入 \"{step.get('text', '')}\""
            elif stype in APP_OP_TYPES or stype == "keyevent":
                ttl = self._step_action_text(step)
            else:
                ttl = desc or f"点击 {step.get('cell', '')}"
            # 副标题
            if stype == "input":
                sub = f"格子 {step.get('cell', '')}"
            elif stype in APP_OP_TYPES or stype == "keyevent":
                sub = "系统操作"
            else:
                x, y = step.get("x"), step.get("y")
                sub = f"格子 {step.get('cell', '')}" + (
                    f" · ({x},{y})" if x is not None and y is not None else "")
            selected = (self._preview_index == i)
            card = tk.Frame(self._normal_inner, bg=THEME["bg2"],
                            highlightthickness=1,
                            highlightbackground=THEME["accent"] if selected else THEME["border"])
            card.pack(fill="x", pady=(0, 6))
            card.columnconfigure(1, weight=1)  # 主体列自动伸缩
            # 编号徽章(圆角)
            num = _RoundedBadge(card, text=str(i + 1), size=22, radius=5)
            num.grid(row=0, column=0, rowspan=2, padx=(6, 8), pady=8, sticky="w")
            # 主体
            body = tk.Frame(card, bg=THEME["bg2"])
            body.grid(row=0, column=1, rowspan=2, sticky="ew", pady=7)
            tk.Label(body, text=ttl, bg=THEME["bg2"], fg=THEME["txt"],
                     font=FONT_UI_BOLD, anchor="w").pack(fill="x")
            tk.Label(body, text=sub, bg=THEME["bg2"], fg=THEME["txt2"],
                     font=FONT_MONO, anchor="w").pack(fill="x", pady=(1, 0))
            # 操作图标按钮(上移/下移/删除):24x24 圆角小方块
            acts = tk.Frame(card, bg=THEME["bg2"])
            acts.grid(row=0, column=2, rowspan=2, padx=(4, 6), pady=6, sticky="e")
            for icon, cmd in [
                ("△", lambda idx=i: self._move(-1)),
                ("▽", lambda idx=i: self._move(1)),
                ("✕", lambda idx=i: self._delete_step(idx)),
            ]:
                _RoundedButton(acts, text=icon, command=cmd,
                               width=24, height=24, radius=5).pack(side="left", padx=1)
            # 点击卡片选中(同步 listbox 选中态,触发预览)
            for w in (card, num, body):
                w.bind("<Button-1>", lambda _e, idx=i: self._select_card(idx))
                w.bind("<Double-Button-1>", lambda _e, idx=i: self._edit_step_waits(idx))

    def _refresh(self) -> None:
        """重新从设备截取当前画面并刷新网格显示(步骤列表保留)。"""
        if self._running:
            return
        try:
            self._capture_from_device()
        except Exception as exc:
            messagebox.showerror("截图失败", str(exc))
            return
        self._log(f"已刷新截图: {self.device_id} "
                  f"({self.grid_bgr.shape[1]}x{self.grid_bgr.shape[0]}, 格子 {self.cell_size}px)")

    # ------------------------------------------------------------------
    # 保存 / 加载
    # ------------------------------------------------------------------
    # Windows 文件名非法字符
    _INVALID_NAME_CHARS = set('\\/:*?"<>|')

    def _save(self) -> None:
        """
        保存脚本:以"脚本名称"输入框为唯一来源,直接存到 scripts/<名称>.json。

        - 名称为空:提示用户先输入名称,不保存;
        - 含非法字符:提示修正;
        - 与当前内存脚本名不同:
            * 新建草稿(untitled):移动锚点文件夹到新名称;
            * 已加载脚本另存新名:复制锚点文件夹(保留原脚本完整);
        - 目标 JSON 已存在且不是当前脚本:覆盖前二次确认;
        - 保存成功后清空名称输入框并回到"新建脚本"状态,
          确保下次保存必须输入新名称。
        """
        name = self.script_name_var.get().strip()
        if not name:
            messagebox.showwarning("缺少脚本名称", "请先在「脚本名称」输入框中输入名称,再点击保存。")
            return
        bad = sorted(set(name) & self._INVALID_NAME_CHARS)
        if bad:
            messagebox.showwarning("名称非法", f"脚本名称不能包含字符: {' '.join(bad)}")
            return

        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        target_path = os.path.join(SCRIPTS_DIR, name, f"{name}.json")
        same_script = self.script_path is not None and \
            os.path.normcase(os.path.abspath(target_path)) == \
            os.path.normcase(os.path.abspath(self.script_path))

        if not same_script and os.path.isfile(target_path):
            if not messagebox.askyesno("覆盖确认", f"脚本 {name} 已存在,是否覆盖?"):
                return

        if name != self.script_name:
            # 新建草稿:move(untitled 是临时目录);已加载脚本另存:copy(保留原脚本)
            move = self.script_path is None
            if not self._migrate_anchor_folder(self.script_name, name, move=move):
                messagebox.showerror(
                    "保存失败",
                    f"脚本文件夹 scripts/{name}/ 已存在且非空,\n"
                    f"请换一个脚本名,或先清空该文件夹。",
                )
                return
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        try:
            save_steps(self.steps, target_path)
            n_before = sum(1 for s in self.steps if s.get("before_image"))
            n_after = sum(1 for s in self.steps if s.get("after_image"))
            self._log(f"脚本已保存: {target_path}(共 {len(self.steps)} 步,"
                      f"执行前图 {n_before} 张,执行后图 {n_after} 张)")
        except StepError as exc:
            messagebox.showerror("保存失败", str(exc))
            return

        self._cleanup_orphan_anchors()
        # 成功后回到"新建脚本"状态:清空名称,后续录制进入新的 untitled 草稿
        self._reset_to_new_script()

    def _reset_to_new_script(self) -> None:
        """保存成功后重置:清空名称输入框/脚本绑定/步骤列表,准备录制下一个脚本。"""
        self.script_name_var.set("")
        self.script_name = DRAFT_SCRIPT_NAME
        self.script_path = None
        self.steps.clear()
        self._before_seq = 0
        self._after_seq = 0
        self._refresh_list()
        self._select_and_preview(None)

    def _cleanup_orphan_anchors(self) -> None:
        """删除当前脚本 images/ 中未被任何步骤 before/after 引用的图片(孤儿文件)。"""
        anchor_dir = self._anchor_dir()
        if not os.path.isdir(anchor_dir):
            return
        referenced_names = {
            os.path.basename(s.get(field, ""))
            for s in self.steps for field in ("before_image", "after_image")
            if s.get(field)
        }
        for name in os.listdir(anchor_dir):
            if name.lower().endswith(".png") and name not in referenced_names:
                try:
                    os.remove(os.path.join(anchor_dir, name))
                except OSError:
                    pass

    def _migrate_anchor_folder(self, old_name: str, new_name: str, move: bool = True) -> bool:
        """
        保存为不同脚本名时迁移脚本文件夹 scripts/<old>/ -> scripts/<new>/。

        JSON 中锚点路径固定为 "images/NNNN.png"(相对 JSON 所在目录),
        文件夹改名后无需改写。

        Args:
            move: True 整个文件夹移动(新建草稿 untitled -> 正式名,草稿目录消失);
                  False 仅复制 images/ 子目录(已加载脚本另存新名,
                  原脚本的 <old>.json 不会被带进新文件夹)。

        Returns:
            True 完成迁移(或无图片可迁);False 目标文件夹已存在非空内容,中止。
        """
        import shutil

        if old_name == new_name:
            return True
        old_dir = os.path.join(SCRIPTS_DIR, old_name)
        new_dir = os.path.join(SCRIPTS_DIR, new_name)
        if os.path.isdir(new_dir) and os.listdir(new_dir):
            return False
        os.makedirs(new_dir, exist_ok=True)
        if move:
            # 草稿目录里只有 images/,整体移动
            old_images = os.path.join(old_dir, "images")
            if os.path.isdir(old_images):
                shutil.move(old_images, os.path.join(new_dir, "images"))
            if os.path.isdir(old_dir):
                try:
                    os.rmdir(old_dir)  # 草稿目录此时应为空
                except OSError:
                    pass
        else:
            # 另存:只复制图片,不复制原 JSON
            old_images = os.path.join(old_dir, "images")
            if os.path.isdir(old_images):
                shutil.copytree(old_images, os.path.join(new_dir, "images"),
                                dirs_exist_ok=True)
        self.script_name = new_name
        # 编号计数器在下次截图时按 images/ 实际内容扫描,无需在此同步
        return True

    def _load(self) -> None:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        path = filedialog.askopenfilename(
            initialdir=SCRIPTS_DIR, filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        self._load_script(os.path.abspath(path))

    def _load_script(self, path: str) -> bool:
        """加载指定脚本 JSON 到普通模式(执行模式"加载到普通模式"复用)。成功返回 True。"""
        try:
            self.steps = load_steps(path)
        except StepError as exc:
            messagebox.showerror("加载失败", str(exc))
            return False
        # 切换当前脚本:锚点文件夹与 JSON 同名,锚点相对路径以 JSON 所在目录为基准
        self.script_path = path
        self.script_name = os.path.splitext(os.path.basename(path))[0]
        self.script_name_var.set(self.script_name)  # 回填名称输入框
        script_dir = os.path.dirname(path)
        # 编号计数器在下次截图时按 images/ 实际内容扫描接续
        self._refresh_list()
        self._select_and_preview(0 if self.steps else None)  # 加载后展示第一步标准图
        missing = [s[field] for s in self.steps for field in ("before_image", "after_image")
                   if s.get(field)
                   and not os.path.isfile(os.path.join(script_dir, s[field]))]
        self._log(f"脚本已加载: {path}(共 {len(self.steps)} 步)")
        if missing:
            self._log(f"[提示] {len(missing)} 张标准图缺失,对应位置将跳过画面校验/退化为固定等待")
        return True

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _run(self) -> None:
        if self._running:
            return
        if not self.steps:
            messagebox.showwarning("无步骤", "请先在左侧图片上点击格子添加步骤")
            return
        # 选中且仅选中一个步骤时:只执行该步;否则从头顺序执行全部
        sel = self._list_sel()
        if len(sel) == 1:
            self._run_offset = sel[0]
            run_steps = [self.steps[sel[0]]]
        else:
            self._run_offset = 0
            run_steps = self.steps
        self._start_run(run_steps, record=False)

    def _full_run(self) -> None:
        """完整运行:忽略选中态从头跑全部步骤,并自动录制 + 渲染回放视频。"""
        if self._running:
            return
        if not self.steps:
            messagebox.showwarning("无步骤", "请先在左侧图片上点击格子添加步骤")
            return
        self._run_offset = 0
        self._start_run(list(self.steps), record=True)

    def _start_run(self, run_steps: List[Dict[str, Any]], record: bool) -> None:
        """_run/_full_run 共用的执行入口:停刷新 -> 建 runner -> 起后台线程。"""
        # 先进入运行态:阻止新的自动刷新启动;再等在途刷新落盘完成,
        # 避免读到半写入的 screenshot.png / 与执行轮询争抢 adb
        self._running = True
        self.run_btn.configure(state="disabled")
        self.full_run_btn.configure(state="disabled")
        deadline = time.monotonic() + 3.0
        while self._auto_busy and time.monotonic() < deadline:
            self.update()
            time.sleep(0.03)
        if self._auto_busy:
            # 极端情况下刷新仍未结束:执行线程与刷新各用独立临时文件,不再互相破坏,
            # 但 screenshot.png 可能正在写——执行不依赖它(坐标只取分辨率),继续即可
            self._log("[提示] 画面刷新尚未完成,已忽略其结果继续执行")
        try:
            runner = StepRunner.from_capture_artifacts(_BASE_DIR)
        except StepError as exc:
            self._running = False
            self.run_btn.configure(state="normal")
            self.full_run_btn.configure(state="normal")
            messagebox.showerror("执行环境缺失", str(exc))
            return
        # 锚点图相对路径以当前脚本 JSON 所在目录为基准(未保存草稿用 scripts/untitled/)
        runner.script_dir = (os.path.dirname(os.path.abspath(self.script_path))
                             if self.script_path
                             else os.path.join(SCRIPTS_DIR, DRAFT_SCRIPT_NAME))
        if record:
            self._log(f"开始完整运行脚本 [{self.script_name}],共 {len(run_steps)} 步"
                      f"(自动录制+回放),设备: {runner.device_id}")
        elif self._run_offset:
            self._log(f"开始执行选中步骤 第 {self._run_offset + 1} 步,"
                      f"设备: {runner.device_id}")
        else:
            self._log(f"开始执行脚本 [{self.script_name}],共 {len(self.steps)} 步,"
                      f"设备: {runner.device_id}")
        threading.Thread(target=self._run_worker,
                         args=(runner, run_steps, record), daemon=True).start()

    def _run_worker(self, runner: StepRunner, run_steps: List[Dict[str, Any]],
                    record: bool) -> None:
        recorder = None
        try:
            if record:
                from recording import Recorder
                recorder = Recorder.create(device_id=runner.device_id)
                rec_path = recorder.recording_path
                self._bridge.post(lambda p=rec_path: self._log(f"[录制] 运行目录: {p}"))
            runner.run(run_steps, on_event=self._on_step_event, recorder=recorder)
        except Exception as exc:  # AdbError / StepError 等,统一在 UI 层提示
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._log(f"[失败] {m}"))
        finally:
            self._bridge.post(lambda: self._run_finished(recorder))

    def _run_finished(self, recorder=None) -> None:
        self._running = False
        self.run_btn.configure(state="normal")
        self.full_run_btn.configure(state="normal")
        # 完整运行:录制已收尾(recording.json 落盘),后台渲染回放视频
        if recorder is not None and os.path.isfile(recorder.recording_path):
            rec_path = recorder.recording_path
            threading.Thread(target=self._render_worker,
                             args=(rec_path,), daemon=True).start()

    def _render_worker(self, recording_path: str) -> None:
        """后台渲染回放视频(测试报告),结果回执行日志。"""
        try:
            self._bridge.post(lambda: self._log("[回放] 正在渲染回放视频…"))
            from replay_render import render_to_video
            out = render_to_video(recording_path)
            size_mb = os.path.getsize(out) / 1048576
            self._bridge.post(
                lambda o=out, s=size_mb: self._log(f"[回放] 视频已生成: {o} ({s:.2f} MB)"))
        except Exception as exc:
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._log(f"[回放] 渲染失败: {m}"))

    # ------------------------------------------------------------------
    # 执行模式(测试计划:批量勾选 scripts 下的用例执行)
    # ------------------------------------------------------------------
    def _exec_log(self, msg: str) -> None:
        """写入执行模式日志区。"""
        self._exec_log_text.configure(state="normal")
        self._exec_log_text.insert("end", msg + "\n")
        self._exec_log_text.see("end")
        self._exec_log_text.configure(state="disabled")

    def _exec_refresh(self) -> None:
        """扫描 SCRIPTS_DIR(递归,支持多层文件夹):每个 JSON 文件视为一条用例。"""
        self._exec_cases.clear()
        if os.path.isdir(SCRIPTS_DIR):
            for dirpath, dirnames, filenames in os.walk(SCRIPTS_DIR):
                dirnames[:] = [d for d in dirnames if d != "images"]  # 跳过锚点图目录
                for fn in sorted(filenames):
                    if not fn.lower().endswith(".json"):
                        continue
                    path = os.path.abspath(os.path.join(dirpath, fn))
                    self._exec_cases[path] = {
                        "name": os.path.splitext(fn)[0],
                        "path": path,
                        "dir": dirpath,
                        "rel": os.path.relpath(path, SCRIPTS_DIR),
                    }
        # 重建树(保留旧勾选状态)
        checked = {iid for iid in self._exec_tree.get_children()
                   if self._exec_tree.set(iid, "sel") == "☑"}
        self._exec_tree.delete(*self._exec_tree.get_children())
        for iid, case in self._exec_cases.items():
            mark = "☑" if iid in checked else "☐"
            self._exec_tree.insert("", "end", iid=iid, values=(
                mark, case["name"], "待执行"))
        self._exec_log(f"[用例] 扫描完成,共 {len(self._exec_cases)} 条"
                       f"(目录: {SCRIPTS_DIR})")

    def _on_exec_tree_click(self, event) -> None:
        """点击用例行:右侧展示详情;点"选"或"用例"列同时切换勾选状态。"""
        region = self._exec_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self._exec_tree.identify_column(event.x)
        iid = self._exec_tree.identify_row(event.y)
        if not iid:
            return
        self._show_exec_case_detail(iid)
        if col in ("#1", "#2"):  # 点"选"或"用例"列都可切换勾选
            cur = self._exec_tree.set(iid, "sel")
            self._exec_tree.set(iid, "sel", "☐" if cur == "☑" else "☑")

    def _show_exec_case_detail(self, iid: str) -> None:
        """执行模式:展示用例名称/路径/步骤摘要,并展开详情面板。"""
        case = self._exec_cases.get(iid)
        if case is None:
            return
        self._exec_detail_iid = iid
        try:
            steps = load_steps(case["path"])
        except Exception as exc:
            self._exec_detail_info.set(
                f"用例: {case['name']} | 路径: {case['rel']} | 加载失败: {exc}")
            steps = []
        else:
            self._exec_detail_info.set(
                f"用例: {case['name']} | 路径: {case['rel']} | 共 {len(steps)} 步")
        lines = []
        for i, s in enumerate(steps, 1):
            if s["type"] == "input":
                action = f"输入[{s.get('text', '')}]"
            elif s["type"] in APP_OP_TYPES or s["type"] == "keyevent":
                action = self._step_action_text(s)
            else:
                action = f"点击 {s['cell']}"
            desc = s.get("desc") or ""
            lines.append(f"{i}. {action} 前{s['delay_before']}s/后{s['wait_after']}s"
                         + (f"  {desc}" if desc else ""))
        self._exec_detail_steps.configure(state="normal")
        self._exec_detail_steps.delete("1.0", "end")
        self._exec_detail_steps.insert("end", "\n".join(lines) if lines else "(无步骤)")
        self._exec_detail_steps.configure(state="disabled")
        self._exec_detail_frame.grid()  # 点击用例时展开详情

    def _exec_load_to_normal(self) -> None:
        """把执行模式当前详情的用例加载到普通模式并切换过去。"""
        case = self._exec_cases.get(self._exec_detail_iid or "")
        if case is None:
            messagebox.showinfo("未选中用例", "请先在左侧列表点击一条用例")
            return
        if self._load_script(case["path"]):
            self._switch_tab(1)  # 切到普通模式

    def _exec_open_report(self) -> None:
        """用系统资源管理器打开最近一次测试计划的结果目录。"""
        if self._last_plan_dir and os.path.isdir(self._last_plan_dir):
            os.startfile(self._last_plan_dir)  # Windows
        else:
            messagebox.showinfo("无报告", "尚未执行过测试计划")

    def _exec_toggle_all(self) -> None:
        """全选/全不选:有任一未勾选则全部勾选,否则全部取消。"""
        iids = self._exec_tree.get_children()
        target = "☐" if all(self._exec_tree.set(i, "sel") == "☑" for i in iids) else "☑"
        for iid in iids:
            self._exec_tree.set(iid, "sel", target)

    def _exec_set_all(self, on: bool) -> None:
        """全选(on=True)或全不选(on=False)。"""
        target = "☑" if on else "☐"
        for iid in self._exec_tree.get_children():
            self._exec_tree.set(iid, "sel", target)

    def _exec_set_status(self, iid: str, status: str) -> None:
        self._exec_tree.set(iid, "status", status)

    def _exec_checked(self) -> None:
        """执行所有勾选用例:每次生成一个测试计划目录 runner/result/<plan-id>/。"""
        if self._running or self._exec_busy:
            return
        checked = [iid for iid in self._exec_tree.get_children()
                   if self._exec_tree.set(iid, "sel") == "☑"]
        if not checked:
            messagebox.showwarning("未选择用例", "请先勾选要执行的用例")
            return
        self._exec_busy = True
        self._running = True  # 与普通模式运行互斥
        self._exec_btn.configure(state="disabled")
        plan_id = time.strftime("plan-%Y%m%d-%H%M%S")
        plan_dir = os.path.join(RESULT_DIR, plan_id)
        os.makedirs(plan_dir, exist_ok=True)
        self._last_plan_dir = plan_dir
        self._exec_log(f"[计划] {plan_id}:共 {len(checked)} 条用例,结果目录 {plan_dir}")
        for iid in self._exec_tree.get_children():  # 重置所有状态列
            self._exec_tree.set(iid, "status", "待执行")
        threading.Thread(target=self._exec_worker,
                         args=(checked, plan_dir, plan_id), daemon=True).start()

    def _exec_worker(self, checked: List[str], plan_dir: str, plan_id: str) -> None:
        """后台顺序执行勾选用例;每条用例录制+回放,最后写 plan.json 汇总。"""
        results: List[Dict[str, Any]] = []
        try:
            runner = StepRunner.from_capture_artifacts(_BASE_DIR)
        except Exception as exc:
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._exec_log(f"[失败] 环境异常: {m}"))
            self._bridge.post(lambda: self._exec_finished())
            return
        self._bridge.post(
            lambda d=runner.device_id: self._exec_log(f"[设备] {d}"))

        for seq, iid in enumerate(checked, 1):
            case = self._exec_cases[iid]
            name = case["name"]
            self._bridge.post(lambda i=iid: self._exec_set_status(i, "执行中…"))
            self._bridge.post(lambda n=name, s=seq, t=len(
                checked): self._exec_log(f"—— [{s}/{t}] 用例 [{n}] 开始 ——"))
            t0 = time.time()
            rec = None
            error: Optional[str] = None
            rec_rel = replay_rel = ""
            try:
                steps = load_steps(case["path"])
                if not steps:
                    raise StepError("用例没有任何步骤")
                from recording import Recorder
                rec = Recorder.create(device_id=runner.device_id,
                                      runs_dir=plan_dir, run_id=name)
                rec_rel = os.path.relpath(rec.recording_path, plan_dir)
                runner.script_dir = case["dir"]  # 锚点图以用例目录为基准
                runner.run(steps,
                           on_event=self._make_exec_event(name, len(steps)),
                           recorder=rec)  # 异常时 recorder 内部已 fail+finish
                status = "passed"
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                status = "failed"
            duration = round(time.time() - t0, 1)

            # 回放视频(测试报告):recording.json 已落盘才渲染
            if rec is not None and os.path.isfile(rec.recording_path):
                try:
                    from replay_render import render_to_video
                    out = render_to_video(rec.recording_path)
                    replay_rel = os.path.relpath(out, plan_dir)
                    self._bridge.post(lambda n=name, o=replay_rel: self._exec_log(
                        f"[{n}] 回放视频: {o}"))
                except Exception as exc:
                    self._bridge.post(lambda n=name, m=str(
                        exc): self._exec_log(f"[{n}] 回放渲染失败: {m}"))

            mark = "✓ 成功" if status == "passed" else "✗ 失败"
            self._bridge.post(lambda i=iid, m=mark: self._exec_set_status(i, m))
            if error:
                self._bridge.post(lambda n=name, m=error: self._exec_log(
                    f"[{n}] 失败原因: {m}"))
            self._bridge.post(lambda n=name, s=status, d=duration: self._exec_log(
                f"[{n}] 结束: {s},耗时 {d}s"))
            results.append({
                "name": name,
                "path": case["rel"],
                "status": status,
                "error": error,
                "duration_sec": duration,
                "recording": rec_rel,
                "replay": replay_rel,
            })

        # 汇总测试计划 plan.json
        passed = sum(1 for r in results if r["status"] == "passed")
        summary = {
            "version": 1,
            "plan_id": plan_id,
            "created_at": time.time(),
            "created_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device_id": runner.device_id,
            "cases": results,
            "summary": {
                "total": len(results),
                "passed": passed,
                "failed": len(results) - passed,
            },
        }
        plan_path = os.path.join(plan_dir, "plan.json")
        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._bridge.post(lambda m=str(
                exc): self._exec_log(f"[失败] plan.json 写入失败: {m}"))
        self._bridge.post(
            lambda p=passed, t=len(results), d=plan_dir: self._exec_log(
                f"[计划完成] 通过 {p}/{t},结果目录: {d}"))
        self._bridge.post(lambda: self._exec_finished())

    def _make_exec_event(self, case_name: str, total: int):
        """生成某条用例的步骤事件回调:日志带用例名前缀,写执行模式日志区。"""
        def emit(idx: int, _total: int, msg: str) -> None:
            self._bridge.post(
                lambda n=case_name, i=idx, m=msg: self._exec_log(f"[{n}][{i}/{total}] {m}"))
        return emit

    def _exec_finished(self) -> None:
        self._exec_busy = False
        self._running = False
        self._exec_btn.configure(state="normal")
        if self._last_plan_dir:  # 执行完成后允许打开报告目录
            self._exec_open_btn.configure(state="normal")

    def _on_step_event(self, idx: int, total: int, msg: str) -> None:
        # StepRunner 在后台线程,日志经 bridge 切回主线程刷新
        self._bridge.post(lambda: self._log(f"[{idx}/{total}] {msg}"))
        # 开始执行某步(点击/输入)时,联动选中该步并显示它的预期画面
        # (单步执行时 idx 从 1 重新计,需加 _run_offset 映射回完整列表下标)
        if msg.startswith("点击 ") or msg.startswith("输入 "):
            offset = getattr(self, "_run_offset", 0)
            self._bridge.post(lambda: self._preview_running_step(idx - 1 + offset))

    def _preview_running_step(self, index: int) -> None:
        """执行过程中高亮当前步并展示其预期画面(不改编辑用的等待输入框)。"""
        self.listbox.selection_remove(self.listbox.selection())
        if 0 <= index < len(self.steps):
            self._list_select(index)
            self._show_expect_preview(index)

    def _log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # 同步到普通模式 Tab 内的日志区
        if hasattr(self, "_normal_log_text"):
            self._normal_log_text.configure(state="normal")
            self._normal_log_text.insert("end", msg + "\n")
            self._normal_log_text.see("end")
            self._normal_log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # AI 模式
    # ------------------------------------------------------------------
    def _ai_trace(self, msg: str) -> None:
        """aiclient trace 钩子:AI 请求/回复(思考过程)经 bridge 进 AI 日志区。"""
        self._bridge.post(lambda: self._ai_log(msg))

    def _ai_log(self, msg: str) -> None:
        """写入 AI 模式日志区。"""
        self._ai_log_text.configure(state="normal")
        self._ai_log_text.insert("end", msg + "\n")
        self._ai_log_text.see("end")
        self._ai_log_text.configure(state="disabled")

    def _ai_chat_append(self, speaker: str, text: str) -> None:
        """向 AI 对话区追加一条消息(气泡式:AI 左灰底 / 用户右紫底)。"""
        if speaker == "AI":
            self._ai_chat.add_ai(text)
        elif speaker == "系统":
            self._ai_chat.add_sys(text)
        else:
            self._ai_chat.add_user(text)

    def _show_cand_card(self, visible: bool) -> None:
        """候选确认卡片整体显隐(信息 + 三个按钮同进同退)。

        AI Tab 内部用 pack,卡片必须插在对话区之后、输入行之前,
        故用 before=self._ai_input_row 固定顺序。
        """
        if visible:
            self._ai_cand_frame.pack(side="top", fill="x", pady=(0, 4),
                                     before=self._ai_input_row)
        else:
            self._ai_cand_frame.pack_forget()

    def _set_cand_btns(self, confirm: bool, deny: bool, next_btn: bool) -> None:
        """候选卡片内按钮显隐(卡片已显示时调用)。"""
        for btn, flag in ((self._ai_confirm_btn, confirm),
                          (self._ai_deny_btn, deny),
                          (self._ai_next_btn, next_btn)):
            if flag:
                btn.pack(side="left", padx=4, pady=2)
            else:
                btn.pack_forget()

    def _switch_tab(self, idx: int) -> None:
        """切换右栏 Tab:高亮选中按钮 + 显示对应面板。"""
        if not 0 <= idx < len(self._tab_panels):
            return
        self._tab_idx = idx
        for i, (tf, lbl, uline) in enumerate(self._tab_buttons):
            if i == idx:
                tf.configure(bg=THEME["surface"])
                lbl.configure(bg=THEME["surface"], fg=THEME["txt"])
                uline.configure(bg=THEME["accent"])
            else:
                tf.configure(bg=THEME["bg2"])
                lbl.configure(bg=THEME["bg2"], fg=THEME["txt2"])
                uline.configure(bg=THEME["bg2"])
        for i, panel in enumerate(self._tab_panels):
            if i == idx:
                panel.grid()
            else:
                panel.grid_remove()
        self._on_tab_changed()

    def _on_tab_changed(self, event=None) -> None:
        """Tab 切换:普通模式清除 AI 候选叠加;AI 模式重绘当前候选。"""
        idx = self._tab_idx
        if idx == 0:  # AI 模式
            if (self._ai_result is not None
                    and self._ai_result.top_candidates and not self._ai_busy):
                self._show_candidate(self._ai_cand_idx, silent=True)
        elif idx == 1:  # 普通模式
            self._clear_ai_overlay()

    _AI_MODE_MAP = {"全流程": "full", "仅OCR": "ocr_only", "仅VLM": "vlm_only"}

    def _ai_send(self) -> None:
        """用户发送指令 -> 新定位管线(OCR+VLM+模板 融合验证)。"""
        instruction = self._ai_input_var.get().strip()
        if not instruction or self._ai_busy:
            return
        self._ai_input_var.set("")
        self._ai_instruction = instruction  # 确认后随步骤保存为 desc

        # 清除上一次的候选预览
        self._show_cand_card(False)
        self._ai_cand_info_var.set("")
        self._clear_ai_overlay()
        self._ai_result = None
        self._ai_adjusted = False
        self._ai_drag_last = None

        self._ai_chat_append("用户", instruction)
        mode_label = self._ai_mode_var.get()
        self._ai_chat_append(
            "AI", f"[{mode_label}] 正在定位(意图解析→候选→融合→验证),约 10~30 秒…")
        self._ai_log(f"用户指令: {instruction} ({mode_label})")

        if not os.path.isfile(RAW_IMAGE):
            self._ai_chat_append("AI", "未找到截图,请先点击「⟳ 刷新截图」。")
            return

        self._ai_busy = True
        threading.Thread(
            target=self._ai_locate_worker,
            args=(instruction, mode_label),
            daemon=True,
        ).start()

    def _ai_locate_worker(self, instruction: str, mode_label: str) -> None:
        """后台线程:跑完整定位管线,结果回主线程展示。"""
        try:
            from locate.config import load_config
            from locate import pipeline as locate_pipeline

            image = cvio.imread(RAW_IMAGE)
            if image is None:
                raise RuntimeError(f"无法读取截图: {RAW_IMAGE}")
            cfg = load_config()
            cfg.mode = self._AI_MODE_MAP[mode_label]
            # "仅OCR"语义:完全不调 VLM(含验证),纯本地快速路径
            if cfg.mode == "ocr_only":
                cfg.verify.enabled = False
            result = locate_pipeline.locate(image, instruction, cfg)
            self._bridge.post(lambda: self._ai_locate_done(result))
        except Exception as exc:  # 管线内任何异常都回显,不崩 GUI
            # except 块结束后 exc 会被 Python 删除,lambda 延迟执行取不到,
            # 必须先落地为普通局部变量再捕获
            err_msg = str(exc)
            self._bridge.post(lambda m=err_msg: self._ai_log(f"定位异常: {m}"))
            self._bridge.post(lambda m=err_msg: self._ai_locate_error(m))

    def _ai_locate_error(self, msg: str) -> None:
        self._ai_busy = False
        self._ai_chat_append("AI", f"定位失败: {msg}")

    def _ai_locate_done(self, result) -> None:
        """管线返回 -> 展示第 1 个候选。"""
        self._ai_busy = False
        self._ai_result = result
        self._ai_cand_idx = 0

        if not result.top_candidates:
            self._ai_chat_append(
                "AI", f"没找到匹配元素({result.reason or '无候选'})。"
                      "可换个说法,或直接在普通模式点格子。")
            self._ai_log(f"定位失败: {result.reason}")
            return

        if not result.ok:
            self._ai_chat_append(
                "AI", f"⚠ 候选均未通过验证({result.reason}),"
                      f"仍列出 {len(result.top_candidates)} 个候选供人工裁决。")
        self._show_candidate(0)

    def _show_candidate(self, idx: int, silent: bool = False) -> None:
        """在画布叠加显示第 idx 个融合候选的框+十字。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        self._ai_cand_idx = idx
        cand = result.top_candidates[idx]
        n = len(result.top_candidates)
        self._draw_ai_overlay(cand, [c for j, c in enumerate(result.top_candidates) if j != idx])

        x, y = int(cand.point[0]), int(cand.point[1])
        cell = self._cell_for_point(x, y)
        if self._ai_adjusted:
            ver = "🔧已手动微调"
        elif cand.verified:
            ver = "✓验证通过"
        elif cand.verify_reason:
            ver = "✗验证未过"
        else:
            ver = "未验证"
        sources = "+".join(sorted(set(cand.sources or [cand.source])))
        detail = "" if self._ai_adjusted else (cand.verify_reason or cand.text or "")
        info = (f"候选 {idx + 1}/{n}  格子 {cell}  像素({x},{y})\n"
                f"来源: {sources}  置信度: {cand.confidence:.2f}  {ver}\n"
                f"{detail}")
        self._ai_cand_info_var.set(info.strip())
        if not silent:
            self._ai_chat_append("AI", f"候选 {idx + 1}/{n}: 格子 {cell} ({x},{y}) "
                                       f"[{sources}] 置信度 {cand.confidence:.2f} {ver}"
                                       "(自动刷新已暂停,点「✓ 保存为步骤」确认,"
                                       "或「⟳ 刷新截图」丢弃)")
            self._ai_log(f"候选{idx + 1}/{n} {cell} ({x},{y}) {sources} "
                         f"conf={cand.confidence:.2f} {ver}")
        self._show_cand_card(True)
        self._set_cand_btns(True, True, n > 1)

    def _ai_cycle_candidate(self, delta: int) -> None:
        """切换到下一个/上一个候选。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        n = len(result.top_candidates)
        if n <= 1:
            # 只有一个候选时「✗ 不是」= 丢弃本次提案:
            # 否则候选一直挂着,自动刷新会一直处于暂停状态
            self._reset_ai_proposal_ui()
            self._ai_chat_append("AI", "好,已丢弃这个候选。可以重新输入指令,"
                                       "或切到普通模式点格子手动指定。")
            return
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._show_candidate((self._ai_cand_idx + delta) % n)

    def _ai_confirm(self) -> None:
        """确认当前候选 -> 带像素坐标的 tap/input 步骤入库(指令文本存为 desc)。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        cand = result.top_candidates[self._ai_cand_idx]
        x, y = int(cand.point[0]), int(cand.point[1])
        cell = self._cell_for_point(x, y)

        # 意图:点击 or 输入(定位管线已解析,随 result 带出)
        intent = getattr(result, "intent", None)
        action = getattr(intent, "action", "tap") or "tap"
        input_text = (getattr(intent, "input_text", "") or "").strip()

        step = {
            "type": "input" if (action == "input" and input_text) else "tap",
            "cell": cell,
            "x": x, "y": y,  # 精确像素坐标,回放时优先于格子换算
            "delay_before": self._spin_value(self.before_var),
            "wait_after": self._spin_value(self.after_var),
            "before_image": "",
            "after_image": "",
            "desc": (self._ai_instruction or "").strip(),  # 用户指令文本,便于阅读脚本
        }
        if step["type"] == "input":
            step["text"] = input_text
        before_rel = self._capture_anchor_now("before")
        if before_rel:
            step["before_image"] = before_rel
        self.steps.append(step)
        self._refresh_list()
        self._select_and_preview(len(self.steps) - 1)
        self._list_see_end()

        if step["type"] == "input":
            warn = ""
            if any(ord(ch) > 127 for ch in input_text):
                warn = "。⚠ 含非 ASCII 字符(如中文),adb input text 可能无法输入"
            self._ai_chat_append(
                "AI", f"已保存为脚本步骤 {len(self.steps)}: 在 {cell} ({x},{y}) "
                      f"输入 {input_text!r}{warn}。"
                      "可继续输入下一条指令,或到普通模式查看/运行脚本。")
            self._ai_log(f"确认 -> 步骤 {len(self.steps)}: input {cell} ({x},{y}) "
                         f"text={input_text!r}")
        else:
            self._ai_chat_append("AI", f"已保存为脚本步骤 {len(self.steps)}: "
                                       f"点击 {cell} ({x},{y})。"
                                       "可继续输入下一条指令,或到普通模式查看/运行脚本。")
            self._ai_log(f"确认 -> 步骤 {len(self.steps)}: {cell} ({x},{y})")
        self._reset_ai_proposal_ui()

    def _reset_ai_proposal_ui(self) -> None:
        """确认/切换截图后复位候选预览状态。"""
        self._clear_ai_overlay()
        self._ai_result = None
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._ai_cand_idx = 0
        self._ai_cand_info_var.set("")
        self._show_cand_card(False)

    def _clear_ai_overlay(self) -> None:
        """画布恢复纯网格图。"""
        if getattr(self, "_ai_overlay_active", False):
            self._set_grid(self.grid_bgr, self.cell_size)
            self._ai_overlay_active = False

    def _cell_for_point(self, x: int, y: int) -> str:
        """原始截图像素坐标 -> 格子编号(显示用,坐标仍以 x/y 为准)。"""
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        cols = max(1, int(np.ceil(raw_w / self.cell_size)))
        rows = max(1, int(np.ceil(raw_h / self.cell_size)))
        col = max(0, min(int(x // self.cell_size), cols - 1))
        row = max(0, min(int(y // self.cell_size), rows - 1))
        return f"{col_name(col)}{row + 1}"

    def _draw_ai_overlay(self, cand, others) -> None:
        """在网格图副本上画候选框(坐标是原始截图坐标,需加 MARGIN 边距)。"""
        img = self.grid_bgr.copy()

        def _shift(bbox):
            x1, y1, x2, y2 = [int(v) for v in bbox]
            return x1 + MARGIN, y1 + MARGIN, x2 + MARGIN, y2 + MARGIN

        # 其他候选:细品红框
        for c in others:
            cv2.rectangle(img, _shift(c.bbox)[:2], _shift(c.bbox)[2:],
                          (200, 0, 200), 1)
        # 当前候选:绿(验证通过)/橙(未过)粗框 + 十字
        color = (0, 200, 0) if cand.verified else (0, 165, 255)
        x1, y1, x2, y2 = _shift(cand.bbox)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
        cx, cy = int(cand.point[0]) + MARGIN, int(cand.point[1]) + MARGIN
        cv2.drawMarker(img, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 24, 2)
        # ASCII 标签(cv2 不渲染中文):格子号+置信度+来源
        x0, y0 = int(cand.point[0]), int(cand.point[1])
        label = f"{self._cell_for_point(x0, y0)} {cand.confidence:.2f}"
        cv2.rectangle(img, (x1, max(0, y1 - 18)), (x1 + 130, y1), color, -1)
        cv2.putText(img, label, (x1 + 4, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

        disp = cv2.resize(img, (self.disp_w, self.disp_h),
                          interpolation=cv2.INTER_AREA)
        self._tmp = os.path.join(tempfile.gettempdir(),
                                 "grapemobile_ai_preview.png")
        cv2.imwrite(self._tmp, disp)
        self.photo = tk.PhotoImage(file=self._tmp)
        self.canvas.configure(width=self.disp_w, height=self.disp_h)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self._ai_overlay_active = True

    # ---- 候选拖动微调 ----
    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._running or not self._ai_overlay_active:
            return
        self._ai_adjust_candidate(self._event_to_raw(event), jump=False)

    def _event_to_raw(self, event: tk.Event) -> tuple:
        """画布事件坐标 -> 原始截图坐标(去缩放/边距),并夹取到画面内。"""
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        x = int(round(event.x / self.scale - MARGIN))
        y = int(round(event.y / self.scale - MARGIN))
        return max(0, min(x, raw_w - 1)), max(0, min(y, raw_h - 1))

    def _ai_adjust_candidate(self, raw: tuple, jump: bool) -> None:
        """拖动/点击微调当前候选:jump=点击时框中心跳到光标;否则按增量平移。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        cand = result.top_candidates[self._ai_cand_idx]
        x1, y1, x2, y2 = [int(v) for v in cand.bbox]
        raw_w = self.grid_bgr.shape[1] - 2 * MARGIN
        raw_h = self.grid_bgr.shape[0] - 2 * MARGIN
        if jump:
            # 保持框大小,中心移到光标
            bw, bh = x2 - x1, y2 - y1
            nx = max(0, min(raw[0], raw_w - 1))
            ny = max(0, min(raw[1], raw_h - 1))
            nx1 = max(0, min(nx - bw // 2, raw_w - bw))
            ny1 = max(0, min(ny - bh // 2, raw_h - bh))
            cand.bbox = (nx1, ny1, nx1 + bw, ny1 + bh)
            cand.point = (nx, ny)
        else:
            if self._ai_drag_last is None:
                self._ai_drag_last = raw
                return
            dx, dy = raw[0] - self._ai_drag_last[0], raw[1] - self._ai_drag_last[1]
            if dx == 0 and dy == 0:
                return
            # 平移并夹取,框碰壁时贴边(点同步夹取)
            nx1 = max(0, min(x1 + dx, raw_w - (x2 - x1)))
            ny1 = max(0, min(y1 + dy, raw_h - (y2 - y1)))
            cand.bbox = (nx1, ny1, nx1 + (x2 - x1), ny1 + (y2 - y1))
            px = max(0, min(cand.point[0] + (nx1 - x1), raw_w - 1))
            py = max(0, min(cand.point[1] + (ny1 - y1), raw_h - 1))
            cand.point = (px, py)
        self._ai_drag_last = raw
        self._ai_adjusted = True
        # 微调后不再代表模型原始验证结论
        cand.verified = False
        cand.verify_reason = "用户手动微调"
        self._show_candidate(self._ai_cand_idx, silent=True)


def main() -> int:
    app = StepApp()
    # 用户在设备选择框取消,或首次截图失败:窗口已销毁,不进入主循环
    if getattr(app, "_startup_ok", False) and app.winfo_exists():
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
