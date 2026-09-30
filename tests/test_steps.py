"""
测试:步骤编排执行引擎 (src/steps.py)
覆盖:步骤校验/规范化(含 before_image/after_image 标准图字段、旧 expect_image 兼容)、
JSON 保存与加载、执行逻辑(固定等待 / 开始条件校验 / 轮询等待画面到达 / 超时告警),
用假 client 隔离真实 adb。
"""

import os

import numpy as np
import pytest

from steps import StepError, StepRunner, load_steps, save_steps, validate_steps


def _solid_image(color, shape=(120, 160, 3)):
    """生成纯色测试图。"""
    img = np.zeros(shape, dtype=np.uint8)
    img[:, :] = color
    return img


class TestValidateSteps:
    def test_tap_defaults_fill_zero(self):
        """未写字段时间字段默认补 0,标准图默认空串。"""
        steps = validate_steps([{"type": "tap", "cell": "u18"}])
        assert steps == [
            {"type": "tap", "cell": "U18", "x": None, "y": None,
             "delay_before": 0.0,
             "wait_after": 0.0, "before_image": "", "after_image": ""}
        ]

    def test_tap_with_waits(self):
        steps = validate_steps([
            {"type": "tap", "cell": "U18", "delay_before": 2, "wait_after": 10},
        ])
        assert steps[0]["delay_before"] == 2.0
        assert steps[0]["wait_after"] == 10.0
        assert steps[0]["before_image"] == ""
        assert steps[0]["after_image"] == ""

    def test_before_after_images_kept(self):
        steps = validate_steps([
            {"type": "tap", "cell": "A1",
             "before_image": "images/before_0001.png",
             "after_image": "images/after_0001.png"},
        ])
        assert steps[0]["before_image"] == "images/before_0001.png"
        assert steps[0]["after_image"] == "images/after_0001.png"

    def test_legacy_expect_image_maps_to_before(self):
        """旧脚本 expect_image 字段加载时自动映射为 before_image。"""
        steps = validate_steps([
            {"type": "tap", "cell": "A1", "expect_image": "images/before_0001.png"},
        ])
        assert steps[0]["before_image"] == "images/before_0001.png"
        assert steps[0]["after_image"] == ""
        assert "expect_image" not in steps[0]

    def test_before_image_takes_precedence_over_expect(self):
        steps = validate_steps([
            {"type": "tap", "cell": "A1",
             "before_image": "images/before_new.png",
             "expect_image": "images/old.png"},
        ])
        assert steps[0]["before_image"] == "images/before_new.png"

    @pytest.mark.parametrize("field", ["before_image", "after_image"])
    def test_image_field_non_string_rejected(self, field):
        with pytest.raises(StepError):
            validate_steps([{"type": "tap", "cell": "A1", field: 123}])

    @pytest.mark.parametrize("bad", [
        [],
        "not-a-list",
        [{"type": "tap"}],                                # 缺 cell
        [{"type": "tap", "cell": "3U"}],                  # 格子编号非法
        [{"type": "tap", "cell": "A1", "wait_after": -1}],      # 负数
        [{"type": "tap", "cell": "A1", "delay_before": "abc"}], # 非数字
        [{"type": "wait", "seconds": 10}],                # 独立 wait 步骤已移除
        ["tap"],                                          # 非对象
    ])
    def test_invalid_steps_raise(self, bad):
        with pytest.raises(StepError):
            validate_steps(bad)


class TestSaveLoadSteps:
    def test_round_trip(self):
        import tempfile, os
        steps = [{"type": "tap", "cell": "A3", "wait_after": 5}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "steps.json")
            save_steps(steps, path)
            loaded = load_steps(path)
        assert loaded == [
            {"type": "tap", "cell": "A3", "x": None, "y": None,
             "delay_before": 0.0,
             "wait_after": 5.0, "before_image": "", "after_image": ""}
        ]

    def test_pixel_coords_kept(self):
        """AI 定位的精确像素坐标 x/y 被保留,cell 可省略。"""
        steps = validate_steps([
            {"type": "tap", "cell": "Y6", "x": 735, "y": 176},
        ])
        assert steps[0]["x"] == 735 and steps[0]["y"] == 176
        # 无 cell 但有 x/y 也合法
        steps2 = validate_steps([{"type": "tap", "x": 100, "y": 200}])
        assert steps2[0]["cell"] == "" and steps2[0]["x"] == 100

    def test_partial_coords_rejected(self):
        """只给 x 不给 y 报错。"""
        with pytest.raises(StepError):
            validate_steps([{"type": "tap", "cell": "A1", "x": 100}])

    def test_round_trip_with_images(self, tmp_path):
        steps = [{"type": "tap", "cell": "A1"},
                 {"type": "tap", "cell": "B2",
                  "before_image": "images/before_0002.png",
                  "after_image": "images/after_0001.png"}]
        path = tmp_path / "steps.json"
        save_steps(steps, str(path))
        loaded = load_steps(str(path))
        assert loaded[1]["before_image"] == "images/before_0002.png"
        assert loaded[1]["after_image"] == "images/after_0001.png"

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(StepError):
            load_steps(str(tmp_path / "nope.json"))

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(StepError):
            load_steps(str(path))


class _FakeClient:
    """
    替代 AdbClient,隔离真实 adb:
      - tap 记录点击坐标;
      - screenshot(save_path=) 按队列逐张写入预置截图(模拟页面跳转过程)。
    """

    def __init__(self, shot_sequence=None):
        import cv2
        self.cv2 = cv2
        self.taps = []
        self.shot_calls = 0
        self.shot_sequence = list(shot_sequence or [])

    def attach(self, device_id):
        pass

    def tap(self, x, y):
        self.taps.append((x, y))

    def screenshot(self, save_path=None):
        self.shot_calls += 1
        idx = min(self.shot_calls - 1, len(self.shot_sequence) - 1)
        img = self.shot_sequence[idx]
        if save_path:
            self.cv2.imwrite(save_path, img)
        return save_path


class TestScreenSimilarity:
    def test_identical_images_score_one(self, tmp_path):
        import cv2
        p = str(tmp_path / "a.png")
        cv2.imwrite(p, _solid_image((10, 120, 200)))
        assert StepRunner.screen_similarity(p, p) == pytest.approx(1.0, abs=1e-6)

    def test_different_images_score_low(self, tmp_path):
        import cv2
        a = str(tmp_path / "a.png")
        b = str(tmp_path / "b.png")
        img_a = np.zeros((120, 160), dtype=np.uint8)
        img_b = np.zeros((120, 160), dtype=np.uint8)
        img_a[10:60, 10:60] = 255
        img_b[70:115, 100:150] = 255  # 亮块在完全不同位置
        cv2.imwrite(a, img_a)
        cv2.imwrite(b, img_b)
        assert StepRunner.screen_similarity(a, b) < 0.5

    def test_unreadable_returns_zero(self, tmp_path):
        p = str(tmp_path / "a.png")
        assert StepRunner.screen_similarity(p, p) == 0.0


class TestStepRunnerExecution:
    def _make_runner(self, runner_dir=None, poll_interval=0.01):
        # 1280x720, cell=30:U18 -> col20 row17 -> (615,525);F1 -> col5 row0 -> (165,15)
        runner = StepRunner(
            "emulator-5554", cell_size=30, image_shape=(720, 1280, 3),
            runner_dir=runner_dir or ".", poll_interval=poll_interval,
        )
        return runner

    def test_run_executes_in_order(self):
        runner = self._make_runner()
        runner._client = _FakeClient()
        events = []
        runner.run(
            [{"type": "tap", "cell": "U18", "wait_after": 0.03},
             {"type": "tap", "cell": "F1", "delay_before": 0.03}],
            on_event=lambda i, n, m: events.append((i, m)),
            wait_tick=0.01,
        )
        assert runner._client.taps == [(615, 525), (165, 15)]
        msgs = [m for _, m in events]
        assert any("U18" in m for m in msgs)
        assert any("F1" in m for m in msgs)
        assert any("执行后等待" in m for m in msgs)   # U18 的 wait_after
        assert any("执行前等待" in m for m in msgs)   # F1 的 delay_before
        assert any("完成" in m for m in msgs)

    def test_zero_wait_skips_wait_events(self):
        runner = self._make_runner()
        runner._client = _FakeClient()
        events = []
        runner.run([{"type": "tap", "cell": "A1"}],
                   on_event=lambda i, n, m: events.append(m), wait_tick=0.01)
        assert not any("等待" in m for m in events)

    def test_pixel_coords_override_cell(self):
        """带 x/y 的步骤直接点击像素坐标,不做格子换算。"""
        runner = self._make_runner()
        runner._client = _FakeClient()
        runner.run(
            [{"type": "tap", "cell": "Y6", "x": 735, "y": 176}],
            on_event=lambda i, n, m: None, wait_tick=0.01,
        )
        assert runner._client.taps == [(735, 176)]

    def test_run_revalidates_steps(self):
        runner = self._make_runner()
        with pytest.raises(StepError):
            runner.run([{"type": "tap", "cell": "!!"}])

    def test_from_capture_artifacts_missing(self, tmp_path):
        with pytest.raises(StepError):
            StepRunner.from_capture_artifacts(str(tmp_path))


class TestStartCondition:
    """执行前标准图(before_image):点击前截图校验,不一致只告警不阻断。"""

    def test_matching_start_condition_reported(self, tmp_path):
        import cv2
        screen = _solid_image((30, 100, 200))
        anchor = tmp_path / "before.png"
        cv2.imwrite(str(anchor), screen)
        runner = StepRunner(
            "emulator-5554", cell_size=30, image_shape=(120, 160, 3),
            runner_dir=str(tmp_path),
        )
        runner._client = _FakeClient([screen])
        events = []
        runner.run(
            [{"type": "tap", "cell": "A1", "before_image": str(anchor)}],
            on_event=lambda i, n, m: events.append(m),
        )
        msgs = "\n".join(events)
        assert "开始条件满足" in msgs
        assert runner._client.taps == [(15, 15)]  # 仍正常点击

    def test_mismatched_start_condition_warns_but_taps(self, tmp_path):
        import cv2
        actual = _solid_image((10, 10, 200))
        anchor_img = _solid_image((200, 30, 30))
        anchor = tmp_path / "before.png"
        cv2.imwrite(str(anchor), anchor_img)
        runner = StepRunner(
            "emulator-5554", cell_size=30, image_shape=(120, 160, 3),
            runner_dir=str(tmp_path),
        )
        runner._client = _FakeClient([actual])
        events = []
        runner.run(
            [{"type": "tap", "cell": "A1", "before_image": str(anchor)}],
            on_event=lambda i, n, m: events.append(m),
        )
        msgs = "\n".join(events)
        assert "与执行前标准图不一致" in msgs
        assert runner._client.taps == [(15, 15)]  # 告警但不阻断


class TestExpectScreenPolling:
    """点击后轮询等待完成条件(本步 after_image 优先,缺省用下一步 before_image)。"""

    def _setup(self, tmp_path, shot_sequence, expected_img):
        import cv2
        anchor = tmp_path / "anchor.png"
        cv2.imwrite(str(anchor), expected_img)
        runner = StepRunner(
            "emulator-5554", cell_size=30, image_shape=(720, 1280, 3),
            runner_dir=str(tmp_path), poll_interval=0.005,
        )
        runner._client = _FakeClient(shot_sequence)
        return runner, str(anchor)

    def test_proceed_early_when_screen_matches(self, tmp_path):
        expected = _solid_image((30, 100, 200))
        # 截图序列:轮询第1张旧画面(红),第2张到达(蓝);
        # 第3张给 B2 的开始条件校验(蓝,与 before 一致)
        runner, anchor = self._setup(
            tmp_path,
            shot_sequence=[_solid_image((200, 30, 30)), expected, expected],
            expected_img=expected,
        )
        events = []
        steps = [
            {"type": "tap", "cell": "A1", "wait_after": 5},
            {"type": "tap", "cell": "B2", "before_image": anchor},
        ]
        runner.run(steps, on_event=lambda i, n, m: events.append(m), wait_tick=0.01)
        msgs = "\n".join(events)
        assert "画面已到达预期状态" in msgs
        assert "提前" in msgs
        assert runner._client.taps == [(15, 15), (45, 45)]  # 两步都执行了
        # 2 次轮询 + 1 次 B2 开始条件校验
        assert runner._client.shot_calls == 3

    def test_explicit_after_image_is_wait_target(self, tmp_path):
        """本步配了 after_image 时,以它为完成条件(而不是下一步 before)。"""
        after = _solid_image((30, 100, 200))
        runner, after_path = self._setup(
            tmp_path,
            shot_sequence=[after, _solid_image((1, 1, 1))],
            expected_img=after,
        )
        events = []
        steps = [
            {"type": "tap", "cell": "A1", "wait_after": 5, "after_image": after_path},
            {"type": "tap", "cell": "B2",
             "before_image": os.path.join(str(tmp_path), "nonexistent.png")},
        ]
        runner.run(steps, on_event=lambda i, n, m: events.append(m), wait_tick=0.01)
        msgs = "\n".join(events)
        assert "画面已到达预期状态" in msgs
        # B2 开始条件图不存在 -> 告警但继续点击
        assert "执行前标准图不存在" in msgs
        assert len(runner._client.taps) == 2

    def test_timeout_warns_and_continues(self, tmp_path):
        expected = _solid_image((30, 100, 200))
        runner, anchor = self._setup(
            tmp_path,
            shot_sequence=[_solid_image((200, 30, 30))],  # 始终是不同画面
            expected_img=expected,
        )
        events = []
        steps = [
            {"type": "tap", "cell": "A1", "wait_after": 0.05},
            {"type": "tap", "cell": "B2", "before_image": anchor},  # A1 的完成条件
        ]
        t0 = __import__("time").monotonic()
        runner.run(steps, on_event=lambda i, n, m: events.append(m), wait_tick=0.01)
        elapsed = __import__("time").monotonic() - t0
        msgs = "\n".join(events)
        assert "画面仍未到达执行后标准图" in msgs        # 超时告警(新文案)
        assert "与执行前标准图不一致" in msgs            # B2 开始条件也不满足(仍继续)
        assert elapsed < 1.0                            # 没有死等 5 秒
        assert len(runner._client.taps) == 2            # 超时仍继续下一步

    def test_missing_target_falls_back_to_fixed_wait(self, tmp_path):
        runner = StepRunner(
            "emulator-5554", cell_size=30, image_shape=(720, 1280, 3),
            runner_dir=str(tmp_path), poll_interval=0.005,
        )
        runner._client = _FakeClient()
        events = []
        steps = [
            {"type": "tap", "cell": "A1", "wait_after": 0.03},
            {"type": "tap", "cell": "B2", "before_image": "images/missing.png"},
        ]
        runner.run(steps, on_event=lambda i, n, m: events.append(m), wait_tick=0.01)
        msgs = "\n".join(events)
        assert "执行后标准图不存在" in msgs  # 完成条件取下一步 before,但文件缺失
        assert "执行后等待" in msgs          # 退化为固定等待
        assert runner._client.shot_calls == 0


class TestScriptDirResolution:
    """标准图相对路径以脚本目录(script_dir)解析,与 capture 产物目录解耦。"""

    def test_default_script_dir_equals_runner_dir(self, tmp_path):
        runner = StepRunner("dev", 30, (720, 1280, 3), runner_dir=str(tmp_path))
        assert runner.script_dir == str(tmp_path)
        assert runner._resolve_expect_path("images/a.png").replace("\\", "/").endswith(
            "images/a.png")

    def test_anchor_resolved_against_script_dir(self, tmp_path):
        # 标准布局:runner/scripts/<脚本名>/<脚本名>.json + images/
        runner_dir = tmp_path / "runner"
        script_dir = runner_dir / "scripts" / "steps-1"
        images_dir = script_dir / "images"
        images_dir.mkdir(parents=True)
        anchor = images_dir / "before_0002.png"
        anchor.write_bytes(b"x")
        runner = StepRunner(
            "dev", 30, (720, 1280, 3),
            runner_dir=str(runner_dir), script_dir=str(script_dir),
        )
        resolved = runner._resolve_expect_path("images/before_0002.png")
        assert os.path.normpath(resolved) == os.path.normpath(str(anchor))

    def test_absolute_anchor_path_unchanged(self, tmp_path):
        runner = StepRunner("dev", 30, (720, 1280, 3), runner_dir=str(tmp_path))
        abs_path = os.path.abspath(__file__)
        assert runner._resolve_expect_path(abs_path) == abs_path
