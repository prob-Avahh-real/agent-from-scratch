"""阶段 1：裸调 LLM。

不借助任何 SDK / 框架，直接用 HTTP POST 打 DeepSeek 的 chat/completions，
把"一次对话"拆到最底层看清楚：请求长什么样、响应长什么样。

跑法：
    export DEEPSEEK_API_KEY=sk-...        # 已写在 ~/.zshrc；新开 shell 会自动有
    .venv/bin/python 01_chat.py "用一句话介绍你自己"
    .venv/bin/python 01_chat.py "你好" --raw   # 顺便看完整响应 JSON
"""

from __future__ import annotations

import json
import os
import sys

import requests

# 默认指向 DeepSeek 官方；换兼容 OpenAI 协议的服务时改这两个环境变量即可。
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
CHAT_URL = f"{BASE_URL}/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

TIMEOUT = 60


def get_api_key() -> str:
    """从环境变量取 key。取不到就明确报错，别让它变成一个 KeyError 堆栈。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit(
            "缺少 DEEPSEEK_API_KEY。\n"
            "  确认 shell 里 export 过（或 source ~/.zshrc），再跑本脚本。"
        )
    return key


def chat(prompt: str, system: str | None = None) -> dict:
    """发一次对话请求，返回解析后的响应 JSON。"""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }

    resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=TIMEOUT)

    # 先把非 200 拦下来，把服务端的话原样带出来，方便定位 401 / 余额 / 模型名。
    if resp.status_code != 200:
        sys.exit(f"请求失败：HTTP {resp.status_code}\n{resp.text}")

    return resp.json()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--raw"]
    raw = "--raw" in sys.argv[1:]

    prompt = " ".join(args) if args else "用一句话介绍你自己。"

    data = chat(prompt)

    if raw:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print("-" * 40)

    reply = data["choices"][0]["message"]["content"]
    print(reply)

    usage = data.get("usage") or {}
    if usage:
        print(
            f"\n[tokens] prompt={usage.get('prompt_tokens')} "
            f"completion={usage.get('completion_tokens')} "
            f"total={usage.get('total_tokens')}"
        )


if __name__ == "__main__":
    main()
