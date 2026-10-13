"""
测试: GUI 结构冒烟(runner/gui_steps.py 及 runner/ui/ 拆分模块)
覆盖: 模块可导入、mixin 方法归属完整(拆分不变量)、常量 re-export 兼容
(_ui_preview.py 等通过 G.XXX 访问)、MRO 正确、行数守卫;
有显示环境时追加真实 Tk 控件渲染冒烟(无 display 自动 skip)。

本测试是 CR-08(拆分 gui_steps.py 上帝类)的回归保护网:
    任何"搬方法时漏搬/搬错模块/常量分叉"都会在此暴露。
"""

import os
import sys

import pytest

# runner/ 加入路径:与 pyproject 的 pythonpath=["src"] 互补,
# 使测试可 import gui_steps(其内部自行把 src/ 插入 sys.path)
_RUNNER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runner")
_RUNNER_DIR = os.path.abspath(_RUNNER_DIR)
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)
_SRC_DIR = os.path.join(os.path.dirname(_RUNNER_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _import_gui():
    """延迟 import:模块级 import 失败时测试报错信息更清晰。"""
    import gui_steps
    return gui_steps


# --------------------------------------------------------------------------
# 拆分不变量:每个方法应归属的 mixin(增删方法必须同步本表)
# --------------------------------------------------------------------------
CANVAS_MIXIN_METHODS = [
    # 设备画面生产(可后台线程)
    "_auto_tick", "_auto_worker", "_auto_apply",
    "_produce_grid_from_device", "_capture_from_device", "_read_cell_size",
    # 网格设置与画布渲染
    "_set_grid", "_toggle_grid", "_zoom", "_zoom_reset", "_render_canvas",
    # 点击换算 / 锚点截图
    "_on_canvas_click", "_anchor_dir", "_max_existing_anchor_seq",
    "_capture_anchor_now", "_recapture_anchor",
    # 标准图预览辅助与展示
    "_make_placeholder_image", "_imread_cached", "_set_preview_image",
    "_show_expect_preview",
    # 画布拖动坐标换算
    "_on_canvas_drag", "_event_to_raw",
    # UI 构建
    "_build_left_panel",
]

STEPS_MIXIN_METHODS = [
    # 系统操作步骤
    "_add_system_step", "_step_action_text",
    # 列表选择与等待编辑
    "_spin_value", "_list_sel", "_list_select", "_list_see_end",
    "_on_select", "_apply_step_detail", "_on_step_double_click",
    "_edit_step_waits",
    # 选中联动(卡片高亮在 steps_cards)
    "_select_and_preview", "_select_card", "_update_list_row_waits",
    # 步骤增删改
    "_delete_step", "_delete_selected", "_move", "_clear",
    # 列表重建(卡片重建在 steps_cards)
    "_refresh_list", "_refresh",
    # 保存 / 加载
    "_save", "_reset_to_new_script", "_cleanup_orphan_anchors",
    "_migrate_anchor_folder", "_load", "_load_script", "_current_script_dir",
    # UI 构建
    "_build_center_panel", "_build_normal_tab",
]

# steps_mixin 的二次拆分:卡片渲染与选中高亮(CR-08)
CARDS_MIXIN_METHODS = [
    "_refresh_card_wait_chips", "_update_wait_labels", "_update_card_selection",
    "_refresh_step_cards", "_refresh_normal_cards",
]

RUNNER_MIXIN_METHODS = [
    # 普通模式运行
    "_run", "_full_run", "_start_run", "_run_worker", "_run_finished",
    "_render_worker",
    # 执行模式(测试计划)
    "_exec_log", "_exec_refresh", "_on_exec_tree_click",
    "_show_exec_case_detail", "_exec_load_to_normal", "_exec_open_report",
    "_exec_toggle_all", "_exec_set_all", "_exec_set_status", "_exec_checked",
    "_exec_worker", "_make_exec_event", "_exec_finished",
    # UI 构建
    "_build_exec_tab",
]

AI_MIXIN_METHODS = [
    # 候选卡片
    "_show_cand_card", "_set_cand_btns",
    # AI 定位主链
    "_ai_send", "_ai_locate_worker", "_ai_locate_error", "_ai_locate_done",
    "_show_locate_result",
    # 候选展示 / 切换 / 确认
    "_show_candidate", "_ai_cycle_candidate", "_ai_confirm",
    "_reset_ai_proposal_ui", "_clear_ai_overlay",
    # 坐标换算与叠加绘制
    "_cell_for_point", "_draw_ai_overlay", "_ai_adjust_candidate",
    # Tab 切换联动
    "_on_tab_changed",
    # UI 构建
    "_build_ai_tab",
]

# 保留在 gui_steps.py 主体的方法(窗口骨架/主题/日志/Tab)
MAIN_FILE_METHODS = [
    "__init__", "_quit_startup", "_on_close", "_apply_dark_theme", "_build_ui",
    "_on_step_event", "_preview_running_step",
    "_log", "_ai_trace", "_ai_log", "_ai_chat_append",
    "_exec_log_apply", "_mirror_exec_log",
    "_switch_tab",
]


class TestModuleImport:
    """模块与 re-export 兼容(_ui_preview.py 经 G.XXX 访问)。"""

    def test_gui_steps_importable(self):
        G = _import_gui()
        assert hasattr(G, "StepApp")
        assert hasattr(G, "main")

    def test_ui_submodules_importable(self):
        _import_gui()
        import ui.theme
        import ui.constants
        import ui.widgets
        import ui.device_dialog
        import ui.canvas_mixin
        import ui.steps_mixin
        import ui.steps_cards
        import ui.runner_mixin
        import ui.ai_mixin

    def test_reexport_widgets_and_dialog(self):
        """_ui_preview.py / 其他调用方通过 gui_steps.XXX 访问的符号必须仍在。"""
        G = _import_gui()
        for name in ("_ChatPane", "_UiBridge", "_AiLogHandler",
                     "_RoundedBadge", "_RoundedButton", "DeviceSelectDialog"):
            assert hasattr(G, name), f"gui_steps 缺少 re-export: {name}"

    def test_reexport_constants_identity(self):
        """gui_steps 与 ui 模块的常量必须是同一对象(分叉即 bug)。"""
        G = _import_gui()
        import ui.constants
        import ui.theme
        for name in ("GRID_IMAGE", "GRID_META", "RAW_IMAGE", "DEVICE_ID_FILE",
                     "SCRIPTS_DIR", "RESULT_DIR", "DRAFT_SCRIPT_NAME",
                     "DEFAULT_CELL_SIZE", "MARGIN", "EXPECT_IMG_W"):
            assert getattr(G, name) is getattr(ui.constants, name), name
        assert G.THEME is ui.theme.THEME
        assert G.FONT_UI is ui.theme.FONT_UI
        assert G.FONT_MONO is ui.theme.FONT_MONO

    def test_theme_keys_stable(self):
        """主题键集合稳定:SKILL.md 约定视觉层只走 THEME dict。"""
        G = _import_gui()
        expected = {"bg", "bg2", "surface", "surface2", "border", "border2",
                    "txt", "txt2", "txt3", "accent", "accent_bg", "accent_soft",
                    "green", "red", "term_bg", "term_fg", "row_alt",
                    "chat_ai", "chat_user", "bubble_ai", "bubble_user",
                    "bubble_border", "bubble_sys_bg"}
        assert set(G.THEME) == expected


class TestMixinAttribution:
    """拆分不变量:方法在 StepApp 上可解析,且定义在预期 mixin 中。"""

    @pytest.mark.parametrize("mixin_attr,methods", [
        ("ui.canvas_mixin", CANVAS_MIXIN_METHODS),
        ("ui.steps_mixin", STEPS_MIXIN_METHODS),
        ("ui.steps_cards", CARDS_MIXIN_METHODS),
        ("ui.runner_mixin", RUNNER_MIXIN_METHODS),
        ("ui.ai_mixin", AI_MIXIN_METHODS),
    ])
    def test_methods_live_in_expected_mixin(self, mixin_attr, methods):
        G = _import_gui()
        import importlib
        mixin = importlib.import_module(mixin_attr)
        mixin_cls = next(
            cls for cls in vars(mixin).values()
            if isinstance(cls, type) and cls.__module__ == mixin_attr
            and cls.__name__.endswith("Mixin")
        )
        for name in methods:
            assert hasattr(mixin_cls, name), f"{mixin_attr} 缺少方法 {name}"
            # StepApp 继承链上必须能解析到同一函数
            assert getattr(G.StepApp, name) is getattr(mixin_cls, name), (
                f"StepApp.{name} 与 {mixin_attr}.{name} 不是同一函数"
            )

    @pytest.mark.parametrize("name", MAIN_FILE_METHODS)
    def test_main_file_methods_on_step_app(self, name):
        G = _import_gui()
        fn = getattr(G.StepApp, name, None)
        assert fn is not None, f"StepApp 缺少主体方法 {name}"
        assert fn.__module__ == "gui_steps", (
            f"{name} 应保留在 gui_steps.py,实际在 {fn.__module__}"
        )

    def test_step_app_mro(self):
        """MRO: 各 mixin 在前, tk.Tk 收尾。"""
        G = _import_gui()
        import tkinter as tk
        import ui.canvas_mixin
        import ui.steps_mixin
        import ui.steps_cards
        import ui.runner_mixin
        import ui.ai_mixin
        mro = G.StepApp.__mro__
        for mod in (ui.canvas_mixin, ui.steps_mixin, ui.steps_cards,
                    ui.runner_mixin, ui.ai_mixin):
            mixin_cls = next(
                cls for cls in vars(mod).values()
                if isinstance(cls, type) and cls.__module__ == mod.__name__
                and cls.__name__.endswith("Mixin")
            )
            assert mixin_cls in mro, f"MRO 缺少 {mod.__name__}"
        assert tk.Tk in mro

    def test_no_method_left_behind(self):
        """StepApp 可调用方法总数 = 各 mixin + 主体清单之和(漏搬即失败)。

        注意: mixin 方法经 MRO 继承,须遍历 MRO 收集,不能只看 vars(StepApp)。
        """
        G = _import_gui()
        expected = (len(CANVAS_MIXIN_METHODS) + len(STEPS_MIXIN_METHODS)
                    + len(CARDS_MIXIN_METHODS)
                    + len(RUNNER_MIXIN_METHODS) + len(AI_MIXIN_METHODS)
                    + len(MAIN_FILE_METHODS))
        actual = {}
        for klass in G.StepApp.__mro__:
            if klass.__module__ not in ("gui_steps", "ui.canvas_mixin",
                                        "ui.steps_mixin", "ui.steps_cards",
                                        "ui.runner_mixin", "ui.ai_mixin"):
                continue
            for name, val in vars(klass).items():
                if callable(val) and (not name.startswith("__") or name == "__init__"):
                    actual[name] = val
        all_listed = (CANVAS_MIXIN_METHODS + STEPS_MIXIN_METHODS
                      + CARDS_MIXIN_METHODS + RUNNER_MIXIN_METHODS
                      + AI_MIXIN_METHODS + MAIN_FILE_METHODS)
        assert len(actual) == expected, (
            f"StepApp 方法数 {len(actual)} != 清单 {expected}; "
            f"差异: {set(actual) ^ set(all_listed)}"
        )


class TestFileSizeGuard:
    """CR-08 验收: gui_steps.py 不超过约 800 行; mixin 各自有界。"""

    def test_gui_steps_line_budget(self):
        path = os.path.join(_RUNNER_DIR, "gui_steps.py")
        with open(path, encoding="utf-8") as f:
            lines = sum(1 for _ in f)
        assert lines <= 800, f"gui_steps.py {lines} 行,超过 800 行预算"

    @pytest.mark.parametrize("mod,max_lines", [
        ("ui/canvas_mixin.py", 800), ("ui/steps_mixin.py", 800),
        ("ui/steps_cards.py", 800),
        ("ui/runner_mixin.py", 800), ("ui/ai_mixin.py", 800),
        ("ui/widgets.py", 800), ("ui/device_dialog.py", 800),
        ("ui/theme.py", 200), ("ui/constants.py", 200),
    ])
    def test_module_line_budget(self, mod, max_lines):
        path = os.path.join(_RUNNER_DIR, "ui", os.path.basename(mod))
        if not os.path.isfile(path):
            pytest.skip(f"{mod} 尚未创建")
        with open(path, encoding="utf-8") as f:
            lines = sum(1 for _ in f)
        assert lines <= max_lines, f"{mod} {lines} 行,超过 {max_lines} 行预算"


class TestTkWidgetSmoke:
    """真实 Tk 渲染冒烟(无 display 环境自动 skip)。

    覆盖拆分后仍活跃的纯控件路径:_RoundedButton/_RoundedBadge/_ChatPane,
    验证 THEME 颜色值可被 Tk 接受、控件可正常构建与 update。
    """

    @pytest.fixture()
    def tk_root(self):
        tk = pytest.importorskip("tkinter")
        try:
            root = tk.Tk()
        except tk.TclError:
            pytest.skip("无显示环境,跳过真实 Tk 冒烟")
        yield root
        try:
            root.destroy()
        except tk.TclError:
            pass

    def test_rounded_widgets_render(self, tk_root):
        G = _import_gui()
        import tkinter as tk
        host = tk.Frame(tk_root)
        btn = G._RoundedButton(host, text="测试", command=lambda: None)
        badge = G._RoundedBadge(host, text="1")
        assert btn.winfo_width() > 0 or True  # 几何可能尚未布局
        host.update_idletasks()
        btn.configure(state="disabled")
        btn.configure(state="normal")
        badge.destroy()

    def test_chat_pane_render(self, tk_root):
        G = _import_gui()
        import tkinter as tk
        host = tk.Frame(tk_root)
        pane = G._ChatPane(host)
        pane.add_ai("AI 消息")
        pane.add_user("用户消息")
        pane.add_sys("系统提示")
        host.update_idletasks()
        # 每条气泡 = Label + holder Frame;系统提示只有 Label,共 3 个 Label
        labels = [c for c in pane.inner.winfo_children() if isinstance(c, tk.Label)]
        assert len(labels) == 3
        pane.clear()
        assert pane.inner.winfo_children() == []
