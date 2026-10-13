"""
runner/ui/widgets.py - 通用控件与线程桥(自 gui_steps.py 拆出,CR-08)
==========================================================================
包含:
    _ChatPane       AI 对话气泡面板(Canvas + 内层 Frame 模拟可滚动消息流)
    _UiBridge       后台线程 -> 主线程的安全投递桥梁
    _AiLogHandler   locate 管线日志 -> AI 日志区的 logging.Handler
    _RoundedBadge   圆角编号徽章(PIL 绘制底图)
    _RoundedButton  圆角按钮(normal/primary/ghost + hover)
"""

from __future__ import annotations

import logging
import queue
from collections import OrderedDict
from typing import Any, Optional

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw, ImageTk

from ui.theme import (
    THEME,
    FONT_BTN, FONT_BUBBLE, FONT_BUBBLE_SM, FONT_CAPTION,
)


class _ChatPane:
    """AI 对话气泡面板:AI 左灰底、用户右紫底、系统提示居中浅色。

    用 Canvas + 内层 Frame 模拟可滚动消息流(每条消息一个 Label),
    取代纯文本 Text 追加,呈现对齐设计稿的气泡式对话。
    """

    def __init__(self, parent: tk.Misc, width: int = 420,
                 height: int = 260) -> None:
        # 外层描边框:让对话区在深色底上明确成一个"面板"(对齐设计稿)
        self.frame = tk.Frame(parent, bg=THEME["border2"], bd=0)
        # frame 内部用 grid:canvas 占 (0,0) sticky=nsew, 滚条占 (0,1) sticky=ns
        # frame 不写 propagate,但 canvas 用 sticky="nsew" 撑满即可,
        # 不钉死 height/width,这样父容器给多少高度就占多少
        # frame 内部 grid 必须配 weight,否则 canvas sticky="nsew" 也撑不开
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.frame, bg=THEME["surface"], highlightthickness=1,
                                highlightbackground=THEME["border"],
                                highlightcolor=THEME["border"],
                                bd=0)
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

    def __init__(self, app: "Any") -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        app = self._app
        app._bridge.post(lambda m=msg: app._ai_log(m))


class _RoundedBadge(tk.Label):
    """圆角编号徽章:用 PIL 绘制圆角矩形底图(替代 Canvas 手绘,缩放不糊)。"""

    # 类级 PIL 缓存:限制大小避免 Tcl 图片表无限增长导致 GC 越来越慢
    _MAX_CACHE = 96
    _img_cache: "OrderedDict[tuple, ImageTk.PhotoImage]" = OrderedDict()

    @classmethod
    def _make_bg(cls, w: int, h: int, r: int, fill: str) -> ImageTk.PhotoImage:
        key = (w, h, r, fill)
        if key in cls._img_cache:
            cls._img_cache.move_to_end(key)
            return cls._img_cache[key]
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill)
        cls._img_cache[key] = ImageTk.PhotoImage(img)
        if len(cls._img_cache) > cls._MAX_CACHE:
            cls._img_cache.popitem(last=False)
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

    # 类级 PIL 缓存:限制大小避免 Tcl 图片表无限增长导致 GC 越来越慢
    _MAX_CACHE = 96
    _img_cache: "OrderedDict[tuple, ImageTk.PhotoImage]" = OrderedDict()

    @classmethod
    def _make_bg(cls, w: int, h: int, r: int, fill: str,
                 outline: Optional[str] = None) -> ImageTk.PhotoImage:
        key = (w, h, r, fill, outline)
        if key in cls._img_cache:
            cls._img_cache.move_to_end(key)
            return cls._img_cache[key]
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if outline:
            d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill, outline=outline, width=1)
        else:
            d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill)
        cls._img_cache[key] = ImageTk.PhotoImage(img)
        if len(cls._img_cache) > cls._MAX_CACHE:
            cls._img_cache.popitem(last=False)
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
