"""
src/locate/intent.py — Step A: 意图解析
========================================
从用户自然语言描述中提取目标信息(目标文字、类型、外观提示)。

优先用正则提取引号内的文字、中文/英文专名;
正则提取不到时,若传入 AIClient 实例则用一次纯文本 LLM 调用兜底;
LLM 失败时退化为 target_type="other"。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .types import Intent


# --------------------------------------------------------------------
# 正则
# --------------------------------------------------------------------
# 引号内的文字(支持中文“”‘’、英文 ""'')
_QUOTED_RE = re.compile(r'["“”‘’\']\s*([^“”‘’""\'\n]+?)\s*["“”‘’\']')
# 拉丁字母/数字段(应用名/英文专名,可含 : . - _):如 kof、KOF:Legend、Appium
_LATIN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_:.\-]*')
# 连续中文段
_CJK_RE = re.compile(r'[\u4e00-\u9fa5]+')

# 关键字
_ICON_KEYWORDS = ("图标", "icon")
_BUTTON_KEYWORDS = ("按钮", "button")

# 输入/填写意图关键词(中英)
_INPUT_KEYWORDS = ("输入", "填写", "键入", "录入", "type", "enter", "input")
_INPUT_INTENT_RE = re.compile(
    r"输入|填写|键入|录入|\btype\b|\benter\b|\binput\b", re.IGNORECASE)
# 句型 A: 在/到/向 <目标> (中/里) 输入 <文本>
_INPUT_AFTER_TARGET_RE = re.compile(
    r"(?:在|到|向)\s*[\"“”‘’']?\s*(?P<target>.+?)\s*[\"“”‘’']?\s*(?:中|里|内)?\s*"
    r"(?:输入|填写|键入|录入)\s*[\"“”‘’']?\s*(?P<text>.+?)\s*[\"“”‘’']?\s*$")
# 句型 B: 输入 <文本> 到/在/向 <目标>
_INPUT_BEFORE_TARGET_RE = re.compile(
    r"(?:输入|填写|键入|录入)\s*[\"“”‘’']?\s*(?P<text>.+?)\s*[\"“”‘’']?\s*"
    r"(?:到|在|向)\s*[\"“”‘’']?\s*(?P<target>.+?)\s*[\"“”‘’']?\s*$")


def _parse_input_action(query: str):
    """
    检测"输入文字"意图并拆出 (目标框描述, 待输入文本)。

    Returns:
        (target_text, input_text) 或 None(非输入意图/拆不出目标)。
    """
    if not query or not _INPUT_INTENT_RE.search(query):
        return None
    for pattern in (_INPUT_AFTER_TARGET_RE, _INPUT_BEFORE_TARGET_RE):
        m = pattern.search(query)
        if not m:
            continue
        target = m.group("target").strip().strip("\"“”‘’' ")
        text = m.group("text").strip().strip("\"“”‘’' ")
        # 目标需含中文或字母(避免把纯标点当目标);文本非空
        if target and text and re.search(r"[A-Za-z0-9\u4e00-\u9fa5]", target):
            return target, text
    return None

# 应被过滤的非目标词(动词、方位、颜色、UI 类目、虚词等)
_STOP_WORDS = {
    # 方位
    "左上", "左下", "右上", "右下", "左面", "右面", "上面", "下面",
    "左上角", "左下角", "右上角", "右下角", "中间", "顶部", "底部", "中部",
    "上", "下", "左", "右", "中",
    # 颜色
    "红色", "蓝色", "绿色", "黄色", "黑色", "白色", "橙色", "紫色",
    "粉色", "灰色", "青色", "棕色",
    # 动词/虚词
    "点击", "点一下", "请点击", "找", "找到", "按", "敲", "选择", "进入",
    "打开", "关闭", "触发", "一下", "那个", "这个", "的", "位于", "处于", "在",
    "帮我", "给我", "我要", "想要",
    "输入", "填写", "键入", "录入",
    # UI 类目词(不是专名)
    "按钮", "图标", "菜单", "菜单项", "项", "链接", "图片", "图像",
    "标签", "选项", "选项卡", "栏", "条",
    # 英文常见词
    "click", "tap", "press", "the", "a", "an", "of", "on", "at",
    "icon", "button", "menu", "item", "link", "image", "label",
    "type", "enter", "input",
    "top", "bottom", "left", "right", "corner", "red", "blue",
    "green", "yellow", "black", "white",
}


# --------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------
def _heuristic_type(query: str, target_text: str) -> str:
    """启发式判断 target_type。"""
    q = query.lower()
    has_icon_kw = any(k in q for k in _ICON_KEYWORDS)
    has_button_kw = any(k in q for k in _BUTTON_KEYWORDS)

    if has_icon_kw:
        # 有目标文字 → 带标签的图标;否则纯图标
        return "icon_with_label" if target_text else "icon"
    if has_button_kw:
        return "text"
    if target_text:
        return "text"
    return "other"


def _extract_visual_hint(query: str, target_text: str) -> str:
    """从 query 中剥离目标文字(含可能包裹的引号),余下作为外观提示。"""
    if not target_text:
        return (query or "").strip()
    # 连同紧邻的引号和空白一起移除,避免留下空引号
    pattern = r'\s*["“”‘’\']?\s*' + re.escape(target_text) + r'\s*["“”‘’\']?\s*'
    hint = re.sub(pattern, ' ', query or '')
    hint = re.sub(r'\s+', ' ', hint).strip()
    return hint


def _strip_cjk_stopwords(seg: str) -> str:
    """从中文段两端剥离开停用词(点击/按钮/图标等),返回剩余主体。"""
    core, _, _ = _strip_cjk_affixes(seg)
    return core


# 仅含中文的停用词,按长度降序(长词优先剥离,避免"右上角"被"右"先吃掉)
_CJK_STOP_SORTED = sorted(
    (sw for sw in _STOP_WORDS if re.fullmatch(r'[\u4e00-\u9fa5]+', sw)),
    key=len, reverse=True,
)
# 拉丁段与后续中文段之间允许的间隔
_GAP_RE = re.compile(r'[\s:：.\-_\'"“”‘’「」()（）]*')


def _strip_cjk_affixes(seg: str) -> tuple:
    """
    剥离开中文段两端的停用词,返回 (主体, 剥掉的前缀长度, 剥掉的后缀长度)。
    每轮取最长匹配,防止单字方位词把多字词切碎。
    """
    start, end = 0, len(seg)
    changed = True
    while changed:
        changed = False
        for sw in _CJK_STOP_SORTED:
            if start < end and seg.startswith(sw, start, end):
                start += len(sw)
                changed = True
                break
        for sw in _CJK_STOP_SORTED:
            if start < end and seg.endswith(sw, start, end):
                end -= len(sw)
                changed = True
                break
    return seg[start:end], start, len(seg) - end


def _regex_extract(query: str) -> str:
    """
    提取目标文字:引号内容优先;否则把中英混合串按文字体系切开,
    去掉停用词;拉丁段(应用名/英文专名)优先,并与紧邻的中文主体合并
    (如 "Play商店"),其次取最长中文主体。
    """
    # 1) 引号内文字
    for m in _QUOTED_RE.finditer(query):
        text = m.group(1).strip()
        if text:
            return text

    # 2) 拉丁/数字段(含至少一个字母,去英文停用词),记录在原文中的跨度
    latin_hits = []
    for m in _LATIN_RE.finditer(query):
        tok = m.group()
        low = tok.lower().strip(":.-_")
        if not low or low in _STOP_WORDS or not re.search(r'[A-Za-z]', tok):
            continue
        latin_hits.append((tok, m.start(), m.end()))

    # 3) 中文段:剥离开停用词,记录主体与它在原文中的跨度
    cjk_cores = []  # (主体, 主体在 query 中的起始, 结束)
    for m in _CJK_RE.finditer(query):
        seg = m.group()
        core, pre, suf = _strip_cjk_affixes(seg)
        if core and core not in _STOP_WORDS:
            cjk_cores.append((core, m.start() + pre, m.end() - suf))

    # 4) 拉丁段优先;若其后紧邻(仅隔空白/标点)中文主体,合并为混合名
    candidates = []
    for tok, s, e in latin_hits:
        merged = tok
        end = e
        for core, cs, ce in cjk_cores:
            if cs >= end and _GAP_RE.fullmatch(query[end:cs] or ""):
                merged += core
                end = ce
        candidates.append(merged)

    # 中文主体也作为候选
    candidates.extend(core for core, _, _ in cjk_cores)

    # 5) 含拉丁字母的候选优先(应用图标名通常带英文),取最长;否则取最长中文
    latin_based = [c for c in candidates if re.search(r'[A-Za-z]', c)]
    if latin_based:
        return max(latin_based, key=len)
    if candidates:
        return max(candidates, key=len)
    return ""


# --------------------------------------------------------------------
# LLM 兜底
# --------------------------------------------------------------------
_LLM_SYSTEM = (
    "你是 Android UI 自动化的意图解析器。从用户输入中提取要操作的 UI 元素信息。"
    "只输出严格 JSON,不要任何解释或代码块标记。格式:"
    '{"action":"tap|input","target_text":"提取的目标文字","input_text":"要输入的文本",'
    '"target_type":"icon_with_label|text|icon|other","visual_hint":"外观提示简述"}'
    "。action:用户要点击元素用 tap;要在输入框/搜索框等输入文字用 input"
    "(此时 target_text 填输入框的描述文字,input_text 填要输入的内容,无输入内容时留空)。"
    "target_type 取值:带文字标签的图标用 icon_with_label;"
    "纯文字(按钮/链接/菜单项)用 text;"
    "无文字的纯图标用 icon;无法判断用 other。"
    "若用户未指定具体文字,target_text 留空字符串。"
)


def _parse_via_llm(query: str, ai_client) -> Optional[Intent]:
    """
    用一次纯文本 LLM 调用解析意图。
    返回 Intent 表示成功;返回 None 表示解析失败(由调用方退化为 other)。
    """
    prompt = f"用户输入:{query}\n请输出 JSON。"
    resp = ai_client.chat(prompt, system=_LLM_SYSTEM)
    if not resp or not isinstance(resp, str):
        return None

    # 容忍 ```json 代码块包裹,提取首个 {...}
    m = re.search(r'\{[^{}]*\}', resp.strip(), re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    target_text = str(data.get("target_text", "") or "").strip()
    target_type = str(data.get("target_type", "other") or "").strip().lower()
    if target_type not in ("icon_with_label", "text", "icon", "other"):
        target_type = "other"
    visual_hint = str(data.get("visual_hint", "") or "").strip()
    action = str(data.get("action", "tap") or "").strip().lower()
    if action not in ("tap", "input"):
        action = "tap"
    input_text = str(data.get("input_text", "") or "").strip()

    return Intent(
        target_text=target_text,
        target_type=target_type,
        visual_hint=visual_hint,
        raw_query=query,
        action=action,
        input_text=input_text,
    )


# --------------------------------------------------------------------
# 公开接口
# --------------------------------------------------------------------
def parse_intent(query: str, ai_client=None) -> Intent:
    """
    从用户自然语言描述中解析定位意图。

    Args:
        query: 用户原始描述(如 "点击左上角的 KOF:Legend 图标"、
               "在搜索框输入 hello")。
        ai_client: 可选 AIClient 实例。正则提取不到目标文字时,
                   用一次纯文本 LLM 调用兜底解析。

    Returns:
        Intent(target_text, target_type, visual_hint, raw_query, action, input_text)
    """
    raw = (query or "").strip()

    # 输入意图优先:正则直接拆出 目标框 + 待输入文本
    # (若走通用提取,"输入hello"里的 hello 会被误当成定位目标)
    input_hit = _parse_input_action(raw)
    if input_hit is not None:
        target_text, input_text = input_hit
        return Intent(
            target_text=target_text,
            target_type=_heuristic_type(raw, target_text),
            visual_hint=_extract_visual_hint(raw, target_text),
            raw_query=raw,
            action="input",
            input_text=input_text,
        )

    target_text = _regex_extract(raw)
    target_type = _heuristic_type(raw, target_text)
    visual_hint = _extract_visual_hint(raw, target_text)

    # 正则提取不到 → 走 LLM 兜底
    if not target_text and ai_client is not None:
        try:
            llm_intent = _parse_via_llm(raw, ai_client)
            if llm_intent is not None:
                # 信任 LLM 的解析(含其给出的 target_type/action/input_text)
                return llm_intent
            # LLM 返回 None(解析失败)→ 退化为 other
            target_type = "other"
        except Exception:
            # 任何异常退化为 other
            target_type = "other"
    elif not target_text:
        # 没文字、也没传 ai_client
        target_type = "other"

    return Intent(
        target_text=target_text,
        target_type=target_type,
        visual_hint=visual_hint,
        raw_query=raw,
    )
