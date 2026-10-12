"""共享记忆层：多会话存储 + 历史压缩。

会话落盘在 .agent_sessions/<name>.json：
    {"name": ..., "summary": ..., "messages": [...], "updated_at": ...}

messages 只存 user / assistant 的文本（不存 tool 消息），历史干净、
不会出现悬空的 tool_call_id。较老的对话在超过阈值时用 LLM 压成 summary，
只保留最近若干条原文，于是上下文不会无限膨胀。

这里刻意不 import requests：摘要用的 LLM 调用由调用方以 summarize 函数传进来，
方便单测，也让本模块保持纯逻辑。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent

SESSION_DIR = Path(os.environ.get("AGENT_SESSION_DIR", str(WORKSPACE / ".agent_sessions")))
LEGACY_FILE = WORKSPACE / ".agent_memory.json"
DEFAULT_SESSION = "default"

# 超过 MAX_MESSAGES 条就压缩，压缩后保留最近 KEEP_RECENT 条原文
MAX_MESSAGES = int(os.environ.get("AGENT_MAX_HISTORY", "30"))
KEEP_RECENT = int(os.environ.get("AGENT_KEEP_RECENT", "10"))

SUMMARY_INSTRUCTION = (
    "请把下面的对话压缩成要点，保留：用户身份与偏好、已确认的事实、"
    "未完成的待办、以及后续对话需要知道的关键结论。用中文，200 字以内，只输出摘要正文。"
)


def safe_name(name: str) -> str:
    cleaned = "".join(c for c in (name or "") if c.isalnum() or c in "-_")
    return cleaned or DEFAULT_SESSION


def _path(name: str) -> Path:
    return SESSION_DIR / f"{safe_name(name)}.json"


def new_session(name: str = DEFAULT_SESSION) -> dict:
    return {"name": safe_name(name), "summary": "", "messages": []}


def list_sessions() -> list[str]:
    if not SESSION_DIR.exists():
        return []
    return sorted(p.stem for p in SESSION_DIR.glob("*.json"))


def load(name: str = DEFAULT_SESSION) -> dict:
    migrate_legacy()
    name = safe_name(name)
    p = _path(name)
    if not p.exists():
        return new_session(name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"会话文件损坏，按空会话处理：{p}", flush=True)
        return new_session(name)
    return {
        "name": name,
        "summary": data.get("summary", "") or "",
        "messages": data.get("messages", []) or [],
    }


def save(session: dict) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": safe_name(session.get("name", DEFAULT_SESSION)),
        "summary": session.get("summary", "") or "",
        "messages": session.get("messages", []),
        "updated_at": int(time.time()),
    }
    _path(payload["name"]).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def reset(name: str = DEFAULT_SESSION) -> None:
    _path(name).unlink(missing_ok=True)


def reset_all() -> int:
    names = list_sessions()
    for n in names:
        reset(n)
    return len(names)


def migrate_legacy() -> None:
    """把老的单文件 .agent_memory.json 迁进 default 会话，避免用户数据丢失。"""
    if not LEGACY_FILE.exists():
        return
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    target = _path(DEFAULT_SESSION)
    if not target.exists():
        try:
            data = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        target.write_text(
            json.dumps(
                {"name": DEFAULT_SESSION, "summary": "", "messages": data.get("messages", [])},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    LEGACY_FILE.unlink(missing_ok=True)


def context(session: dict) -> list:
    """把摘要拼成一条 system 消息，放在历史之前。"""
    summary = (session.get("summary") or "").strip()
    if not summary:
        return []
    return [{"role": "system", "content": f"以下是更早对话的摘要，供你参考：\n{summary}"}]


def build_prompt(transcript: str, previous_summary: str = "") -> str:
    prev = previous_summary.strip() or "（无）"
    return f"{SUMMARY_INSTRUCTION}\n\n【已有摘要】\n{prev}\n\n【新增对话】\n{transcript}"


def transcript_of(messages: list) -> str:
    parts = []
    for m in messages:
        text = (m.get("content") or "").strip()
        if text:
            who = {"user": "用户", "assistant": "助手"}.get(m.get("role"), m.get("role", "?"))
            parts.append(f"{who}：{text}")
    return "\n".join(parts)


def trim(session: dict, summarize, max_messages: int | None = None, keep_recent: int | None = None) -> bool:
    """历史超长时压成摘要。返回是否发生了压缩。

    summarize(previous_summary, transcript) -> str，由调用方注入（要走 LLM）。
    """
    max_messages = MAX_MESSAGES if max_messages is None else max_messages
    keep_recent = KEEP_RECENT if keep_recent is None else keep_recent

    messages = session.get("messages", [])
    if len(messages) <= max_messages:
        return False

    older, recent = messages[:-keep_recent], messages[-keep_recent:]
    transcript = transcript_of(older)
    if transcript.strip():
        try:
            session["summary"] = summarize(session.get("summary", ""), transcript)
        except Exception as exc:  # noqa: BLE001 — 摘要失败不能拖垮对话，退化成直接丢弃
            print(f"（摘要失败，改为直接丢弃旧消息：{exc}）", flush=True)
    session["messages"] = recent
    return True
