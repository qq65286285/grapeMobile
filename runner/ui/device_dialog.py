"""
runner/ui/device_dialog.py - 启动设备选择模态框(自 gui_steps.py 拆出,CR-08)
==========================================================================
枚举 adb devices,用户必须选中一台在线设备才能进入主界面。

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

from __future__ import annotations

import threading
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

from ui.theme import (
    THEME,
    FONT_CAPTION, FONT_EMOJI, FONT_MONO, FONT_UI, FONT_UI_BOLD,
)
from ui.widgets import _RoundedButton, _UiBridge
from adbtools import AdbClient


class DeviceSelectDialog(tk.Toplevel):
    """启动时的模态设备选择框:枚举 adb devices,用户必须选中一台在线设备才能进入。"""

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
        self._devices: List[Tuple[str, str]] = []
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
