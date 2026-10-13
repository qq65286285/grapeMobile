"""
runner/gui_steps.py - 步骤编排可视化界面(Tkinter)主窗口
==========================================================
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

模块结构(CR-08 拆分,原 3424 行单文件):
    gui_steps.py(本文件)   StepApp 主窗口:启动闸门/暗色主题/三栏骨架/
                           日志与 Tab 管理
    ui/theme.py            暗色主题 THEME 与字体常量(视觉层唯一来源)
    ui/constants.py        路径与布局常量
    ui/widgets.py          _ChatPane/_UiBridge/_AiLogHandler/圆角控件
    ui/device_dialog.py    启动设备选择模态框
    ui/canvas_mixin.py     设备画面/网格/画布交互/锚点截图
    ui/steps_mixin.py      步骤编排/列表/卡片/保存加载
    ui/runner_mixin.py     运行控制与执行模式(测试计划)
    ui/ai_mixin.py         AI 模式定位/候选/叠加
"""

from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

import aiclient  # noqa: F401  AI 调用客户端(trace 钩子用于展示 AI 思考过程)

# 将 src/ 加入 sys.path(与其他 runner 脚本一致)
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)
# 项目依赖目录(rapidfuzz/cv2/PIL 等)加入 sys.path:
# 不设 PYTHONPATH 直接启动 GUI 时,locate 管线也能找到第三方包
_DEPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_deps")
if os.path.isdir(_DEPS_DIR):
    sys.path.insert(0, os.path.abspath(_DEPS_DIR))

from adbtools import AdbClient  # noqa: E402  (__init__ 类型注解用)

# ui 包:内部完成 src/_deps/runner 路径注入,并提供主题/常量/控件/mixin
from ui.constants import (  # noqa: E402  (re-export 兼容 _ui_preview.py 等)
    DEFAULT_CELL_SIZE, DEVICE_ID_FILE, DRAFT_SCRIPT_NAME, EXPECT_IMG_H,
    EXPECT_IMG_W, GRID_IMAGE, GRID_META, MARGIN, MAX_DISPLAY_HEIGHT,
    MAX_DISPLAY_WIDTH, RAW_IMAGE, RESULT_DIR, SCRIPTS_DIR, _BASE_DIR,
)
from ui.theme import (  # noqa: E402  (re-export 兼容)
    FONT_APPBAR_ICO, FONT_BTN, FONT_BTN_BOLD, FONT_BUBBLE, FONT_BUBBLE_SM,
    FONT_CAPTION, FONT_EMOJI, FONT_LIST, FONT_MONO, FONT_MONO_CHAT,
    FONT_UI, FONT_UI_BOLD, THEME,
)
from ui.widgets import (  # noqa: E402  (re-export 兼容 _ui_preview.py)
    _AiLogHandler, _ChatPane, _RoundedBadge, _RoundedButton, _UiBridge,
)
from ui.device_dialog import DeviceSelectDialog  # noqa: E402
from ui.canvas_mixin import _CanvasMixin  # noqa: E402
from ui.steps_mixin import _StepsMixin  # noqa: E402
from ui.steps_cards import _CardsMixin  # noqa: E402
from ui.runner_mixin import _RunnerMixin  # noqa: E402
from ui.ai_mixin import _AiMixin  # noqa: E402


class StepApp(_CanvasMixin, _StepsMixin, _CardsMixin, _RunnerMixin, _AiMixin, tk.Tk):
    """步骤编排主窗口:网格图选格 + 步骤列表 + 执行控制。

    职责域经 mixin 拆分(CR-08),本类只保留窗口骨架:
    启动闸门(设备选择 + 首次截图)、暗色主题、三栏 UI 骨架、
    跨域日志(普通/AI/执行三区镜像)与 Tab 管理。
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("GrapeMobile 步骤编排器")
        # 可缩放:固定尺寸会把右栏 Spinbox/按钮裁掉(实测截图问题)
        self.resizable(True, True)
        self.minsize(1200, 720)
        # 暗色克制主题(单一葡萄紫强调色,无霓虹渐变);须在任意控件创建前应用
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
        self._auto_var = tk.BooleanVar(value=True)  # 自动刷新开关(默认开)
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
    # UI 构建:三栏骨架 + 顶栏,主体面板委托各 mixin 的 _build_*
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
        # 左栏:设备画面(canvas_mixin)
        # ==================================================================
        # 宽 320:容纳 288px 画布 + 17px 竖滚动条(288+17=305,280 会把滚动条裁掉)
        left = tk.Frame(root, bg=THEME["bg"], width=320)
        left.grid(row=1, column=0, sticky="ns", padx=(10, 0), pady=8)
        left.grid_propagate(False)
        self._build_left_panel(left)

        # ==================================================================
        # 中栏:步骤 · 实时预览(steps_mixin)
        # ==================================================================
        center = tk.Frame(root, bg=THEME["surface"])
        center.grid(row=1, column=1, sticky="nsew", padx=10, pady=8)
        self._build_center_panel(center)

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

        self._build_ai_tab(ai_tab)
        self._build_normal_tab(normal)
        self._build_exec_tab(exec_tab)

        self._exec_busy = False
        self._last_plan_dir: Optional[str] = None
        self._exec_refresh()

        # 默认步骤等待时间(画布点击追加步骤时使用)
        self.before_var = tk.StringVar(value="0")
        self.after_var = tk.StringVar(value="10")

    # ------------------------------------------------------------------
    # 跨域日志:普通 Console / AI 思考过程 / 执行日志三区镜像
    # ------------------------------------------------------------------
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
        old_idx = self._preview_index
        self._select_and_preview(index)
        self._update_card_selection(old_idx, index)

    def _log(self, msg: str) -> None:
        self._mirror_exec_log(msg)
        # 同步到普通模式 Tab 内的日志区
        if hasattr(self, "_normal_log_text"):
            self._normal_log_text.configure(state="normal")
            self._normal_log_text.insert("end", msg + "\n")
            self._normal_log_text.see("end")
            self._normal_log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # AI 日志区(思考过程 / 对话流 / 执行日志镜像)
    # ------------------------------------------------------------------
    def _ai_trace(self, msg: str) -> None:
        """aiclient trace 钩子:AI 请求/回复(思考过程)经 bridge 进顶部思考过程区。"""
        self._bridge.post(lambda: self._ai_log(msg))

    def _ai_log(self, msg: str) -> None:
        """写入顶部"AI 思考过程"区。"""
        if not hasattr(self, "_ai_think_text"):
            return
        self._ai_think_text.configure(state="normal")
        self._ai_think_text.insert("end", msg + "\n")
        self._ai_think_text.see("end")
        self._ai_think_text.configure(state="disabled")

    def _ai_chat_append(self, speaker: str, text: str) -> None:
        """对话消息并入顶部思考过程区(带时间与发送者前缀)。"""
        if not hasattr(self, "_ai_think_text"):
            return
        stamp = time.strftime("%H:%M:%S")
        self._ai_think_text.configure(state="normal")
        self._ai_think_text.insert("end", f"[{stamp}] [{speaker}] {text}\n")
        self._ai_think_text.see("end")
        self._ai_think_text.configure(state="disabled")

    def _exec_log_apply(self, msg: str) -> None:
        """主线程:执行日志写入底部"执行日志"区。"""
        if not hasattr(self, "_ai_log_text"):
            return
        self._ai_log_text.configure(state="normal")
        self._ai_log_text.insert("end", msg + "\n")
        # 限制行数,防止长会话无限增长
        lines = int(self._ai_log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self._ai_log_text.delete("1.0", f"{lines - 300}.0")
        self._ai_log_text.see("end")
        self._ai_log_text.configure(state="disabled")

    def _mirror_exec_log(self, msg: str) -> None:
        """执行日志镜像到 AI Tab 底部(线程安全:经 bridge 切回主线程)。"""
        if not hasattr(self, "_bridge"):
            return
        self._bridge.post(lambda: self._exec_log_apply(msg))

    # ------------------------------------------------------------------
    # Tab 管理
    # ------------------------------------------------------------------
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


def main() -> int:
    app = StepApp()
    # 用户在设备选择框取消,或首次截图失败:窗口已销毁,不进入主循环
    if getattr(app, "_startup_ok", False) and app.winfo_exists():
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
