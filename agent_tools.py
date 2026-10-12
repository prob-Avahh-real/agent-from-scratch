"""共享工具层。

阶段 2/3/5 以前各自复制了一份工具代码，这里收敛成唯一一份：
工具的 Python 实现、给模型看的 JSON Schema、以及执行入口。

只用标准库，方便单独 import / 测试。
"""

from __future__ import annotations

import ast
import operator
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent


# ---------------- 实现 ----------------

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("只支持数字和 + - * / % ** 运算")


def calculator(expression: str) -> str:
    return str(_safe_eval(ast.parse(expression, mode="eval")))


def _resolve(path: str) -> Path:
    """把相对路径钉死在工作目录内，挡掉 ../../etc/passwd 这类越界。"""
    p = (WORKSPACE / path).resolve()
    if WORKSPACE not in p.parents and p != WORKSPACE:
        raise ValueError("只能访问工作目录内的文件")
    return p


def list_dir(path: str = ".") -> str:
    p = _resolve(path)
    if not p.exists():
        return f"路径不存在：{path}"
    items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    return "\n".join(f"{'[dir] ' if i.is_dir() else '      '}{i.name}" for i in items) or "（空目录）"


def read_file(path: str, start_line: int = 1, max_lines: int = 100) -> str:
    p = _resolve(path)
    if not p.is_file():
        return f"不是文件：{path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[start_line - 1: start_line - 1 + max_lines]
    return "\n".join(f"{start_line + i:>4} | {t}" for i, t in enumerate(chunk)) or "（无内容）"


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    if p.is_dir():
        return f"是目录，不能写入：{path}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字符）"


TOOL_IMPL = {
    "calculator": calculator,
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个数学表达式，支持 + - * / % **。需要算术时用它，别心算。",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "例如 (17*23+5)/6"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出工作目录下某个路径的文件和子目录。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作目录内某个文本文件，返回带行号的文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 01_chat.py"},
                    "start_line": {"type": "integer", "description": "起始行，默认 1"},
                    "max_lines": {"type": "integer", "description": "最多读多少行，默认 100"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入工作目录内的文件（会覆盖）。只在用户明确要求写文件时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "content": {"type": "string", "description": "要写入的完整文本"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def execute(name: str, args: dict) -> str:
    """执行工具；异常也变成字符串，让模型看到"观察"而不是让进程崩掉。"""
    fn = TOOL_IMPL.get(name)
    if fn is None:
        return f"ERROR: 没有名为 {name} 的工具"
    try:
        return str(fn(**args))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
