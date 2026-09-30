"""
LightGlue 深度学习兜底层 (Deep Learning Fallback Layer)
=========================================================
集成 LightGlue (https://github.com/cvg/LightGlue) 作为责任链的最后一层，
用于应对前几层传统算法都失败的复杂场景：大形变、跨主题UI(深色/浅色切换)、
弱纹理图标、严重遮挡等。

设计要点：
    - 本层依赖 torch + kornia + lightglue，均为"可选依赖"(imgloc[deep])
    - 未安装这些依赖时，is_available() 返回 False，
      MultiStrategyLocator 会自动跳过本层，不影响核心库的轻量化
    - 所有 import 都在方法内部做"惰性导入"(lazy import)，
      即使本文件被 import，只要不实际调用 LightGlueMatcher，
      也不会因缺少 torch 而报错

参考：LightGlue 官方 API 用法(LightGlue + SuperPoint/DISK/ALIKED 特征提取器)。
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from imgloc.base import Matcher
from imgloc.exceptions import MatcherNotAvailableError
from imgloc.types import MatchResult
from imgloc.utils import ensure_bgr


def _check_deps_available() -> bool:
    """检测 torch/kornia/lightglue 是否均已安装，不抛异常，仅返回布尔值。"""
    try:
        import torch  # noqa: F401
        import lightglue  # noqa: F401
    except ImportError:
        return False
    return True


class LightGlueMatcher(Matcher):
    """
    深度学习兜底层：LightGlue + (SuperPoint/DISK/ALIKED) 特征匹配。

    仅在安装了可选依赖 (pip install imgloc[deep]) 时可用；
    未安装时 is_available() 返回 False，match() 会抛出
    MatcherNotAvailableError，由 MultiStrategyLocator 捕获并跳过本层。
    """

    name = "lightglue"

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self._extractor = None
        self._matcher = None
        self._device = None

    def is_available(self) -> bool:
        return _check_deps_available()

    def _lazy_init_models(self) -> None:
        """惰性初始化模型（首次调用时才真正加载权重，避免拖慢库导入速度）。"""
        if self._matcher is not None:
            return
        if not self.is_available():
            raise MatcherNotAvailableError(
                "LightGlueMatcher 依赖未安装。请运行: pip install imgloc[deep] "
                "(需要 torch / torchvision / kornia / lightglue)"
            )

        import torch
        from lightglue import LightGlue, SuperPoint, DISK, ALIKED

        device_cfg = self.config.get("device", "auto")
        if device_cfg == "auto":
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self._device = device_cfg

        extractor_name = self.config.get("extractor", "superpoint")
        max_keypoints = self.config.get("max_keypoints", 1024)

        extractor_map = {
            "superpoint": SuperPoint,
            "disk": DISK,
            "aliked": ALIKED,
        }
        extractor_cls = extractor_map.get(extractor_name, SuperPoint)
        self._extractor = extractor_cls(max_num_keypoints=max_keypoints).eval().to(self._device)
        self._matcher = LightGlue(features=extractor_name).eval().to(self._device)

    def _match(
        self,
        image_source: np.ndarray,
        image_target: np.ndarray,
        threshold: float,
        **kwargs: Any,
    ) -> MatchResult:
        if not self.is_available():
            raise MatcherNotAvailableError(
                "LightGlueMatcher 依赖未安装，无法执行深度学习匹配。"
                "请运行: pip install imgloc[deep]"
            )

        self._lazy_init_models()

        import torch
        import cv2

        min_match_count = self.config.get("min_match_count", 6)

        src_tensor = self._image_to_tensor(image_source)
        tgt_tensor = self._image_to_tensor(image_target)

        with torch.no_grad():
            feats_src = self._extractor.extract(src_tensor)
            feats_tgt = self._extractor.extract(tgt_tensor)
            matches_result = self._matcher({"image0": feats_tgt, "image1": feats_src})

        # LightGlue 输出：matches0 是模板(image0)每个关键点在大图(image1)中的匹配索引(-1表示无匹配)
        feats_tgt_c, feats_src_c, matches_result_c = [
            self._rbd(x) for x in (feats_tgt, feats_src, matches_result)
        ]
        kpts_tgt = feats_tgt_c["keypoints"]
        kpts_src = feats_src_c["keypoints"]
        matches = matches_result_c["matches"]  # shape (N, 2): [idx_tgt, idx_src]
        scores = matches_result_c.get("scores", None)

        if matches is None or len(matches) < min_match_count:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "not_enough_matches", "match_count": int(len(matches)) if matches is not None else 0},
            )

        pts_tgt = kpts_tgt[matches[:, 0]].cpu().numpy()
        pts_src = kpts_src[matches[:, 1]].cpu().numpy()
        mean_score = float(scores.mean().item()) if scores is not None and len(scores) > 0 else 0.0

        homography, mask = cv2.findHomography(
            pts_tgt.reshape(-1, 1, 2), pts_src.reshape(-1, 1, 2), cv2.RANSAC, 5.0
        )
        if homography is None:
            return MatchResult.not_found(
                method_used=self.name,
                extra={"reason": "homography_failed", "match_count": int(len(matches))},
            )

        inlier_count = int(mask.sum()) if mask is not None else 0
        confidence = max(mean_score, inlier_count / max(len(matches), 1))

        h, w = image_target.shape[:2]
        corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
        projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)

        xs, ys = projected[:, 0], projected[:, 1]
        x0, y0, x1, y1 = float(np.min(xs)), float(np.min(ys)), float(np.max(xs)), float(np.max(ys))
        rect = (int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0)))
        center_point = (int(round((x0 + x1) / 2)), int(round((y0 + y1) / 2)))

        found = confidence >= threshold
        return MatchResult(
            found=found,
            rect=rect if found else None,
            center_point=center_point if found else None,
            confidence=float(confidence),
            angle=0.0,
            scale=1.0,
            method_used=self.name,
            extra={"match_count": int(len(matches)), "inliers": inlier_count, "device": self._device},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _rbd(data: dict) -> dict:
        """去除 LightGlue 输出中的 batch 维度(remove batch dimension)，取第0个样本。"""
        return {
            k: (v[0] if isinstance(v, (list, tuple)) or (hasattr(v, "shape") and len(v.shape) > 0 and v.shape[0] == 1) else v)
            for k, v in data.items()
        }

    def _image_to_tensor(self, image: np.ndarray):
        """将 BGR numpy 图像转换为 LightGlue 所需的 RGB float tensor [C,H,W]，归一化到[0,1]。"""
        import torch
        import cv2

        bgr = ensure_bgr(image)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        tensor = torch.from_numpy(rgb).float().permute(2, 0, 1) / 255.0
        return tensor.to(self._device)
