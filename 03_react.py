"""阶段 3：ReAct 循环。

ReAct = Reasoning + Acting：让模型"想一步 → 调工具拿观察 → 再想一步"，
循环到它不再请求工具为止。阶段 2 只走了一趟往返，这里把它套进 while。

模型本身是无状态的，所以每一轮都要把【完整历史】重新发过去：
    user → assistant(tool_calls) → tool(结果) → assistant(tool_calls) → tool → assistant(最终答案)

工具实现见 agent_tools.py。

闸门：多步后正确终止（不再要求调工具，给出自然语言答案）。

跑法：
    .venv/bin/python 03_react.py
    .venv/bin/python 03_react.py "算一下 (1234*5678)**2 的末三位"
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

MAX_STEPS = 8

SYSTEM = (
    "你是一个会用工具解决问题的助手。"
    "需要读文件、列目录或做算术时，必须调用对应工具，不要凭空猜测。"
    "拿到足够信息后，用中文简洁地给出最终答案。"
)


def run(question: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    print(f"👤 {question}\n")

    for step in range(1, MAX_STEPS + 1):
        data = chat(messages, tools=TOOLS)
        choice = data["choices"][0]
        msg = choice["message"]
        calls = msg.get("tool_calls") or []
        think = (msg.get("content") or "").strip()

        # 干净地回填 assistant 这一轮：只保留协议需要的字段
        assistant_msg = {"role": "assistant", "content": msg.get("content")}
        if calls:
            assistant_msg["tool_calls"] = calls
        messages.append(assistant_msg)

        if think:
            print(f"[{step}] 🧠 想：{think}")

        if not calls:
            print(f"[{step}] ✅ 终止（finish_reason={choice.get('finish_reason')}）\n")
            return think

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"[{step}] 🔧 做：{name}({json.dumps(args, ensure_ascii=False)})")
            result = execute(name, args)
            preview = result if len(result) <= 160 else result[:160] + " …"
            print(f"[{step}] 📦 观察：{preview}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    print(f"⚠️ 达到最大步数 {MAX_STEPS} 仍未终止，强制收尾。")
    return ""


if __name__ == "__main__":
    default_q = (
        "先列出当前目录有哪些文件，再读取 01_chat.py 的前 15 行，"
        "用一句话说明它是干什么的；最后用计算器算 (17*23+5)/6。"
    )
    run(" ".join(sys.argv[1:]) or default_q)
