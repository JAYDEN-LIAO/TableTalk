"""精确 Token 计数器 —— 基于 tiktoken，替换 char/4 估算。

用于上下文窗口管理、Token 预算追踪、Agent 熔断判断。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Dict, List, Optional

import tiktoken

logger = logging.getLogger(__name__)

# 每条消息的格式开销（role + 分隔符等），参考 OpenAI 官方估算
_MESSAGE_OVERHEAD_TOKENS = 4
# 回复前缀开销
_REPLY_PREFIX_TOKENS = 3


class TokenCounter:
    """基于 tiktoken 的精确 Token 计数器。

    用法::

        counter = TokenCounter()
        count = counter.count("Hello world")
        msg_count = counter.count_messages([
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ])
        remaining = counter.remaining_budget(8000, used=3500)
    """

    # 默认使用 cl100k_base（GPT-4 / GPT-3.5-turbo 编码），
    # 对于其他模型（如 Qwen）作为近似估算足够精确
    DEFAULT_MODEL = "cl100k_base"

    def __init__(self, model: str = DEFAULT_MODEL):
        self._model = model
        self._encoding = self._get_encoding(model)

    @staticmethod
    @lru_cache(maxsize=2)
    def _get_encoding(model: str) -> tiktoken.Encoding:
        try:
            return tiktoken.get_encoding(model)
        except Exception:
            logger.warning(
                "无法加载编码 %s，回退到 cl100k_base", model
            )
            return tiktoken.get_encoding("cl100k_base")

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def count(self, text: str) -> int:
        """计算单段文本的 token 数。"""
        if not text:
            return 0
        try:
            return len(self._encoding.encode(text, disallowed_special=()))
        except Exception:
            # 粗略回退：英文 1 token ≈ 4 chars，中文 1 token ≈ 1.5 chars
            return self._fallback_estimate(text)

    def count_messages(
        self,
        messages: List[Dict[str, str]],
        *,
        include_reply_prefix: bool = False,
    ) -> int:
        """计算一组 OpenAI 格式消息的总 token 数。

        每条消息额外计入 4 token 的格式开销。
        """
        total = 0
        for msg in messages:
            total += _MESSAGE_OVERHEAD_TOKENS
            for key, value in msg.items():
                if isinstance(value, str):
                    total += self.count(value)
                elif isinstance(value, list):
                    # 处理 tool_call 等嵌套结构
                    total += self.count(str(value))
        if include_reply_prefix:
            total += _REPLY_PREFIX_TOKENS
        return total

    def remaining_budget(self, max_tokens: int, used: int) -> int:
        """计算剩余 token 预算。"""
        return max(0, max_tokens - used)

    def is_within_budget(
        self, max_tokens: int, used: int, *, margin: int = 500
    ) -> bool:
        """检查是否在预算内（保留 margin 作为安全余量）。"""
        return (used + margin) <= max_tokens

    def estimate_tokens_for_completion(
        self,
        messages: List[Dict[str, str]],
        *,
        max_output_tokens: int = 2048,
    ) -> int:
        """估算一次 LLM 调用所需的总 token 预算（input + output）。"""
        input_tokens = self.count_messages(messages)
        return input_tokens + max_output_tokens

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_estimate(text: str) -> int:
        """粗略估算 token 数（用于 tiktoken 不可用时）。"""
        chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other_chars = len(text) - chinese_chars
        return max(1, int(chinese_chars * 1.5 + other_chars / 4))


# ------------------------------------------------------------------
# 模块级便捷实例
# ------------------------------------------------------------------

_default_counter: Optional[TokenCounter] = None


def get_token_counter() -> TokenCounter:
    """获取模块级 TokenCounter 单例。"""
    global _default_counter
    if _default_counter is None:
        _default_counter = TokenCounter()
    return _default_counter
