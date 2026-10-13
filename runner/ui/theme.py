"""
runner/ui/theme.py - 暗色主题与字体常量(自 gui_steps.py 拆出,CR-08)
==========================================================================
视觉定调参照 IDE / 开发者工具暗色(VS Code、GitHub Dark、DevTools),
长时间盯截图与日志不刺眼;状态色用中性工程色,图标保持线性/文字。

SKILL.md 红线:视觉层只走本模块的 THEME dict + FONT_* 常量,禁止裸写 hex。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 暗色主题(克制版:单一葡萄紫强调色,无霓虹/渐变)
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
