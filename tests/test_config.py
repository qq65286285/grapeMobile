"""
测试：配置加载与深度合并逻辑
"""

import pytest

from imgloc.config import load_config, DEFAULT_CONFIG
from imgloc.exceptions import ConfigError


class TestLoadConfig:
    def test_none_returns_default_copy(self):
        cfg = load_config(None)
        assert cfg == DEFAULT_CONFIG
        # 确保是深拷贝，修改不会影响全局默认配置
        cfg["template"]["threshold"] = 0.1
        assert DEFAULT_CONFIG["template"]["threshold"] != 0.1

    def test_dict_override_merges_deeply(self):
        cfg = load_config({"template": {"threshold": 0.95}})
        assert cfg["template"]["threshold"] == 0.95
        # 未覆盖的字段应保留默认值
        assert cfg["template"]["method"] == DEFAULT_CONFIG["template"]["method"]

    def test_dict_override_adds_new_router_order(self):
        cfg = load_config({"router": {"order": ["template", "orb"]}})
        assert cfg["router"]["order"] == ["template", "orb"]

    def test_unsupported_type_raises(self):
        with pytest.raises(ConfigError):
            load_config(12345)  # type: ignore[arg-type]

    def test_nonexistent_yaml_path_raises(self):
        with pytest.raises(ConfigError):
            load_config("nonexistent_config_file.yaml")

    def test_yaml_file_merge(self, tmp_path):
        yaml_content = "template:\n  threshold: 0.6\n"
        cfg_file = tmp_path / "cfg.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")
        cfg = load_config(str(cfg_file))
        assert cfg["template"]["threshold"] == 0.6
        assert "shape" in cfg  # 其余默认层依然存在

    def test_yaml_non_mapping_raises(self, tmp_path):
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(str(cfg_file))
