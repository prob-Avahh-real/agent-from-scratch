"""共享记忆层：多会话 + 分层摘要 + token 成本统计 + 会话隔离。

会话默认落在**工作目录之外**（~/.agent-from-scratch/sessions），
再配合 agent_tools 的拒绝清单，模型的文件工具读不到别的会话 —— 这是真隔离，
而不是只把历史藏起来。

分层摘要（原文永不丢）：
    messages  活跃窗口，每轮发给模型（最近 KEEP_RECENT 条原文）
    archive   更老的原文，保留但不发；可 /archive 查看、/export 导出
    summary   对 archive 的滚动压缩，作为 system 消息发给模型
超阈值时只把最老的一批挪进 archive 并刷新摘要。

本模块只用标准库；摘要用的 LLM 调用由调用方以 summarize 函数注入，方便单测。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent


def _default_session_dir() -> Path:
    """故意放在工作目录外：模型的文件工具被限制在 WORKSPACE 内，因此够不着。"""
    return Path.home() / ".agent-from-scratch" / "sessions"


SESSION_DIR = Path(os.environ.get("AGENT_SESSION_DIR", str(_default_session_dir())))
LEGACY_DIR = WORKSPACE / ".agent_sessions"
LEGACY_FILE = WORKSPACE / ".agent_memory.json"
DEFAULT_SESSION = "default"

MAX_MESSAGES = int(os.environ.get("AGENT_MAX_HISTORY", "30"))
KEEP_RECENT = int(os.environ.get("AGENT_KEEP_RECENT", "10"))
MAX_ARCHIVE = int(os.environ.get("AGENT_MAX_ARCHIVE", "200"))

# 估算价目（元 / 每百万 token）。默认值是示例，请按你自己的账单核对，
# 或直接用环境变量覆盖。cache_hit 指命中 prompt 缓存的输入。
PRICE_CACHE_HIT = float(os.environ.get("AGENT_PRICE_CACHE_HIT", "0.5"))
PRICE_CACHE_MISS = float(os.environ.get("AGENT_PRICE_CACHE_MISS", "2.0"))
PRICE_OUTPUT = float(os.environ.get("AGENT_PRICE_OUTPUT", "8.0"))

SUMMARY_INSTRUCTION = (
    "请把下面的对话压缩成要点，保留：用户身份与偏好、已确认的事实、"
    "未完成的待办、以及后续对话需要知道的关键结论。用中文，200 字以内，只输出摘要正文。"
)

STAT_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens",
             "cache_hit_tokens", "cache_miss_tokens")


def safe_name(name: str) -> str:
    cleaned = "".join(c for c in (name or "") if c.isalnum() or c in "-_")
    return cleaned or DEFAULT_SESSION


def _path(name: str) -> Path:
    return SESSION_DIR / f"{safe_name(name)}.json"


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def new_session(name: str = DEFAULT_SESSION) -> dict:
    return {
        "name": safe_name(name),
        "summary": "",
        "messages": [],
        "archive": [],
        "stats": {k: 0 for k in STAT_KEYS} | {"cost": 0.0},
    }


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

    session = new_session(name)
    session["summary"] = data.get("summary", "") or ""
    session["messages"] = data.get("messages", []) or []
    session["archive"] = data.get("archive", []) or []
    stored = data.get("stats") or {}
    for k in STAT_KEYS:
        session["stats"][k] = stored.get(k, 0)
    session["stats"]["cost"] = stored.get("cost", 0.0)
    return session


def save(session: dict) -> None:
    _ensure_dir()
    payload = {
        "name": safe_name(session.get("name", DEFAULT_SESSION)),
        "summary": session.get("summary", "") or "",
        "messages": session.get("messages", []),
        "archive": session.get("archive", []),
        "stats": session.get("stats", {}),
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
    """把旧版存储（工作区内的 .agent_sessions/ 与 .agent_memory.json）搬进新目录。"""
    _ensure_dir()
    target = SESSION_DIR.resolve()

    if LEGACY_DIR.exists() and LEGACY_DIR.resolve() != target:
        for src in sorted(LEGACY_DIR.glob("*.json")):
            dst = _path(src.stem)
            if not dst.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            src.unlink(missing_ok=True)
        try:
            LEGACY_DIR.rmdir()
        except OSError:
            pass

    if LEGACY_FILE.exists():
        dst = _path(DEFAULT_SESSION)
        if not dst.exists():
            try:
                data = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            session = new_session(DEFAULT_SESSION)
            session["messages"] = data.get("messages", [])
            save(session)
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


def trim(session: dict, summarize, max_messages: int | None = None,
         keep_recent: int | None = None, max_archive: int | None = None) -> bool:
    """活跃窗口超长时，把较老的原文挪进 archive 并刷新摘要。返回是否发生分层。

    summarize(previous_summary, transcript) -> str，由调用方注入（要走 LLM）。
    摘要失败也不会丢原文 —— archive 已经拿到了。
    """
    max_messages = MAX_MESSAGES if max_messages is None else max_messages
    keep_recent = KEEP_RECENT if keep_recent is None else keep_recent
    max_archive = MAX_ARCHIVE if max_archive is None else max_archive

    messages = session.setdefault("messages", [])
    moved = False
    if len(messages) > max_messages:
        older, recent = messages[:-keep_recent], messages[-keep_recent:]
        session["messages"] = recent
        session.setdefault("archive", []).extend(older)
        transcript = transcript_of(older)
        if transcript.strip():
            try:
                session["summary"] = summarize(session.get("summary", ""), transcript)
            except Exception as exc:  # noqa: BLE001 — 摘要失败不拖垮对话，原文已在 archive
                print(f"（摘要失败，原文已保留在 archive：{exc}）", flush=True)
        moved = True

    archive = session.get("archive", [])
    if len(archive) > max_archive:
        # 只丢最老的原文（内容早已被摘要覆盖）
        session["archive"] = archive[-max_archive:]
    return moved


def estimate_cost(cache_hit_tokens: int, cache_miss_tokens: int, completion_tokens: int) -> float:
    return (
        cache_hit_tokens * PRICE_CACHE_HIT
        + cache_miss_tokens * PRICE_CACHE_MISS
        + completion_tokens * PRICE_OUTPUT
    ) / 1_000_000


def record_usage(session: dict, usage: dict | None) -> float:
    """把一次 API 调用的 usage 记进会话统计，返回本次估算成本（元）。"""
    if not usage:
        return 0.0
    stats = session.setdefault("stats", {k: 0 for k in STAT_KEYS} | {"cost": 0.0})

    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    total = usage.get("total_tokens") or (prompt + completion)
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(prompt - hit, 0)

    stats["calls"] = stats.get("calls", 0) + 1
    stats["prompt_tokens"] = stats.get("prompt_tokens", 0) + prompt
    stats["completion_tokens"] = stats.get("completion_tokens", 0) + completion
    stats["total_tokens"] = stats.get("total_tokens", 0) + total
    stats["cache_hit_tokens"] = stats.get("cache_hit_tokens", 0) + hit
    stats["cache_miss_tokens"] = stats.get("cache_miss_tokens", 0) + miss

    delta = estimate_cost(hit, miss, completion)
    stats["cost"] = round(stats.get("cost", 0.0) + delta, 6)
    return delta


def stats_line(session: dict, delta: float = 0.0) -> str:
    s = session.get("stats") or {}
    return (
        f"tokens {s.get('total_tokens', 0)} "
        f"(in {s.get('prompt_tokens', 0)} / out {s.get('completion_tokens', 0)})"
        f" · 本轮 ¥{delta:.4f} · 累计 ¥{s.get('cost', 0.0):.4f}"
    )


def stats_report(session: dict) -> str:
    s = session.get("stats") or {}
    return "\n".join([
        f"会话      : {session.get('name')}",
        f"API 调用  : {s.get('calls', 0)}",
        f"输入 token: {s.get('prompt_tokens', 0)} "
        f"(缓存命中 {s.get('cache_hit_tokens', 0)} / 未命中 {s.get('cache_miss_tokens', 0)})",
        f"输出 token: {s.get('completion_tokens', 0)}",
        f"合计 token: {s.get('total_tokens', 0)}",
        f"估算成本  : ¥{s.get('cost', 0.0):.4f}",
        f"价目      : 命中 {PRICE_CACHE_HIT} / 未命中 {PRICE_CACHE_MISS} / "
        f"输出 {PRICE_OUTPUT} 元每百万 token（可用 AGENT_PRICE_* 覆盖）",
        f"上下文    : 活跃 {len(session.get('messages', []))} 条 · "
        f"归档 {len(session.get('archive', []))} 条 · 摘要 {len(session.get('summary') or '')} 字",
    ])
