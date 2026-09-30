"""
runner/test_ai.py - AI 接口连通性测试
======================================
用法:
    python runner/test_ai.py            # 纯文本测试
    python runner/test_ai.py tmp/image/screenshot.png # 图片理解测试(传入图片路径)
"""

import os
import sys

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

from aiclient import AIClient, AIError


def main():
    ai = AIClient()
    img_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if img_arg:
        if not os.path.isfile(img_arg):
            print(f"[FAIL] 图片不存在: {img_arg}")
            return
        print(f"[发送] 图片 {img_arg} + 提问: 请描述这张截图中的内容")
        try:
            reply = ai.chat_with_image("请描述这张截图中的内容", img_arg)
        except AIError as exc:
            print(f"[FAIL] {exc}")
            return
        print(f"[回复] {reply}")
    else:
        print("[发送] 纯文本: 你好,请用一句话介绍你自己")
        try:
            reply = ai.chat("你好,请用一句话介绍你自己")
        except AIError as exc:
            print(f"[FAIL] {exc}")
            return
        print(f"[回复] {reply}")

    print("[OK] AI 接口连通正常")


if __name__ == "__main__":
    main()
