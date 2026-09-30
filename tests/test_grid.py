"""
测试：网格坐标标注器 (src/grid.py)
覆盖：Excel 风格列名进位、格子编号解析、几何换算（含末尾不完整格/越界）、
标注图绘制（边距外扩/不改原图）。
"""

import numpy as np
import pytest

from grid import GridMarker, col_index, col_name, parse_ref


class TestColumnNameConversion:
    @pytest.mark.parametrize("idx,name", [
        (0, "A"), (25, "Z"), (26, "AA"), (27, "AB"),
        (51, "AZ"), (52, "BA"), (701, "ZZ"), (702, "AAA"),
    ])
    def test_col_name_examples(self, idx, name):
        assert col_name(idx) == name

    @pytest.mark.parametrize("name,idx", [
        ("A", 0), ("Z", 25), ("AA", 26), ("aZ", 51), ("zz", 701),
    ])
    def test_col_index_examples(self, name, idx):
        assert col_index(name) == idx

    @pytest.mark.parametrize("idx", [0, 25, 26, 701, 702, 1000])
    def test_round_trip(self, idx):
        assert col_index(col_name(idx)) == idx

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            col_name(-1)

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError):
            col_index("A1")


class TestParseRef:
    @pytest.mark.parametrize("ref,expected", [
        ("A3", (0, 2)), ("a1", (0, 0)), ("Z9", (25, 8)),
        ("AA12", (26, 11)), ("  b9 ", (1, 8)),
    ])
    def test_valid_refs(self, ref, expected):
        assert parse_ref(ref) == expected

    @pytest.mark.parametrize("bad", ["", "3A", "A", "12", "ABCD1", "A0", "AAA1X", None])
    def test_invalid_refs(self, bad):
        with pytest.raises(ValueError):
            parse_ref(bad)


class TestGridGeometry:
    def test_grid_dims(self):
        shape = (1280, 720, 3)
        assert GridMarker(60).grid_dims(shape) == (12, 22)
        assert GridMarker(40).grid_dims(shape) == (18, 32)
        assert GridMarker(80).grid_dims(shape) == (9, 16)

    def test_full_cell_center(self):
        marker = GridMarker(60)
        shape = (1280, 720, 3)
        # A3: 第1列第3行（0基 0,2）中心 = (30, 150)
        assert marker.cell_center(0, 2, shape) == (30, 150)
        # G12（0基 6,11）= (390, 690)
        assert marker.cell_center(6, 11, shape) == (390, 690)

    def test_last_partial_row_center(self):
        # 1280 高 / 60 = 21 整行余 20px，最后行(21)中心应为 1260 + 20//2 = 1270
        marker = GridMarker(60)
        x, y = marker.cell_center(11, 21, (1280, 720, 3))
        assert x == 690  # 720/60 整除，末列完整
        assert y == 1270

    def test_ref_to_point(self):
        assert GridMarker(60).ref_to_point("F12", (1280, 720, 3)) == (330, 690)

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            GridMarker(60).cell_center(12, 0, (1280, 720, 3))  # 仅 12 列(0~11)
        with pytest.raises(ValueError):
            GridMarker(60).cell_center(0, 22, (1280, 720, 3))  # 仅 22 行(0~21)

    def test_cell_size_too_small_raises(self):
        with pytest.raises(ValueError):
            GridMarker(10)


class TestGridDraw:
    def test_draw_expands_margins_and_keeps_source_unchanged(self):
        # 用结构化随机图（非纯色），便于验证原图内容确实被 1:1 嵌入标注区
        rng = np.random.RandomState(0)
        src = rng.randint(0, 256, (120, 200, 3), dtype=np.uint8)
        src_copy = src.copy()
        marker = GridMarker(cell_size=40, margin=30)
        out = marker.draw(src)
        assert out.shape == (120 + 60, 200 + 60, 3)
        # 原图 1:1 嵌入（半透明网格只做轻度叠加），中心区域与原图高度相关
        inner = out[30:150, 30:230].astype(np.float32).ravel()
        corr = float(np.corrcoef(inner, src.astype(np.float32).ravel())[0, 1])
        assert corr > 0.9
        # 不修改原数组
        assert np.array_equal(src, src_copy)

    def test_draw_accepts_grayscale(self):
        gray = np.full((100, 100), 128, dtype=np.uint8)
        out = GridMarker(40, margin=24).draw(gray)
        assert out.ndim == 3 and out.shape[2] == 3


class TestDecimalRowRef:
    """小数行号:连续行坐标,如 AP1.5 表示 AP 列第 1/2 行之间的网格线。"""

    SHAPE = (720, 1280, 3)  # 30px 格: 43 列 x 24 行

    def test_parse_decimal_row(self):
        assert parse_ref("AP1.5") == (41, 0.5)
        assert parse_ref("A3") == (0, 2.0)  # 整数行兼容

    def test_decimal_row_lands_on_grid_line(self):
        m = GridMarker(cell_size=30)
        # AP1.5 -> x=列AP中心 41*30+15=1245, y=第1/2行分界线=30
        assert m.ref_to_point("AP1.5", self.SHAPE) == (1245, 30)

    def test_decimal_row_quarter_offset(self):
        m = GridMarker(cell_size=30)
        # R7.25 -> x=列R中心 17*30+15=525, y=(6.25+0.5)*30=202.5->202
        assert m.ref_to_point("R7.25", self.SHAPE) == (525, 202)

    def test_integer_row_behavior_unchanged(self):
        m = GridMarker(cell_size=30)
        assert m.ref_to_point("A3", self.SHAPE) == (15, 75)

    def test_decimal_row_out_of_range(self):
        m = GridMarker(cell_size=30)
        with pytest.raises(ValueError):
            m.ref_to_point("A99.5", self.SHAPE)

    def test_decimal_row_below_one_rejected(self):
        with pytest.raises(ValueError):
            parse_ref("A0.5")
