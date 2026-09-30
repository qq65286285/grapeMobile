"""
imgloc 配置加载工具
====================
支持从 dict 或 YAML 文件加载各算法层的参数配置，并与内置默认配置做深度合并，
保证用户只需覆盖自己关心的字段，未提供的字段自动回退到默认值。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Union

import yaml

from imgloc.exceptions import ConfigError

# 各算法层的内置默认参数。
# 用户可通过 dict 或 YAML 文件覆盖任意子字段，未覆盖的字段保留默认值。
DEFAULT_CONFIG: Dict[str, Any] = {
    # 全局路由配置
    "router": {
        # 责任链尝试顺序，可自由增删/调整顺序
        "order": ["template", "shape", "orb", "sift", "lightglue"],
        # 全局统一阈值，各层可通过自己的配置覆盖
        "default_threshold": 0.8,
        # 是否在某一层"抛出异常"(如可选依赖缺失)时静默跳过而不中断整体流程
        "skip_on_error": True,
    },
    # 模板匹配层
    "template": {
        "threshold": 0.85,
        "method": "TM_CCOEFF_NORMED",
        # ---- 尺度搜索策略 ----
        # two_pass=True（默认，推荐）：两级搜索兼顾速度与跨分辨率鲁棒性
        #   第1级：仅以原始尺度 1.0 快速匹配一次，同分辨率截图场景直接命中返回；
        #   第2级：未达阈值时，先在 fallback_scale_range 内以粗步长找最优尺度，
        #          再在最优尺度邻域以 refine_scale_step 精搜，避免漏掉尖锐的尺度峰。
        # two_pass=False：退化为传统单遍遍历，使用下方 scale_range / scale_step。
        "two_pass": True,
        # 两级搜索之粗搜范围/步长（覆盖真机1080p模板用于720p模拟器等跨DPI场景）
        "fallback_scale_range": [0.5, 3.0],
        "fallback_scale_step": 0.1,
        # 粗搜命中后，在最优尺度 ±1 个粗步长内的精搜步长
        "refine_scale_step": 0.02,
        # 单遍遍历模式（two_pass=False）使用的范围与步长
        "scale_range": [0.5, 1.5],
        "scale_step": 0.05,
        # 是否使用彩色三通道加权匹配（False 则转灰度匹配，速度更快）
        "use_color": True,
        # RGB 三通道权重（配合 use_color=True），可根据UI配色特点调整
        "channel_weights": [0.34, 0.33, 0.33],
        # 是否额外做旋转不变匹配（会显著增加耗时，默认关闭）
        "rotation_invariant": False,
        "rotation_range": [-15, 15],
        "rotation_step": 5,
    },
    # 形状匹配层（基于边缘方向梯度）
    "shape": {
        "threshold": 0.75,
        "canny_low": 50,
        "canny_high": 150,
        "num_features": 128,
        "scale_range": [0.8, 1.2],
        "scale_step": 0.1,
        "angle_range": [-10, 10],
        "angle_step": 5,
        # 颜色一致性二次校验：形状匹配只看边缘方向、不看颜色语义，
        # 可能把"尺寸相近但颜色完全不同"的元素误判为命中（实测可区分度极高：
        # 同色按钮 HSV 直方图相关≈0.99，异色误配≈0.03）。
        # 开启后，命中位置颜色与模板不一致时拒绝该结果并继续责任链降级；
        # 深色模式/换肤等"同形变色"场景可关闭此项，交由后续特征点层处理。
        "color_verify": True,
        "color_hist_threshold": 0.5,
    },
    # ORB 特征点匹配层
    "orb": {
        "threshold": 0.35,
        "n_features": 500,
        "matcher_type": "bf",  # bf(BFMatcher) / flann
        "min_match_count": 8,
        "ransac_reproj_threshold": 5.0,
        "knn_ratio": 0.75,
    },
    # SIFT 特征点匹配层
    "sift": {
        "threshold": 0.35,
        "n_features": 0,  # 0 表示不限制
        "matcher_type": "flann",
        "min_match_count": 8,
        "ransac_reproj_threshold": 5.0,
        "knn_ratio": 0.75,
    },
    # LightGlue 深度学习兜底层（可选依赖）
    "lightglue": {
        "threshold": 0.3,
        "device": "auto",  # auto / cpu / cuda
        "extractor": "superpoint",  # superpoint / disk / aliked
        "max_keypoints": 1024,
        "min_match_count": 6,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归深度合并两个 dict，override 中的值优先生效。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(config: Optional[Union[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    加载配置并与默认配置合并。

    Args:
        config: 可以是：
            - None：直接返回内置默认配置的深拷贝
            - dict：与默认配置深度合并
            - str：YAML 文件路径，读取后与默认配置深度合并

    Returns:
        合并后的完整配置字典。

    Raises:
        ConfigError: YAML 文件不存在/解析失败，或 config 类型不支持时抛出。
    """
    if config is None:
        return copy.deepcopy(DEFAULT_CONFIG)

    if isinstance(config, dict):
        return _deep_merge(DEFAULT_CONFIG, config)

    if isinstance(config, str):
        try:
            with open(config, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
        except FileNotFoundError as e:
            raise ConfigError(f"配置文件不存在: {config}") from e
        except yaml.YAMLError as e:
            raise ConfigError(f"配置文件解析失败: {config}, 错误: {e}") from e
        if not isinstance(user_cfg, dict):
            raise ConfigError(f"配置文件内容必须是一个 YAML 映射(dict): {config}")
        return _deep_merge(DEFAULT_CONFIG, user_cfg)

    raise ConfigError(f"不支持的配置类型: {type(config)}，仅支持 None / dict / str(YAML路径)")
