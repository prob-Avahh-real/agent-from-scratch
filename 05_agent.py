"""阶段 5：打磨 —— 一个能真的拿去用的 agent。

把前面所有东西拼起来，再补上"能真的拿去用"需要的部分：

  * 工具调用（阶段 2）   见 agent_tools.py：calculator / list_dir / read_file / write_file
  * ReAct 循环（阶段 3）  想一步 → 调工具拿观察 → 再想，直到不再请调工具
  * 记忆落盘（阶段 4）    见 memory.py：退出重进还在
  * 多会话（本阶段）      /save /load /sessions，各会话独立落盘
  * 历史压缩（本阶段）    超阈值时把旧对话压成摘要，上下文不再无限膨胀
  * 流式输出（本阶段）    边生成边打印，不等整段返回
  * 错误重试（本阶段）    429 / 5xx / 网络抖动，指数退避自动重试
  * 交互 REPL（本阶段）   不给 prompt 就进对话模式

流式下最容易翻车的是 tool_calls：它按 index 分片到达，
同一个 index 的 function.arguments 会一段一段发过来，必须累积拼好再 json.loads。

用法：
    .venv/bin/python 05_agent.py "看看目录里有什么"
    .venv/bin/python 05_agent.py                       # 进 REPL（含会话命令）
    .venv/bin/python 05_agent.py --session work "..."   # 指定会话
    .venv/bin/python 05_agent.py --list-sessions
    .venv/bin/python 05_agent.py --history / --summary
    .venv/bin/python 05_agent.py --reset [--session work]
    .venv/bin/python 05_agent.py --reset-all
    .venv/bin/python 05_agent.py --no-stream "..."
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

import memory
from agent_tools import TOOLS, execute

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
CHAT_URL = f"{BASE_URL}/chat/completions"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
TIMEOUT = 120

MAX_STEPS = 10
MAX_RETRIES = 4
BASE_DELAY = 1.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

SYSTEM = (
    "你是一个会用工具解决问题的助手。"
    "需要读文件、列目录、写文件或做算术时，必须调用对应工具，不要凭空猜测。"
    "拿到足够信息后，用中文简洁地给出最终答案。"
)

HELP = (
    "命令：/sessions 列出会话 · /save [名字] 保存（给名字=另存并切换）· "
    "/load <名字> 切换 · /new [名字] 开新会话 · /reset 清空当前 · "
    "/summary 看摘要 · /history 看历史 · exit 退出"
)


class ApiError(RuntimeError):
    pass


def get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("缺少 DEEPSEEK_API_KEY。先 export（或 source ~/.zshrc）再跑。")
    return key


# ---------------- HTTP：重试 + 流式 ----------------

def _headers() -> dict:
    return {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}


def post_with_retry(payload: dict, stream: bool):
    """POST 一个请求，对网络错误和 429/5xx 做指数退避重试。"""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                CHAT_URL, headers=_headers(), json=payload, timeout=TIMEOUT, stream=stream
            )
        except requests.exceptions.RequestException as exc:
            last_err = exc
        else:
            if resp.status_code == 200:
                return resp
            body = resp.text[:300]
            resp.close()
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                last_err = ApiError(f"HTTP {resp.status_code}: {body}")
            else:
                raise ApiError(f"HTTP {resp.status_code}: {body}")

        delay = BASE_DELAY * (2 ** (attempt - 1))
        print(f"  ↻ 第 {attempt} 次失败（{last_err}），{delay:.0f}s 后重试…", file=sys.stderr)
        time.sleep(delay)

    raise ApiError(f"重试 {MAX_RETRIES} 次仍失败：{last_err}")


def one_call(messages: list, tools: list, stream: bool) -> tuple[dict, str | None, dict | None]:
    """发一次请求，返回 (assistant 消息, finish_reason, usage)。

    stream=True 时边收边把正文打到 stdout；tool_calls 按 index 分片累积。
    """
    payload: dict = {"model": MODEL, "messages": messages, "stream": stream}
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream_options"] = {"include_usage": True}

    resp = post_with_retry(payload, stream)

    if not stream:
        data = resp.json()
        choice = data["choices"][0]
        return choice["message"], choice.get("finish_reason"), data.get("usage")

    content_parts: list[str] = []
    slots: dict[int, dict] = {}
    finish: str | None = None
    usage: dict | None = None

    with resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                    print(delta["content"], end="", flush=True)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = slots.setdefault(
                        idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

    text = "".join(content_parts)
    message: dict = {"role": "assistant", "content": text or None}
    if slots:
        message["tool_calls"] = [slots[k] for k in sorted(slots)]
    return message, finish, usage


# ---------------- 历史压缩 ----------------

def summarize(previous_summary: str, transcript: str) -> str:
    """把较老的对话压成一段摘要（非流式，不往 stdout 打正文）。"""
    msg, _, _ = one_call(
        [
            {"role": "system", "content": "你是对话摘要器，只输出摘要正文。"},
            {"role": "user", "content": memory.build_prompt(transcript, previous_summary)},
        ],
        [],
        stream=False,
    )
    return (msg.get("content") or "").strip()


# ---------------- Agent 主循环 ----------------

def run_turn(session: dict, prompt: str, stream: bool) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        *memory.context(session),
        *session["messages"],
        {"role": "user", "content": prompt},
    ]
    final = ""
    total_tokens = 0
    tag = ""

    for step in range(1, MAX_STEPS + 1):
        msg, finish, usage = one_call(messages, TOOLS, stream)
        if usage:
            total_tokens += usage.get("total_tokens") or 0
        messages.append(msg)
        calls = msg.get("tool_calls") or []

        if not calls:
            final = (msg.get("content") or "").strip()
            if stream:
                print()
            else:
                print(final)
            tag = f"step {step}, finish={finish}"
            break

        if msg.get("content"):
            if stream:
                print()  # 流式正文后换行，工具日志另起
            else:
                print(f"      💬 {msg['content'].strip()}")
        for call in calls:
            name = call["function"]["name"]
            raw = call["function"]["arguments"] or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                obs = f"ERROR: 参数不是合法 JSON：{raw[:200]}"
            else:
                print(f"      🔧 {name}({json.dumps(args, ensure_ascii=False)})")
                obs = execute(name, args)
            preview = obs if len(obs) <= 160 else obs[:160] + " …"
            print(f"      📦 {preview}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": obs})
    else:
        final = "（达到最大步数，强制停止）"
        tag = f"step {MAX_STEPS}, 未终止"
        print(f"⚠️ {final}")

    if total_tokens:
        print(f"      [tokens] {total_tokens}  ({tag})")

    # 记忆只留 user/assistant 文本：不含 tool 调用，历史干净、不会出现悬空 tool_call_id
    session["messages"].append({"role": "user", "content": prompt})
    session["messages"].append({"role": "assistant", "content": final})

    if memory.trim(session, summarize):
        print(
            f"      🗜️ 历史超过 {memory.MAX_MESSAGES} 条，已压缩为摘要"
            f"（保留最近 {memory.KEEP_RECENT} 条）"
        )
    memory.save(session)
    return final


# ---------------- 会话命令 ----------------

def show_history(session: dict) -> None:
    msgs = session.get("messages", [])
    if not msgs:
        print("（当前会话还没有历史）")
        return
    for m in msgs:
        icon = {"user": "👤", "assistant": "🤖"}.get(m["role"], m["role"])
        print(f"{icon} {(m.get('content') or '')[:120]}")


def handle_command(session: dict, line: str, stream: bool) -> dict:
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in {"/help", "/?"}:
        print(HELP)
    elif cmd == "/sessions":
        names = memory.list_sessions()
        print("会话：" + ("、".join(names) if names else "（还没有）"))
    elif cmd == "/save":
        if arg:
            session["name"] = memory.safe_name(arg)
        memory.save(session)
        print(f"（已保存到会话 '{session['name']}'）")
    elif cmd == "/load":
        if not arg:
            print("用法：/load <名字>")
        else:
            session = memory.load(arg)
            print(f"（已切到会话 '{session['name']}'：{len(session['messages'])} 条消息）")
    elif cmd == "/new":
        session = memory.new_session(arg or memory.DEFAULT_SESSION)
        memory.save(session)
        print(f"（新会话 '{session['name']}'）")
    elif cmd == "/reset":
        memory.reset(session["name"])
        session["summary"] = ""
        session["messages"] = []
        print("（当前会话已清空）")
    elif cmd == "/summary":
        print(session.get("summary") or "（还没有摘要）")
    elif cmd == "/history":
        show_history(session)
    else:
        print(f"未知命令：{cmd}（/help 看帮助）")
    return session


def repl(session: dict, stream: bool) -> None:
    print(
        f"（会话 '{session['name']}'：{len(session['messages'])} 条消息"
        + ("，含摘要" if session.get("summary") else "")
        + "）"
    )
    print(HELP)
    while True:
        try:
            line = input("\n👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"exit", "quit", ":q"}:
            break
        if line.startswith("/"):
            session = handle_command(session, line, stream)
            continue
        try:
            run_turn(session, line, stream)
        except ApiError as exc:
            print(f"❌ {exc}", file=sys.stderr)
    print("再见。")


# ---------------- CLI ----------------

def parse_args(argv: list) -> dict:
    opts = {"stream": True, "session": memory.DEFAULT_SESSION, "flags": set(), "prompt": ""}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--session":
            i += 1
            if i >= len(argv):
                sys.exit("--session 需要一个会话名")
            opts["session"] = argv[i]
        elif a == "--no-stream":
            opts["stream"] = False
        elif a.startswith("--"):
            opts["flags"].add(a)
        else:
            rest.append(a)
        i += 1
    opts["prompt"] = " ".join(rest)
    return opts


def main() -> None:
    opts = parse_args(sys.argv[1:])
    flags = opts["flags"]

    if "--list-sessions" in flags:
        names = memory.list_sessions()
        print("会话：" + ("、".join(names) if names else "（还没有）"))
        return
    if "--reset-all" in flags:
        print(f"已清空 {memory.reset_all()} 个会话。")
        return
    if "--reset" in flags:
        memory.reset(opts["session"])
        print(f"会话 '{memory.safe_name(opts['session'])}' 已清空。")
        return

    session = memory.load(opts["session"])

    if "--history" in flags:
        show_history(session)
        return
    if "--summary" in flags:
        print(session.get("summary") or "（还没有摘要）")
        return

    if not opts["prompt"]:
        repl(session, opts["stream"])
        return

    print(f"👤 {opts['prompt']}")
    try:
        run_turn(session, opts["prompt"], opts["stream"])
    except ApiError as exc:
        sys.exit(f"❌ {exc}")


if __name__ == "__main__":
    main()
