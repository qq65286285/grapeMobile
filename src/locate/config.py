"""
src/locate/config.py — 定位模块配置
=====================================
从 YAML 文件或环境变量加载配置,提供默认值。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("imgloc.locate.config")


class ConfigError(Exception):
    """配置解析或缺失异常。"""


@dataclass
class VLMConfig:
    provider: str = "openai_compat"
    base_url: str = "https://apihub.agnes-ai.com/v1"
    api_key: str = ""
    model: str = "agnes-3.0-flash"
    fallback_model: str = ""
    samples: int = 3            # 同一请求采样次数
    temperature: float = 0.4
    coord_format: str = "norm1000_xyxy"  # norm1000_xyxy | gemini_yxyx
    max_side: int = 1568        # 送入 VLM 的长边最大像素
    timeout: int = 120          # 秒


@dataclass
class AIConfig:
    """AI 客户端配置(OpenAI 兼容接口)。"""
    base_url: str = "https://apihub.agnes-ai.com/v1"
    api_key: str = ""
    model: str = "agnes-3.0-flash"
    timeout: int = 120
    max_tokens: int = 2048


@dataclass
class GridConfig:
    """网格配置。"""
    cell_size: int = 30


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
    ai: AIConfig = field(default_factory=AIConfig)
    grid: GridConfig = field(default_factory=GridConfig)


def _project_root() -> str:
    """返回项目根目录(此文件的上两级)。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_config(yaml_path: Optional[str] = None) -> LocateConfig:
    """
    从 YAML 文件加载配置,文件不存在则用默认值。

    无参数时默认读取项目根的 config.yaml。
    API Key 优先级:环境变量 LOCATE_VLM_KEY > config.yaml > 默认值。
    """
    cfg = LocateConfig()

    if yaml_path is None:
        yaml_path = os.path.join(_project_root(), "config.yaml")

    if yaml_path and os.path.isfile(yaml_path):
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ConfigError(
                f"配置文件解析失败({yaml_path}): {exc}"
            ) from exc

        # AI 段
        a = data.get("ai", {})
        if a:
            cfg.ai = AIConfig(
                base_url=a.get("base_url", cfg.ai.base_url),
                api_key=a.get("api_key", cfg.ai.api_key),
                model=a.get("model", cfg.ai.model),
                timeout=a.get("timeout", cfg.ai.timeout),
                max_tokens=a.get("max_tokens", cfg.ai.max_tokens),
            )

        # locate 段
        lc = data.get("locate", {})
        cfg.mode = lc.get("mode", cfg.mode)
        v = lc.get("vlm", {})
        if v:
            cfg.vlm = VLMConfig(
                provider=v.get("provider", cfg.vlm.provider),
                base_url=v.get("base_url", cfg.vlm.base_url),
                api_key=v.get("api_key", cfg.vlm.api_key),
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

        # grid 段
        g = data.get("grid", {})
        if g:
            cfg.grid = GridConfig(
                cell_size=g.get("cell_size", cfg.grid.cell_size),
            )

    # 环境变量优先级最高:覆盖 YAML 值
    env_key = os.environ.get("LOCATE_VLM_KEY")
    if env_key:
        cfg.vlm.api_key = env_key
        cfg.ai.api_key = env_key

    # 同步 ai 与 vlm 的 key:若一方为空则用另一方
    if not cfg.ai.api_key and cfg.vlm.api_key:
        cfg.ai.api_key = cfg.vlm.api_key
    if not cfg.vlm.api_key and cfg.ai.api_key:
        cfg.vlm.api_key = cfg.ai.api_key

    return cfg
