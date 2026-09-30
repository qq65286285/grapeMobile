"""
benchmark/run_benchmark.py - 性能与准确率基准测试主脚本
==========================================================
对比模板匹配/形状匹配/ORB/SIFT/(可选)LightGlue 各算法层，在不同分辨率、
旋转角度、遮挡程度、光照/主题变化、噪声干扰场景下的：
    - 平均耗时 (ms)
    - 命中率 (found=True 的比例)
    - 坐标准确率 (中心点与预期位置的欧氏距离 <= 容差像素 的比例)

用法：
    python benchmark/run_benchmark.py
    python benchmark/run_benchmark.py --output benchmark/results/report.csv
    python benchmark/run_benchmark.py --repeat 3   # 每个场景重复跑3次取平均耗时

输出：
    - 终端打印汇总表格（按算法 x 场景分类）
    - 可选导出 CSV 详细报表（每条用例每个算法的具体结果）
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List

# 确保可以直接 import src/imgloc（未 pip install -e 时也能跑）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from imgloc.matchers.template import TemplateMatcher
from imgloc.matchers.shape import ShapeMatcher
from imgloc.matchers.orb import OrbMatcher
from imgloc.matchers.sift import SiftMatcher
from imgloc.matchers.lightglue_matcher import LightGlueMatcher

from scenarios import generate_all_cases, BenchmarkCase

# 各算法层的实例化配置（可根据需要调整，与 imgloc.config.DEFAULT_CONFIG 保持一致风格）
MATCHERS = {
    "template": TemplateMatcher(config={"scale_range": [0.5, 2.2], "scale_step": 0.05, "threshold": 0.6}),
    "shape": ShapeMatcher(config={"scale_range": [0.7, 1.5], "scale_step": 0.1, "threshold": 0.5}),
    "orb": OrbMatcher(config={"threshold": 0.2, "min_match_count": 6}),
    "sift": SiftMatcher(config={"threshold": 0.2, "min_match_count": 6}),
    "lightglue": LightGlueMatcher(config={"threshold": 0.2}),
}

COORD_TOLERANCE_PX = 15  # 中心点坐标容差（像素），小于该值视为"定位准确"


def _distance(p1, p2) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def run_single_case(matcher_name: str, matcher, case: BenchmarkCase, repeat: int = 1) -> Dict[str, Any]:
    """对单个场景用单个算法跑 repeat 次，返回平均耗时与本次匹配结果。"""
    if not matcher.is_available():
        return {
            "matcher": matcher_name, "case": case.name, "category": case.category,
            "available": False, "found": False, "confidence": 0.0,
            "elapsed_ms": None, "coord_correct": None,
        }

    elapsed_list = []
    last_result = None
    for _ in range(repeat):
        result = matcher.match(case.scene, case.template, threshold=matcher.config.get("threshold", 0.5))
        elapsed_list.append(result.elapsed_ms)
        last_result = result

    avg_elapsed = sum(elapsed_list) / len(elapsed_list)

    coord_correct = None
    if last_result.found and case.expected_center is not None and last_result.center_point is not None:
        coord_correct = _distance(last_result.center_point, case.expected_center) <= COORD_TOLERANCE_PX

    return {
        "matcher": matcher_name,
        "case": case.name,
        "category": case.category,
        "available": True,
        "found": last_result.found,
        "confidence": round(last_result.confidence, 4),
        "elapsed_ms": round(avg_elapsed, 3),
        "coord_correct": coord_correct,
    }


def run_all(repeat: int = 1) -> List[Dict[str, Any]]:
    cases = generate_all_cases()
    rows: List[Dict[str, Any]] = []
    total = len(cases) * len(MATCHERS)
    done = 0
    for case in cases:
        for matcher_name, matcher in MATCHERS.items():
            row = run_single_case(matcher_name, matcher, case, repeat=repeat)
            rows.append(row)
            done += 1
            print(f"\r[{done}/{total}] 正在测试: {matcher_name:10s} x {case.name:22s}", end="", flush=True)
    print()  # 换行
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按 matcher 汇总：平均耗时、命中率、坐标准确率。"""
    summary: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["matcher"]].append(row)

    for matcher_name, matcher_rows in grouped.items():
        available_rows = [r for r in matcher_rows if r["available"]]
        if not available_rows:
            summary[matcher_name] = {"status": "unavailable(依赖未安装)"}
            continue
        found_rows = [r for r in available_rows if r["found"]]
        coord_checked = [r for r in found_rows if r["coord_correct"] is not None]
        coord_correct_count = sum(1 for r in coord_checked if r["coord_correct"])

        summary[matcher_name] = {
            "status": "available",
            "total_cases": len(available_rows),
            "found_count": len(found_rows),
            "hit_rate": round(len(found_rows) / len(available_rows), 3) if available_rows else 0.0,
            "coord_accuracy": round(coord_correct_count / len(coord_checked), 3) if coord_checked else None,
            "avg_elapsed_ms": round(sum(r["elapsed_ms"] for r in available_rows) / len(available_rows), 3),
        }
    return summary


def print_summary_table(summary: Dict[str, Dict[str, Any]]) -> None:
    print("\n" + "=" * 78)
    print("算法层综合对比报表")
    print("=" * 78)
    header = f"{'算法':<12}{'状态':<24}{'命中率':<10}{'坐标准确率':<12}{'平均耗时(ms)':<14}"
    print(header)
    print("-" * 78)
    for name, s in summary.items():
        if s.get("status") == "unavailable(依赖未安装)":
            print(f"{name:<12}{'依赖未安装(跳过)':<24}{'--':<10}{'--':<12}{'--':<14}")
            continue
        hit_rate = f"{s['hit_rate']*100:.1f}%"
        coord_acc = f"{s['coord_accuracy']*100:.1f}%" if s["coord_accuracy"] is not None else "--"
        elapsed = f"{s['avg_elapsed_ms']:.2f}"
        print(f"{name:<12}{'可用':<24}{hit_rate:<10}{coord_acc:<12}{elapsed:<14}")
    print("=" * 78)


def print_category_breakdown(rows: List[Dict[str, Any]]) -> None:
    """按场景分类(scale/rotation/occlusion/lighting/noise)打印各算法命中率细分。"""
    categories = sorted(set(r["category"] for r in rows))
    matchers = list(MATCHERS.keys())

    print("\n分场景类别命中率细分：")
    for category in categories:
        print(f"\n  [{category}]")
        for matcher_name in matchers:
            sub_rows = [r for r in rows if r["category"] == category and r["matcher"] == matcher_name and r["available"]]
            if not sub_rows:
                print(f"    {matcher_name:<12}: 不可用/无数据")
                continue
            found_count = sum(1 for r in sub_rows if r["found"])
            avg_time = sum(r["elapsed_ms"] for r in sub_rows) / len(sub_rows)
            print(f"    {matcher_name:<12}: 命中 {found_count}/{len(sub_rows)}  平均耗时 {avg_time:.2f}ms")


def export_csv(rows: List[Dict[str, Any]], output_path: str) -> None:
    import csv
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fieldnames = ["matcher", "case", "category", "available", "found", "confidence", "elapsed_ms", "coord_correct"]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n详细报表已导出至: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="imgloc 多算法层性能与准确率基准测试")
    parser.add_argument("--repeat", type=int, default=1, help="每个场景重复运行次数，取平均耗时（默认1）")
    parser.add_argument("--output", type=str, default=None, help="导出详细CSV报表的路径（可选）")
    args = parser.parse_args()

    print(f"开始基准测试（每场景重复 {args.repeat} 次）...\n")
    start = time.time()
    rows = run_all(repeat=args.repeat)
    elapsed_total = time.time() - start

    summary = summarize(rows)
    print_summary_table(summary)
    print_category_breakdown(rows)

    print(f"\n总耗时: {elapsed_total:.2f}s，共 {len(rows)} 条测试记录")

    if args.output:
        export_csv(rows, args.output)


if __name__ == "__main__":
    main()
