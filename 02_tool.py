"""阶段 2：工具调用（tool calling）。

一趟往返看清楚三件事：
  1. 我们怎么把工具"介绍"给模型（tools 里的 JSON Schema）
  2. 模型怎么"点单" —— 不直接回答，而是返回 tool_calls（函数名 + JSON 参数字符串）
  3. 我们执行完，怎么用 role=tool + tool_call_id 把结果喂回去

工具的实现在 agent_tools.py（阶段 2/3/5 共用一份），本文件只关心协议怎么走。

跑法：
    .venv/bin/python 02_tool.py "帮我算一下 (17*23+5)/6"
"""

from __future__ import annotations

import json
import os
import sys

import requests

from agent_tools import TOOLS, execute

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
CHAT_URL = f"{BASE_URL}/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TIMEOUT = 60


def get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("缺少 DEEPSEEK_API_KEY。先 export（或 source ~/.zshrc）再跑。")
    return key


def chat(messages: list, tools: list | None = None) -> dict:
    payload: dict = {"model": MODEL, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}
    resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=TIMEOUT)
    if resp.status_code != 200:
        sys.exit(f"请求失败：HTTP {resp.status_code}\n{resp.text}")
    return resp.json()


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "帮我算一下 (17*23+5)/6"
    messages = [{"role": "user", "content": prompt}]

    print(f"👤 {prompt}\n")

    # 第 1 趟：模型看到工具菜单，决定调不调、调哪个
    data = chat(messages, tools=TOOLS)
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []

    if not calls:
        print("（模型没调工具，直接回答）")
        print(msg.get("content"))
        return

    messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})

    for call in calls:
        name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"] or "{}")
        print(f"🔧 模型请求调用：{name}({json.dumps(args, ensure_ascii=False)})")
        result = execute(name, args)
        print(f"📦 工具返回：{result}\n")
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    # 第 2 趟：把工具结果交回去，模型据此给最终答复
    final = chat(messages, tools=TOOLS)["choices"][0]["message"]
    print("🤖 最终回答：")
    print(final.get("content"))


if __name__ == "__main__":
    main()
