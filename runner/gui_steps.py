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

运行前提:runner/device_id.txt 存在(python runner/capture.py 已跑过一次)。
启动:python runner/gui_steps.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

# 将 src/ 加入 sys.path(与其他 runner 脚本一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

import cvio  # noqa: E402  Unicode 路径兼容的图像读写(中文脚本名目录)
from grid import GridMarker, col_name  # noqa: E402
from steps import StepError, StepRunner, load_steps, save_steps  # noqa: E402
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
# 新建脚本未保存前使用的临时脚本名(锚点先存到 scripts/untitled/images/,保存时迁移)
DRAFT_SCRIPT_NAME = "untitled"
DEFAULT_CELL_SIZE = 30  # 实时截图生成网格时的默认格子边长(像素)
MARGIN = 40  # 与 GridMarker 默认边距一致(网格图在原图四周外扩 40px)
MAX_DISPLAY_WIDTH = 780   # 左侧网格图显示区域最大宽度(像素)
MAX_DISPLAY_HEIGHT = 900  # 最大高度(像素),竖屏截图缩放到屏幕能放下
# 右侧"预期画面"缩略图尺寸(锚点是全屏截图,before/after 各一张)
EXPECT_IMG_W = 168
EXPECT_IMG_H = 150


class StepApp(tk.Tk):
    """步骤编排主窗口:网格图选格 + 步骤列表 + 执行控制。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("GrapeMobile 步骤编排器")
        self.resizable(False, False)

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

        # ---- 加载网格:优先连接设备实时截图,失败时回退到静态网格图文件 ----
        self._startup_msg = ""
        try:
            self._capture_from_device()
            self._startup_msg = f"已连接设备 {self.device_id},当前为实时截图"
        except Exception as exc:
            if not self._load_from_file():
                messagebox.showerror(
                    "加载失败",
                    f"设备实时截图失败: {exc}\n\n且未找到静态网格图,请先运行:\n"
                    "python runner/capture.py",
                )
                self.destroy()
                return
            self._startup_msg = f"设备不可用({exc}),已回退到静态网格图 grid_screenshot.png"

        self._build_ui()
        self._log(self._startup_msg)
        # AI 模式欢迎语
        self._ai_chat_append("AI", "你好!我是 AI 步骤编排助手。\n"
                            "描述你想点击的元素,我会帮你找到对应的格子。\n"
                            "例如:点击kof图标、点击登录按钮")

        # ---- 伪实时:每秒自动重截(后台线程生产,主线程应用) ----
        self._auto_busy = False        # 是否有截图线程在跑(防止重叠)
        self._auto_fail_logged = False # 连续失败只记一次日志,避免刷屏
        self.after(1000, self._auto_tick)

    # ------------------------------------------------------------------
    # 自动刷新(伪实时)
    # ------------------------------------------------------------------
    def _auto_tick(self) -> None:
        """每秒触发一次:条件满足时启动后台截图线程。"""
        if (self._auto_var.get() and not self._running
                and not self._auto_busy and self.device_id):
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
        try:
            self.after(0, self._auto_apply, error, payload)
        except tk.TclError:
            pass  # 窗口已关闭

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
            StepError / AdbError: 无设备记录、设备离线或截图失败时抛出。
        """
        if not os.path.isfile(DEVICE_ID_FILE):
            raise StepError("无设备记录(device_id.txt),请先运行 capture.py")
        with open(DEVICE_ID_FILE, "r", encoding="utf-8") as f:
            device_id = f.read().strip()
        if not device_id:
            raise StepError("device_id.txt 为空")

        client = AdbClient()
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

    def _load_from_file(self) -> bool:
        """回退:从静态网格图文件加载。成功返回 True。"""
        import json as _json

        if not os.path.isfile(GRID_IMAGE) or not os.path.isfile(GRID_META):
            return False
        try:
            with open(GRID_META, "r", encoding="utf-8") as f:
                cell_size = int(_json.load(f)["cell_size"])
            grid = cv2.imread(GRID_IMAGE)
            if grid is None:
                return False
        except (_json.JSONDecodeError, KeyError, ValueError):
            return False
        self._set_grid(grid, cell_size)
        # 静态回退模式:从 screenshot.png 读原始画面用于录制锚点(可能与设备当前画面有延迟)
        self._latest_raw = cv2.imread(RAW_IMAGE)
        return True

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
        self.photo = tk.PhotoImage(file=self._tmp)
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
                self._ai_confirm_btn.grid_remove()
                self._ai_deny_btn.grid_remove()
                self._ai_next_btn.grid_remove()
                self._ai_cand_info_var.set("")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.grid(row=0, column=0)

        # 左:网格图画布(两种模式共享)
        self.canvas = tk.Canvas(root, width=self.disp_w, height=self.disp_h,
                                highlightthickness=1, highlightbackground="#555")
        self.canvas.grid(row=0, column=0, rowspan=2)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        # AI 候选预览时:拖动微调候选点
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        hint = ttk.Label(root, text="普通模式:点击图片添加步骤    |    AI 模式候选出现后:点击/拖动可微调十字位置",
                         foreground="#666")
        hint.grid(row=2, column=0, sticky="w", pady=(4, 0))

        # 右:Tab 切换(普通模式 / AI 模式)
        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=1, sticky="n", padx=(10, 0))

        # ---- Tab 1: 普通模式(原有步骤列表 + 操作按钮 + 执行日志) ----
        right = ttk.Frame(self.notebook, padding=(10, 0, 0, 0))
        self.notebook.add(right, text="普通模式")

        ttk.Label(right, text="步骤列表(从上到下顺序执行)").grid(row=0, column=0, columnspan=3, sticky="w")
        self.listbox = tk.Listbox(right, width=38, height=18, font=("Consolas", 10),
                                  activestyle="dotbox")
        self.listbox.grid(row=1, column=0, columnspan=3, pady=4)
        scroll = ttk.Scrollbar(right, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=1, column=3, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # 前/后等待设置:点图新步骤自动带上当前值;修改后点"应用到选中"更新
        ttk.Label(right, text="执行前等待(秒):").grid(row=2, column=0, sticky="e")
        self.before_var = tk.StringVar(value="0")
        ttk.Spinbox(right, from_=0, to=600, width=6, textvariable=self.before_var).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(right, text="执行后等待(秒):").grid(row=3, column=0, sticky="e")
        self.after_var = tk.StringVar(value="10")
        ttk.Spinbox(right, from_=0, to=600, width=6, textvariable=self.after_var).grid(
            row=3, column=1, sticky="w", padx=4)
        ttk.Button(right, text="应用到选中步骤", command=self._apply_waits).grid(
            row=2, column=2, rowspan=2, sticky="ns", padx=4)

        # 脚本名称:保存前必填;保存成功后清空,回到"新建脚本"状态
        ttk.Label(right, text="脚本名称:").grid(row=4, column=0, sticky="e")
        self.script_name_var = tk.StringVar(value="")
        ttk.Entry(right, textvariable=self.script_name_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=4, pady=(6, 2))

        ttk.Button(right, text="删除选中", command=self._delete_selected).grid(row=5, column=0, sticky="ew", pady=2)
        ttk.Button(right, text="上移", command=lambda: self._move(-1)).grid(row=5, column=1, sticky="ew", pady=2)
        ttk.Button(right, text="下移", command=self._move(1)).grid(row=5, column=2, sticky="ew", pady=2)
        ttk.Button(right, text="清空", command=self._clear).grid(row=6, column=0, sticky="ew", pady=2)
        ttk.Button(right, text="保存脚本", command=self._save).grid(row=6, column=1, sticky="ew", pady=2)
        ttk.Button(right, text="加载脚本", command=self._load).grid(row=6, column=2, sticky="ew", pady=2)

        ttk.Button(right, text="⟳ 刷新截图", command=self._refresh).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=2)
        self._auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="自动刷新(1秒)", variable=self._auto_var).grid(
            row=7, column=2, sticky="w", padx=4)

        self.run_btn = ttk.Button(right, text="▶ 运行", command=self._run)
        self.run_btn.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 2))

        # 右下:执行日志
        ttk.Label(right, text="执行日志:").grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.log_text = tk.Text(right, width=42, height=10, font=("Consolas", 9),
                                state="disabled", bg="#111", fg="#0f0")
        self.log_text.grid(row=10, column=0, columnspan=3, pady=2)

        # ---- Tab 2: AI 模式(对话式步骤编排) ----
        ai_tab = ttk.Frame(self.notebook, padding=(10, 0, 0, 0))
        self.notebook.add(ai_tab, text="AI 模式")
        # 切回普通模式时恢复纯网格,避免候选框干扰手动点格
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        ttk.Label(ai_tab, text="AI 对话").grid(row=0, column=0, columnspan=2, sticky="w")
        self._ai_chat = tk.Text(ai_tab, width=44, height=20, font=("Consolas", 10),
                                state="disabled", bg="#1a1a2e", fg="#e0e0e0",
                                wrap="word", relief="sunken", bd=2)
        self._ai_chat.grid(row=1, column=0, columnspan=2, pady=4)
        ai_scroll = ttk.Scrollbar(ai_tab, orient="vertical", command=self._ai_chat.yview)
        ai_scroll.grid(row=1, column=2, sticky="ns")
        self._ai_chat.configure(yscrollcommand=ai_scroll.set)
        # 消息着色:AI 蓝色,用户绿色
        self._ai_chat.tag_configure("ai", foreground="#6ea8fe")
        self._ai_chat.tag_configure("user", foreground="#6eff6e")

        ttk.Label(ai_tab, text="输入指令(如:点击kof图标):").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        # 定位模式:全流程 / 仅OCR / 仅VLM(调试用)
        self._ai_mode_var = tk.StringVar(value="全流程")
        ttk.Combobox(ai_tab, textvariable=self._ai_mode_var,
                     values=["全流程", "仅OCR", "仅VLM"], width=8,
                     state="readonly").grid(row=2, column=1, sticky="e", pady=(6, 0))

        self._ai_input_var = tk.StringVar()
        ai_entry = ttk.Entry(ai_tab, textvariable=self._ai_input_var, width=34)
        ai_entry.grid(row=3, column=0, sticky="ew", pady=2)
        ai_entry.bind("<Return>", lambda e: self._ai_send())
        ttk.Button(ai_tab, text="发送", command=self._ai_send).grid(
            row=3, column=1, sticky="w", padx=4)

        # 确认/否认/换候选按钮(初始隐藏,AI 提议后显示)
        self._ai_confirm_btn = ttk.Button(ai_tab, text="✓ 是这个",
                                         command=self._ai_confirm)
        self._ai_deny_btn = ttk.Button(ai_tab, text="✗ 不是",
                                       command=lambda: self._ai_cycle_candidate(1))
        self._ai_next_btn = ttk.Button(ai_tab, text="⇄ 换候选",
                                       command=lambda: self._ai_cycle_candidate(1))
        self._ai_confirm_btn.grid(row=4, column=0, sticky="ew", pady=(6, 2))
        self._ai_deny_btn.grid(row=4, column=1, sticky="ew", pady=(6, 2), padx=(2, 0))
        self._ai_next_btn.grid(row=4, column=2, sticky="ew", pady=(6, 2), padx=(2, 0))
        self._ai_confirm_btn.grid_remove()
        self._ai_deny_btn.grid_remove()
        self._ai_next_btn.grid_remove()

        # 候选信息(来源/置信度/验证状态)
        self._ai_cand_info_var = tk.StringVar(value="")
        ttk.Label(ai_tab, textvariable=self._ai_cand_info_var,
                  foreground="#444", wraplength=300, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w")

        # AI 模式也放运行按钮(共享 steps 列表)
        ttk.Button(ai_tab, text="⟳ 刷新截图", command=self._refresh).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Button(ai_tab, text="▶ 运行脚本", command=self._run).grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(8, 2))

        # AI 模式日志
        ttk.Label(ai_tab, text="AI 模式日志:").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self._ai_log_text = tk.Text(ai_tab, width=42, height=8, font=("Consolas", 9),
                                    state="disabled", bg="#111", fg="#0f0")
        self._ai_log_text.grid(row=9, column=0, columnspan=3, pady=2)

        # 最右:当前步骤的标准图(执行前/执行后),两种模式共享
        preview = ttk.Frame(root, padding=(10, 0, 0, 0))
        preview.grid(row=0, column=2, sticky="n")
        ttk.Label(preview, text="当前步骤标准图").grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(preview, text="执行前(开始条件):").grid(row=1, column=0, columnspan=2, sticky="w",
                                                           pady=(6, 0))
        self._before_photo = self._make_placeholder_image()
        self.before_img_label = tk.Label(
            preview, image=self._before_photo,
            width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground="#555", bg="#222")
        self.before_img_label.grid(row=2, column=0, columnspan=2, pady=2)
        self.before_status_var = tk.StringVar(value="未选中步骤")
        ttk.Label(preview, textvariable=self.before_status_var,
                  foreground="#666", wraplength=EXPECT_IMG_W, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w")

        ttk.Label(preview, text="执行后(完成条件):").grid(row=4, column=0, columnspan=2, sticky="w",
                                                           pady=(6, 0))
        self._after_photo = self._make_placeholder_image()
        self.after_img_label = tk.Label(
            preview, image=self._after_photo,
            width=EXPECT_IMG_W, height=EXPECT_IMG_H,
            highlightthickness=1, highlightbackground="#555", bg="#222")
        self.after_img_label.grid(row=5, column=0, columnspan=2, pady=2)
        self.after_status_var = tk.StringVar(value="未选中步骤")
        ttk.Label(preview, textvariable=self.after_status_var,
                  foreground="#666", wraplength=EXPECT_IMG_W, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w")

        ttk.Button(preview, text="📷 重拍执行前图",
                   command=lambda: self._recapture_anchor("before_image")).grid(
            row=7, column=0, sticky="ew", pady=(6, 2), padx=(0, 2))
        ttk.Button(preview, text="📷 截执行后图",
                   command=lambda: self._recapture_anchor("after_image")).grid(
            row=7, column=1, sticky="ew", pady=(6, 2), padx=(2, 0))

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
        self.listbox.see("end")

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
        sel = self.listbox.curselection()
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

    def _show_expect_preview(self, index: Optional[int]) -> None:
        """
        显示指定步骤的执行前/执行后标准图。

        执行前:本步 before_image;
        执行后:本步 after_image;缺省时提示"默认=下一步执行前图"(引擎自动链接)。
        """
        if index is None or not (0 <= index < len(self.steps)):
            self._set_preview_image("before", None)
            self._set_preview_image("after", None)
            self.before_status_var.set("未选中步骤")
            self.after_status_var.set("未选中步骤")
            return

        step = self.steps[index]
        title = f"第 {index + 1} 步 {step['cell']}"

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

    def _on_select(self, _event: tk.Event) -> None:
        """选中步骤时,把该步骤的前/后等待回填到输入框,并显示预期画面。"""
        sel = self.listbox.curselection()
        if len(sel) == 1:
            i = sel[0]
            step = self.steps[i]
            self.before_var.set(f"{step.get('delay_before', 0):g}")
            self.after_var.set(f"{step.get('wait_after', 0):g}")
            self._show_expect_preview(i)

    def _apply_waits(self) -> None:
        """把输入框中的前/后等待写回选中的步骤。"""
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("未选中", "请先在列表中选中一个步骤")
            return
        for i in sel:
            self.steps[i]["delay_before"] = self._spin_value(self.before_var)
            self.steps[i]["wait_after"] = self._spin_value(self.after_var)
        self._refresh_list()
        for i in sel:
            self.listbox.selection_set(i)

    def _select_and_preview(self, index: Optional[int]) -> None:
        """程序化选中某步并刷新预期画面(selection_set 不触发 <<ListboxSelect>>)。"""
        self.listbox.selection_clear(0, "end")
        if index is not None and 0 <= index < len(self.steps):
            self.listbox.selection_set(index)
            step = self.steps[index]
            self.before_var.set(f"{step.get('delay_before', 0):g}")
            self.after_var.set(f"{step.get('wait_after', 0):g}")
            self._show_expect_preview(index)
        else:
            self._show_expect_preview(None)

    def _delete_selected(self) -> None:
        sel = list(self.listbox.curselection())
        for i in reversed(sel):
            del self.steps[i]
        self._refresh_list()
        # 删除后选中相邻步骤(优先同位置),没有则清空预览
        nxt = min(sel[0], len(self.steps) - 1) if self.steps and sel else None
        self._select_and_preview(nxt)

    def _move(self, delta: int) -> None:
        sel = self.listbox.curselection()
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
        self.listbox.delete(0, "end")
        for i, step in enumerate(self.steps, 1):
            before_wait = step.get("delay_before", 0)
            after_wait = step.get("wait_after", 0)
            suffix = ""
            if before_wait or after_wait:
                suffix = f"  (前{before_wait:g}s/后{after_wait:g}s)"
            marks = ""
            if step.get("before_image"):
                marks += " ▶"  # 有执行前标准图
            if step.get("after_image"):
                marks += " ⏹"  # 有执行后标准图
            if step.get("x") is not None and step.get("y") is not None:
                marks += " 🎯"  # AI 精确定位(带像素坐标)
            self.listbox.insert("end", f"{i:2d}. 点击 {step['cell']}{marks}{suffix}")

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
        path = os.path.abspath(path)
        try:
            self.steps = load_steps(path)
        except StepError as exc:
            messagebox.showerror("加载失败", str(exc))
            return
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

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _run(self) -> None:
        if self._running:
            return
        if not self.steps:
            messagebox.showwarning("无步骤", "请先在左侧图片上点击格子添加步骤")
            return
        # 先进入运行态:阻止新的自动刷新启动;再等在途刷新落盘完成,
        # 避免读到半写入的 screenshot.png / 与执行轮询争抢 adb
        self._running = True
        self.run_btn.configure(state="disabled")
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
            messagebox.showerror("执行环境缺失", str(exc))
            return
        # 锚点图相对路径以当前脚本 JSON 所在目录为基准(未保存草稿用 scripts/untitled/)
        runner.script_dir = (os.path.dirname(os.path.abspath(self.script_path))
                             if self.script_path
                             else os.path.join(SCRIPTS_DIR, DRAFT_SCRIPT_NAME))
        self._log(f"开始执行脚本 [{self.script_name}],共 {len(self.steps)} 步,"
                  f"设备: {runner.device_id}")
        threading.Thread(target=self._run_worker, args=(runner,), daemon=True).start()

    def _run_worker(self, runner: StepRunner) -> None:
        try:
            runner.run(self.steps, on_event=self._on_step_event)
        except Exception as exc:  # AdbError / StepError 等,统一在 UI 层提示
            self.after(0, self._log, f"[失败] {exc}")
        finally:
            self.after(0, self._run_finished)

    def _run_finished(self) -> None:
        self._running = False
        self.run_btn.configure(state="normal")

    def _on_step_event(self, idx: int, total: int, msg: str) -> None:
        # StepRunner 在后台线程,日志切回主线程刷新
        self.after(0, self._log, f"[{idx}/{total}] {msg}")
        # 开始执行某步(点击)时,联动选中该步并显示它的预期画面
        if msg.startswith("点击 "):
            self.after(0, self._preview_running_step, idx - 1)

    def _preview_running_step(self, index: int) -> None:
        """执行过程中高亮当前步并展示其预期画面(不改编辑用的等待输入框)。"""
        self.listbox.selection_clear(0, "end")
        if 0 <= index < len(self.steps):
            self.listbox.selection_set(index)
            self.listbox.see(index)
            self._show_expect_preview(index)

    def _log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # AI 模式
    # ------------------------------------------------------------------
    def _ai_log(self, msg: str) -> None:
        """写入 AI 模式日志区。"""
        self._ai_log_text.configure(state="normal")
        self._ai_log_text.insert("end", msg + "\n")
        self._ai_log_text.see("end")
        self._ai_log_text.configure(state="disabled")

    def _ai_chat_append(self, speaker: str, text: str) -> None:
        """向 AI 对话区追加一条消息。"""
        self._ai_chat.configure(state="normal")
        tag = "ai" if speaker == "AI" else "user"
        self._ai_chat.insert("end", f"[{speaker}] ", tag)
        self._ai_chat.insert("end", text + "\n\n")
        self._ai_chat.see("end")
        self._ai_chat.configure(state="disabled")

    def _on_tab_changed(self, event=None) -> None:
        """切普通模式恢复纯网格;切回 AI 模式重绘当前候选。"""
        if self.notebook.index("current") == 0:
            self._clear_ai_overlay()
        elif (self._ai_result is not None and self._ai_result.top_candidates
              and not self._ai_busy):
            self._show_candidate(self._ai_cand_idx, silent=True)

    _AI_MODE_MAP = {"全流程": "full", "仅OCR": "ocr_only", "仅VLM": "vlm_only"}

    def _ai_send(self) -> None:
        """用户发送指令 -> 新定位管线(OCR+VLM+模板 融合验证)。"""
        instruction = self._ai_input_var.get().strip()
        if not instruction or self._ai_busy:
            return
        self._ai_input_var.set("")

        # 清除上一次的候选预览
        self._ai_confirm_btn.grid_remove()
        self._ai_deny_btn.grid_remove()
        self._ai_next_btn.grid_remove()
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
            self.after(0, self._ai_locate_done, result)
        except Exception as exc:  # 管线内任何异常都回显,不崩 GUI
            self.after(0, self._ai_log, f"定位异常: {exc}")
            self.after(0, self._ai_locate_error, str(exc))

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
                                       f"[{sources}] 置信度 {cand.confidence:.2f} {ver}")
            self._ai_log(f"候选{idx + 1}/{n} {cell} ({x},{y}) {sources} "
                         f"conf={cand.confidence:.2f} {ver}")
        self._ai_confirm_btn.grid()
        self._ai_deny_btn.grid()
        if n > 1:
            self._ai_next_btn.grid()
        else:
            self._ai_next_btn.grid_remove()

    def _ai_cycle_candidate(self, delta: int) -> None:
        """切换到下一个/上一个候选。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        n = len(result.top_candidates)
        if n <= 1:
            self._ai_chat_append("AI", "只有一个候选。可在普通模式直接点格子手动指定。")
            return
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._show_candidate((self._ai_cand_idx + delta) % n)

    def _ai_confirm(self) -> None:
        """确认当前候选 -> 带像素坐标的 tap 步骤入库。"""
        result = self._ai_result
        if result is None or not result.top_candidates:
            return
        cand = result.top_candidates[self._ai_cand_idx]
        x, y = int(cand.point[0]), int(cand.point[1])
        cell = self._cell_for_point(x, y)

        step = {
            "type": "tap", "cell": cell,
            "x": x, "y": y,  # 精确像素坐标,回放时优先于格子换算
            "delay_before": self._spin_value(self.before_var),
            "wait_after": self._spin_value(self.after_var),
            "before_image": "",
            "after_image": "",
        }
        before_rel = self._capture_anchor_now("before")
        if before_rel:
            step["before_image"] = before_rel
        self.steps.append(step)
        self._refresh_list()
        self._select_and_preview(len(self.steps) - 1)
        self.listbox.see("end")

        self._ai_chat_append("AI", f"已添加步骤: 点击 {cell} ({x},{y})")
        self._ai_log(f"确认 -> 步骤 {len(self.steps)}: {cell} ({x},{y})")
        self._reset_ai_proposal_ui()
        # 切到普通模式让用户看到新步骤
        self.notebook.select(0)

    def _reset_ai_proposal_ui(self) -> None:
        """确认/切换截图后复位候选预览状态。"""
        self._clear_ai_overlay()
        self._ai_result = None
        self._ai_adjusted = False
        self._ai_drag_last = None
        self._ai_cand_idx = 0
        self._ai_cand_info_var.set("")
        self._ai_confirm_btn.grid_remove()
        self._ai_deny_btn.grid_remove()
        self._ai_next_btn.grid_remove()

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
    if app.winfo_exists():
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
