"""
src/locate/config.py — 定位模块配置
=====================================
从 YAML 文件或环境变量加载配置,提供默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class VLMConfig:
    provider: str = "openai_compat"
    base_url: str = "https://apihub.agnes-ai.com/v1"
    api_key: str = ""           # 从环境变量 LOCATE_VLM_KEY 读取
    model: str = "agnes-3.0-flash"
    fallback_model: str = ""
    samples: int = 3            # 同一请求采样次数
    temperature: float = 0.4
    coord_format: str = "norm1000_xyxy"  # norm1000_xyxy | gemini_yxyx
    max_side: int = 1568        # 送入 VLM 的长边最大像素
    timeout: int = 120          # 秒


@dataclass
class OCRConfig:
    engine: str = "rapidocr"    # rapidocr | paddleocr
    fuzzy_threshold: float = 0.8  # 编辑距离相似度阈值
    icon_search_ratio: float = 3.5  # 图标搜索区域高度 = 文字高 × 此值


@dataclass
class VerifyConfig:
    enabled: bool = True
    crop_expand: float = 0.2
    min_size: int = 256
    top_k: int = 3              # 验证前保留的候选数


@dataclass
class FusionConfig:
    weights: Dict[str, float] = field(default_factory=lambda: {
        "template": 0.5,
        "ocr_exact": 0.35,
        "ocr_fuzzy": 0.2,
        "vlm_consistent": 0.3,
        "multi_source": 0.15,
    })


@dataclass
class DebugConfig:
    save_images: bool = True
    dir: str = "runner/tmp/locate_debug"


@dataclass
class LocateConfig:
    mode: str = "full"          # full | ocr_only | vlm_only
    vlm: VLMConfig = field(default_factory=VLMConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)


def load_config(yaml_path: Optional[str] = None) -> LocateConfig:
    """
    从 YAML 文件加载配置,文件不存在则用默认值。
    API Key 从环境变量 LOCATE_VLM_KEY 读取。
    """
    cfg = LocateConfig()

    if yaml_path and os.path.isfile(yaml_path):
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            lc = data.get("locate", {})
            cfg.mode = lc.get("mode", cfg.mode)
            v = lc.get("vlm", {})
            if v:
                cfg.vlm = VLMConfig(
                    provider=v.get("provider", cfg.vlm.provider),
                    base_url=v.get("base_url", cfg.vlm.base_url),
                    api_key="",  # 始终从环境变量读
                    model=v.get("model", cfg.vlm.model),
                    fallback_model=v.get("fallback_model", ""),
                    samples=v.get("samples", cfg.vlm.samples),
                    temperature=v.get("temperature", cfg.vlm.temperature),
                    coord_format=v.get("coord_format", cfg.vlm.coord_format),
                    max_side=v.get("max_side", cfg.vlm.max_side),
                    timeout=v.get("timeout", cfg.vlm.timeout),
                )
            o = lc.get("ocr", {})
            if o:
                cfg.ocr = OCRConfig(
                    engine=o.get("engine", cfg.ocr.engine),
                    fuzzy_threshold=o.get("fuzzy_threshold", cfg.ocr.fuzzy_threshold),
                    icon_search_ratio=o.get("icon_search_ratio", cfg.ocr.icon_search_ratio),
                )
            ver = lc.get("verify", {})
            if ver:
                cfg.verify = VerifyConfig(
                    enabled=ver.get("enabled", cfg.verify.enabled),
                    crop_expand=ver.get("crop_expand", cfg.verify.crop_expand),
                    min_size=ver.get("min_size", cfg.verify.min_size),
                    top_k=ver.get("top_k", cfg.verify.top_k),
                )
            fus = lc.get("fusion", {})
            if fus and "weights" in fus:
                cfg.fusion = FusionConfig(weights=fus["weights"])
            dbg = lc.get("debug", {})
            if dbg:
                cfg.debug = DebugConfig(
                    save_images=dbg.get("save_images", cfg.debug.save_images),
                    dir=dbg.get("dir", cfg.debug.dir),
                )
        except Exception:
            pass  # 配置解析失败,用默认值

    # API Key 从环境变量读
    cfg.vlm.api_key = os.environ.get("LOCATE_VLM_KEY", cfg.vlm.api_key)

    return cfg
