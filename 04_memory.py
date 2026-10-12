"""阶段 4：记忆。

模型本身是无状态的 —— 它不"记得"任何东西，所谓记忆全靠我们在外面存。
每个会话落盘成一个 JSON，下次重新起进程时先读回来，于是它看起来"记得"。

存储的完整实现（多会话、分层摘要、成本统计、会话隔离）都在 memory.py，
阶段 5 的 05_agent.py 用的是同一份。会话默认落在工作目录之外，
所以模型的 read_file 够不着别的会话。

闸门：重启进程后仍记得。
    .venv/bin/python 04_memory.py "我叫小明，记住这个名字"
    .venv/bin/python 04_memory.py "我叫什么名字？"     # 全新进程，仍能答出小明
    .venv/bin/python 04_memory.py --history
    .venv/bin/python 04_memory.py --reset
"""

from __future__ import annotations

import os
import sys

import requests

import memory

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
CHAT_URL = f"{BASE_URL}/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TIMEOUT = 60

SYSTEM = "你是一个有记忆的助手，回答时可以参考之前的历史对话。"


def get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("缺少 DEEPSEEK_API_KEY。先 export（或 source ~/.zshrc）再跑。")
    return key


def chat(messages: list) -> str:
    headers = {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}
    payload = {"model": MODEL, "messages": messages, "stream": False}
    resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=TIMEOUT)
    if resp.status_code != 200:
        sys.exit(f"请求失败：HTTP {resp.status_code}\n{resp.text}")
    return resp.json()["choices"][0]["message"]["content"] or ""


def show_history(session: dict) -> None:
    if not session["messages"] and not session.get("archive"):
        print("（还没有记忆）")
        return
    if session.get("archive"):
        print(f"（归档 {len(session['archive'])} 条，用 05_agent.py --archive 看）")
    for m in session["messages"]:
        icon = {"user": "👤", "assistant": "🤖"}.get(m["role"], m["role"])
        print(f"{icon} {(m.get('content') or '')[:120]}")


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    words = [a for a in args if not a.startswith("--")]

    if "--reset" in flags:
        memory.reset(memory.DEFAULT_SESSION)
        print("记忆已清空。")
        return

    session = memory.load()

    if "--history" in flags:
        show_history(session)
        return
    if "--stats" in flags:
        print(memory.stats_report(session))
        return

    prompt = " ".join(words) or "你好，我们之前聊过什么吗？"
    print(f"（读回 {len(session['messages'])} 条活跃、{len(session['archive'])} 条归档）\n")

    messages = [
        {"role": "system", "content": SYSTEM},
        *memory.context(session),
        *session["messages"],
        {"role": "user", "content": prompt},
    ]
    reply = chat(messages)

    session["messages"].append({"role": "user", "content": prompt})
    session["messages"].append({"role": "assistant", "content": reply})
    memory.save(session)

    print(f"👤 {prompt}\n🤖 {reply}")


if __name__ == "__main__":
    main()
