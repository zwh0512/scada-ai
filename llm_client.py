"""
极简百炼客户端 —— 不装 openai SDK
=================================
用标准库 `urllib` 直接调阿里百炼的 **OpenAI 兼容接口**。

为什么不装 openai SDK:
  - 这个项目已经在用标准库风格, 再引一个 SDK 多一份依赖
  - 百炼的 OpenAI 兼容接口就两个 HTTP 端点, 自己封装更可控
  - clone 下来 `uv pip install -r requirements.txt` 就能跑, 不用额外装东西

用法:
    from llm_client import BailianClient
    c = BailianClient()
    r = c.chat([{"role": "user", "content": "你好"}])
    print(r["content"])

API Key 从哪来(优先级从高到低):
    1. 环境变量 DASHSCOPE_API_KEY
    2. 本项目的 .env 文件
    3. 同机其它项目常见的 .env(开发便利, 不会进版本库)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"
HERE = Path(__file__).resolve().parent

# 找不到 key 时按顺序找这些地方(只是读同一个人的本机文件, 不外传)
_FALLBACK_ENVS = [
    HERE / ".env",
    Path(r"C:\Users\Administrator\dev\llm-test\.env"),
]


class LLMError(RuntimeError):
    """调用大模型失败。"""


def load_api_key() -> str | None:
    """按优先级找百炼 API Key。找不到返回 None。"""
    env = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if env:
        return env.strip()

    for path in _FALLBACK_ENVS:
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
        except OSError:
            continue
    return None


class BailianClient:
    """百炼 OpenAI 兼容接口的极简封装。"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
    ):
        self.api_key = api_key or load_api_key()
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """调一次 chat/completions。

        返回:
            {"content": str, "tool_calls": [{"id","name","arguments"}], "raw": 原始响应}
        """
        if not self.api_key:
            raise LLMError("没有 API Key, 设环境变量 DASHSCOPE_API_KEY 或在项目里放 .env")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        if max_tokens:
            body["max_tokens"] = max_tokens

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LLMError(f"百炼返回 HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"连不上百炼: {exc.reason}") from exc

        usage = raw.get("usage") or {}
        for k in self.total_usage:
            self.total_usage[k] += usage.get(k, 0) or 0

        message = (raw.get("choices") or [{}])[0].get("message", {})
        tool_calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            tool_calls.append(
                {"id": tc.get("id"), "name": fn.get("name"), "arguments": args}
            )

        return {
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
            "raw": raw,
        }


def to_assistant_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把简化后的 tool_calls 还原成 OpenAI 请求要的形状。

    回传给模型时必须是原始结构:
        {"id": ..., "type": "function", "function": {"name":..., "arguments": "..."}}
    """
    return [
        {
            "id": tc.get("id"),
            "type": "function",
            "function": {
                "name": tc.get("name"),
                "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
            },
        }
        for tc in tool_calls
    ]
