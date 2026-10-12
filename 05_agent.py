"""阶段 5：打磨 —— 一个能真的拿去用的 agent。

  * 工具调用（阶段 2）   见 agent_tools.py：calculator / list_dir / read_file / write_file
  * ReAct 循环（阶段 3）  想一步 → 调工具拿观察 → 再想，直到不再请调工具
  * 记忆落盘（阶段 4）    见 memory.py：退出重进还在
  * 分层摘要（本阶段）    活跃窗口 + archive 原文 + 滚动 summary，原文不丢
  * 多会话（本阶段）      /save /load /rename /delete /new，各会话独立落盘
  * 成本统计（本阶段）    按会话累计 token 与估算成本，/stats 查看
  * 会话隔离（本阶段）    会话默认落在工作目录之外，工具层的拒绝清单兜底
  * 流式输出（本阶段）    边生成边打印
  * 错误重试（本阶段）    429 / 5xx / 网络抖动，指数退避
  * 交互 REPL（本阶段）   不给 prompt 就进对话模式

流式下最容易翻车的是 tool_calls：它按 index 分片到达，必须累积拼好再 json.loads。

用法：
    .venv/bin/python 05_agent.py "看看目录里有什么"
    .venv/bin/python 05_agent.py                 # 进 REPL
    .venv/bin/python 05_agent.py --session work "..."
    .venv/bin/python 05_agent.py --list-sessions
    .venv/bin/python 05_agent.py --history / --archive / --stats
    .venv/bin/python 05_agent.py --export out.md
    .venv/bin/python 05_agent.py --reset [--session work] / --reset-all
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
    "命令：/sessions 列出 · /save [名] 保存(给名=另存并切换) · /load <名> 切换 · "
    "/new [名] 新建 · /rename <名> 重命名 · /delete <名> 删除 · /reset 清空当前 · "
    "/summary 摘要 · /history 活跃历史 · /archive [n] 归档原文 · /stats 用量成本 · "
    "/export [文件] 导出 Markdown · exit 退出"
)


class ApiError(RuntimeError):
    pass


def fix_argv(argv: list) -> list:
    """某些非交互 shell 没有 LANG，中文 argv 会以 surrogateescape 解码成孤立代理，
    一 print 就炸。这里把原始字节按 UTF-8 还原，命令行因此对 locale 免疫。
    """
    out = []
    for a in argv:
        try:
            a.encode("utf-8")
            out.append(a)
        except UnicodeEncodeError:
            out.append(a.encode("utf-8", "surrogateescape").decode("utf-8", "replace"))
    return out


def get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("缺少 DEEPSEEK_API_KEY。先 export（或 source ~/.zshrc）再跑。")
    return key


# ---------------- HTTP：重试 + 流式 ----------------

def _headers() -> dict:
    return {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}


def post_with_retry(payload: dict, stream: bool):
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
    """发一次请求，返回 (assistant 消息, finish_reason, usage)。流式时按 index 累积 tool_calls。"""
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


def make_summarizer(session: dict):
    """返回一个 summarize(prev, transcript)，顺手把摘要这次调用的 token 也记进会话。"""
    def summarize(previous_summary: str, transcript: str) -> str:
        msg, _, usage = one_call(
            [
                {"role": "system", "content": "你是对话摘要器，只输出摘要正文。"},
                {"role": "user", "content": memory.build_prompt(transcript, previous_summary)},
            ],
            [],
            stream=False,
        )
        memory.record_usage(session, usage)
        return (msg.get("content") or "").strip()
    return summarize


# ---------------- Agent 主循环 ----------------

def run_turn(session: dict, prompt: str, stream: bool) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        *memory.context(session),
        *session["messages"],
        {"role": "user", "content": prompt},
    ]
    final = ""
    delta_total = 0.0
    tag = ""

    for step in range(1, MAX_STEPS + 1):
        msg, finish, usage = one_call(messages, TOOLS, stream)
        delta_total += memory.record_usage(session, usage)
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
                print()
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

    session["messages"].append({"role": "user", "content": prompt})
    session["messages"].append({"role": "assistant", "content": final})

    if memory.trim(session, make_summarizer(session)):
        print(
            f"      🗜️ 活跃窗口超 {memory.MAX_MESSAGES} 条：原文已归档，摘要已刷新"
            f"（归档 {len(session['archive'])} 条）"
        )
    memory.save(session)
    print(f"      [{memory.stats_line(session, delta_total)}]  ({tag})")
    return final


# ---------------- 会话展示与导出 ----------------

def show_history(session: dict) -> None:
    msgs = session.get("messages", [])
    if not msgs:
        print("（当前会话活跃窗口为空）")
        return
    for m in msgs:
        icon = {"user": "👤", "assistant": "🤖"}.get(m["role"], m["role"])
        print(f"{icon} {(m.get('content') or '')[:120]}")


def show_archive(session: dict, limit: int = 10) -> None:
    archive = session.get("archive") or []
    if not archive:
        print("（归档为空）")
        return
    shown = archive[-limit:]
    print(f"（归档共 {len(archive)} 条，显示最后 {len(shown)} 条）")
    for m in shown:
        icon = {"user": "👤", "assistant": "🤖"}.get(m["role"], m["role"])
        print(f"{icon} {(m.get('content') or '')[:120]}")


def export_session(session: dict, path: str | None = None) -> None:
    path = path or f"session-{session['name']}.md"
    p = (memory.WORKSPACE / path).resolve()
    if memory.WORKSPACE not in p.parents and p != memory.WORKSPACE:
        print("导出路径必须在工作目录内。")
        return
    lines = [
        f"# 会话 {session['name']}", "",
        "## 统计", "", "~~~", memory.stats_report(session), "~~~", "",
    ]
    if session.get("summary"):
        lines += ["## 滚动摘要", "", session["summary"], ""]
    archive = session.get("archive") or []
    if archive:
        lines += [f"## 归档原文（{len(archive)} 条）", ""]
        lines += [f"- **{m['role']}**: {m.get('content') or ''}" for m in archive]
        lines += [""]
    lines += [f"## 活跃窗口（{len(session.get('messages', []))} 条）", ""]
    lines += [f"- **{m['role']}**: {m.get('content') or ''}" for m in session.get("messages", [])]
    p.write_text("\n".join(lines), encoding="utf-8")
    print(f"（已导出到 {path}）")


# ---------------- 会话命令 ----------------

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
            print(f"（已切到会话 '{session['name']}'：活跃 {len(session['messages'])} 条、"
                  f"归档 {len(session['archive'])} 条）")
    elif cmd == "/new":
        session = memory.new_session(arg or memory.DEFAULT_SESSION)
        memory.save(session)
        print(f"（新会话 '{session['name']}'）")
    elif cmd == "/rename":
        if not arg:
            print("用法：/rename <新名字>")
        else:
            old = session["name"]
            session["name"] = memory.safe_name(arg)
            memory.save(session)
            if old != session["name"]:
                memory.reset(old)
            print(f"（'{old}' → '{session['name']}'）")
    elif cmd == "/delete":
        if not arg:
            print("用法：/delete <名字>")
        elif memory.safe_name(arg) == session["name"]:
            print("不能删当前会话，先 /load 或 /new 别的；要清空用 /reset。")
        else:
            memory.reset(arg)
            print(f"（已删除会话 '{memory.safe_name(arg)}'）")
    elif cmd == "/reset":
        memory.reset(session["name"])
        session["summary"] = ""
        session["messages"] = []
        session["archive"] = []
        session["stats"] = {k: 0 for k in memory.STAT_KEYS} | {"cost": 0.0}
        print("（当前会话已清空）")
    elif cmd == "/summary":
        print(session.get("summary") or "（还没有摘要）")
    elif cmd == "/history":
        show_history(session)
    elif cmd == "/archive":
        show_archive(session, int(arg) if arg.isdigit() else 10)
    elif cmd == "/stats":
        print(memory.stats_report(session))
    elif cmd == "/export":
        export_session(session, arg or None)
    else:
        print(f"未知命令：{cmd}（/help 看帮助）")
    return session


def repl(session: dict, stream: bool) -> None:
    print(
        f"（会话 '{session['name']}'：活跃 {len(session['messages'])} 条、"
        f"归档 {len(session['archive'])} 条"
        + ("，含摘要" if session.get("summary") else "")
        + f"；存储 {memory.SESSION_DIR}）"
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
    opts = {
        "stream": True,
        "session": memory.DEFAULT_SESSION,
        "flags": set(),
        "prompt": "",
        "export": None,
    }
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in {"--session", "--export"}:
            i += 1
            if i >= len(argv):
                sys.exit(f"{a} 需要一个值")
            opts["session" if a == "--session" else "export"] = argv[i]
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
    sys.argv = fix_argv(sys.argv)
    opts = parse_args(sys.argv[1:])
    flags = opts["flags"]

    if "--list-sessions" in flags:
        names = memory.list_sessions()
        print("会话：" + ("、".join(names) if names else "（还没有）"))
        print(f"存储目录：{memory.SESSION_DIR}")
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
    if "--archive" in flags:
        show_archive(session)
        return
    if "--stats" in flags:
        print(memory.stats_report(session))
        return
    if opts["export"]:
        export_session(session, opts["export"])
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
