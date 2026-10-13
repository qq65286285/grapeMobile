"""
runner/ui - GUI 公共模块(theme/constants/widgets/device_dialog 及 StepApp mixin)
================================================================================
CR-08 拆分产物:把原 gui_steps.py 单文件中的主题/常量/通用控件/设备对话框
与 StepApp 的四大职责域(canvas/steps/runner/ai)拆为独立模块。

导入约定:
    - 本包 __init__ 负责把 src/ 与 _deps/ 注入 sys.path,
      各子模块可直接 `from steps import ...` / `from adbtools import ...`;
    - 子模块之间用 `from ui.xxx import ...`(gui_steps.py 把 runner/ 插在 path 首位);
    - 视觉层只走 ui.theme 的 THEME dict + FONT_* 常量,禁止裸写 hex(SKILL.md 红线)。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUNNER_DIR = os.path.dirname(_HERE)
_SRC_DIR = os.path.join(os.path.dirname(_RUNNER_DIR), "src")
_DEPS_DIR = os.path.join(os.path.dirname(_RUNNER_DIR), "_deps")

for _p in (_SRC_DIR, _DEPS_DIR, _RUNNER_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
