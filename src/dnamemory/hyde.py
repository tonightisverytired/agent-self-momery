# -*- coding: utf-8 -*-
"""HyDE 查询改写（0.8.1 二轮，攻语义鸿沟）。

把「问题」改写成「假设性答案句」参与检索：问题与文档的表达不对称
（问句 vs 陈述句）是词面/稠密路对推理类问答失效的主因（PerLTQA 实测
bge-m3 余弦 0.40-0.44，低于阈值）。LLM 不可用/失败一律回退空列表，
原查询照常检索，绝不阻断。
"""
from __future__ import annotations

import os
import time
from typing import Protocol

from .extract import find_deepseek_key


class QueryRewriter(Protocol):
    """调用方注入的查询改写回调：返回 0..N 个改写变体（不含原文）。"""

    def rewrite(self, text: str) -> list[str]: ...


_HYDE_PROMPT = """你是记忆检索的查询改写器。把用户的问题改写成两行检索变体：
第一行：一句假设性的答案陈述句（直接写出答案内容，不要解释）；
第二行：问题中的关键实体与主题词（空格分隔）。
只输出这两行，不要输出任何其它内容。

问题：%s"""


class DeepSeekHyDERewriter:
    """DeepSeek 参考实现（OpenAI 兼容，带结果缓存与失败回退）。"""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str = "https://api.deepseek.com", timeout: int = 60,
                 max_retries: int = 1, max_tokens: int = 300,
                 thinking: str = "disabled", max_variants: int = 2):
        self.model = model or os.environ.get("DEEPSEEK_MODEL") \
            or "deepseek-chat"
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.max_variants = max_variants
        self._cache: dict[str, list[str]] = {}
        self._requests = None
        self._base_url = base_url.rstrip("/")
        if not api_key:
            env_name, api_key = find_deepseek_key()
            if not api_key:
                raise ValueError("未找到 DEEPSEEK_API_KEY 环境变量中的 API key")
        self._api_key = api_key

    def _http(self):
        if self._requests is None:
            import requests
            self._requests = requests
        return self._requests

    def rewrite(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        if text in self._cache:
            return self._cache[text]
        out: list[str] = []
        payload = {
            "model": self.model,
            "messages": [{"role": "user",
                          "content": _HYDE_PROMPT % text}],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
        }
        if self.model.startswith("deepseek-v4"):
            payload["thinking"] = {"type": self.thinking}
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http().post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "Content-Type": "application/json"},
                    json=payload, timeout=self.timeout)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"] or ""
                for line in content.splitlines():
                    line = line.strip().lstrip("0123456789.、：: ").strip()
                    if line and line != text:
                        out.append(line)
                break
            except Exception as e:  # noqa: BLE001 LLM 失败回退空列表
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))
        out = out[:self.max_variants]
        self._cache[text] = out
        return out
