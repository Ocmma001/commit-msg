#!/usr/bin/env python3
"""对话记录（transcript）采集：定位、解析、相关性筛选。

只负责「对话文件」，不碰 git —— git 侧逻辑全部在 collect.py。
"""
from __future__ import annotations

import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol, TypedDict, runtime_checkable


class Message(TypedDict):
    """一条对话消息 —— 对外契约，不是内部实现细节。

    用 TypedDict 而非 dataclass/NamedTuple 是刻意的：它是纯静态标注，
    运行时仍然就是 dict。消费方按下标取值 `m["text"]`，保持字典接口。

    timestamp 必须为带时区的 datetime，默认实现统一为 UTC；缺字段、解析失败
    或无时区时为 None。消费方应在比较和排序前过滤不可用时间戳，不猜测时区。
    """

    role: str
    text: str
    timestamp: datetime | None
    session: str


@runtime_checkable
class ConversationSource(Protocol):
    """对话源统一约定：任何客户端只要实现这一个方法就能接进来。

    Claude Code 只是其中一种实现（见 ClaudeCodeSource），不是唯一形态。
    想接别的客户端（Cursor、Codex、自建记录），实现下面这个签名即可，
    不必继承任何基类、也不必 import 本模块。

    - project_path：项目绝对路径，**原样传入**，由实现方决定怎么定位自己的记录；
      不要假设调用方已经把它编码成 Claude Code 的目录名。
    - keywords：路径与文件名，通常用来过滤；实现方可自行决定怎么用。
    - anchor_time / window：期望的时间范围，anchor 必须带时区，一般是改动文件的 mtime。
    - limit：返回条数上限。实现方至少要保证不超过它。
    - 返回 [] 表示「没有」，不是错误 —— 调用方据此给出建议，不区分是哪种「没有」。
    """

    def search(
        self,
        project_path: str,
        keywords: Iterable[str],
        anchor_time: datetime,
        window: timedelta,
        limit: int,
    ) -> list[Message]:
        ...


def encode_project_dir(path: str) -> str:
    """把项目绝对路径编码成 Claude Code 的 transcript 目录名。

    d:\\projects\\my-app  ->  d--projects-my-app

    注意：盘符大小写保持原样，本函数不做归一化。匹配时由
    find_project_dir() 负责大小写不敏感的查找。
    """
    out = []
    for ch in path:
        out.append("-" if ch in ":\\/" else ch)
    return "".join(out)


def find_project_dir(cwd: str, projects_root: Path) -> Path | None:
    """在 projects_root 下找到 cwd 对应的 transcript 目录。

    大小写不敏感匹配：实测环境中盘符可能以 'd--' 或 'D--' 两种形态出现。
    """
    if not projects_root.is_dir():
        return None
    target = encode_project_dir(cwd).lower()
    for entry in projects_root.iterdir():
        if entry.is_dir() and entry.name.lower() == target:
            return entry
    return None


def extract_text(entry: dict) -> str:
    """从一条 transcript 记录里取出人类可读的对话正文。

    content 有两种形态：
      - 字符串（常见于 user 消息）
      - 内容块数组（assistant 消息，以及部分 user 消息）
    工具调用块（tool_use / tool_result）不是对话，直接丢弃。
    """
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _aware_utc(value: datetime | None) -> datetime | None:
    """只接受明确带时区的时间，不把本机时区猜作记录的时区。"""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _utc_anchor(anchor: datetime) -> datetime:
    normalized = _aware_utc(anchor)
    if normalized is None:
        raise ValueError("anchor 必须为带时区的 datetime")
    return normalized


def _parse_timestamp(raw: str | None) -> datetime | None:
    # 挡住非字符串：epoch 秒（1758340860）是 JSONL 时间戳的常见写法，而 int
    # 没有 .replace，会抛 AttributeError —— `except ValueError` 接不住。生成器
    # 一抛栈，collect.py chat 的 list() 就是整个命令失败，而不是优雅降级。
    if not isinstance(raw, str):
        return None
    try:
        # .replace 是 Py3.10 兼容保险：3.11+ 的 fromisoformat 原生接受 'Z'
        return _aware_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def iter_messages(jsonl_path: Path) -> Iterator[Message]:
    """逐行解析 transcript，产出规范化后的对话消息。

    逐行解析意味着内存占用与文件大小无关 —— transcript 可以长到上百 MB。
    yield 的字段见 Message。timestamp 可能为 None，下游按时间排序前必须先滤掉。
    """
    session = jsonl_path.stem
    # utf-8-sig：BOM 开头的文件若按 utf-8 读，BOM 会跟着首行进 json.loads →
    # JSONDecodeError → 被 continue 静默吞掉，第一条合法消息无声丢失
    with jsonl_path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in ("user", "assistant"):
                continue
            if entry.get("isSidechain"):
                continue
            # isMeta 是插件/系统注入的样板文本（整篇 SKILL.md、命令横幅、
            # 图片占位符）。实测占对话正文输出字符的 27.8%，且读起来像祈使
            # 句式的工程指令 —— 混进「用户说的话」会诱发编造动机。
            if entry.get("isMeta"):
                continue
            text = extract_text(entry)
            if not text:
                continue
            yield {
                "role": entry["type"],
                "text": text,
                "timestamp": _parse_timestamp(entry.get("timestamp")),
                "session": session,
            }


def select_relevant(
    messages: Iterable[Message],
    keywords: Iterable[str],
    anchor: datetime,
    window: timedelta,
    limit: int | None = 40,
    max_chars: int = 8000,
) -> list[Message]:
    """筛出与本次提交相关的对话片段。

    两个信号必须同时满足：
      1. 消息文本里出现任一关键词（通常是文件名，大小写不敏感）
      2. 时间戳落在 anchor ± window 之内（anchor 通常是文件 mtime）

    anchor 必须带时区，否则抛 ValueError。消息的 None 或无时区时间戳会跳过；
    有效时间统一为 UTC，比较不同客户端的记录时不依赖本机时区。

    取交集是因为任一单信号都太粗：只按文件名会捞进大量顺带提到该文件的
    闲聊，只按时间窗口会把整段时间的无关对话全带上。交集把 token 花在
    同时具备两个信号的片段上。注意文件名才是主过滤器 —— 对话里没提过
    文件名的改动，这里筛不出东西，这不是本函数的缺陷而是它的定义。

    两道上限缺一不可：
      - limit：条数上限。None 表示不限量；0 表示条数预算为零
      - max_chars：字符软预算。只限条数不够 —— 实测单条消息长度差异极大，
        40 条 × 3 个 session 最坏可达约 89KB（中文约 3 万 token）
    两者都从**最旧**的消息开始丢弃，因为越近的对话越可能解释当前改动，
    但**至少保留最新的一条命中**，绝不裁到空（见下方 while 条件）。
    """
    anchor = _utc_anchor(anchor)
    lowered = [k.lower() for k in keywords if k]
    hits = []
    for msg in messages:
        ts = _aware_utc(msg.get("timestamp"))
        if ts is None:
            continue
        if abs(ts - anchor) > window:
            continue
        # isinstance 而非 `or ""`：`("x" or "")` 仍是真值，真值非 str 会在
        # .lower() 上抛 AttributeError，与各层的加固约定保持一致
        raw = msg.get("text")
        text = (raw if isinstance(raw, str) else "").lower()
        if not any(k in text for k in lowered):
            continue
        # 不改写第三方传入的字典，返回的消息时间统一为 UTC。
        hits.append({**msg, "timestamp": ts})
    hits.sort(key=lambda m: m["timestamp"])
    if limit is not None:
        # limit=0 表示条数预算为零，不限量请传 None。不能写成
        # `hits[-limit:]` 一把梭 —— limit=0 时 `-0 == 0`，切出的是整个列表
        hits = hits[-limit:] if limit > 0 else []

    total = sum(len(m["text"]) for m in hits)
    # 保留至少一条：触发条件不是「消息太长」而是「最新那条命中自身超过
    # max_chars」，此时循环会把它也弹掉、返回空列表，下游于是输出「对话
    # 记录里没有相关讨论」—— 实测本仓库 4 组 (session, 文件) 里 3 组中招，
    # 被丢的正是 14KB 的上下文续接摘要，「为什么」最密集的一条。
    # 代价是 max_chars 变为软上限，最坏退化为单条消息长度，仍有界。
    while len(hits) > 1 and total > max_chars:
        total -= len(hits.pop(0)["text"])
    return hits


class ClaudeCodeSource:
    """ConversationSource 的 Claude Code 实现：读 ~/.claude/projects 下的 JSONL。

    「当前会话」是本实现独有的启发式（跳过 mtime 最新的那份记录文件），所以
    它是构造参数而不是 search() 的参数 —— 契约里不该出现只有一种实现才有的概念。
    """

    def __init__(
        self,
        projects_root: Path,
        exclude_current: bool = True,
        max_sessions: int = 3,
        max_chars: int = 8000,
    ) -> None:
        self.projects_root = Path(projects_root)
        self.exclude_current = exclude_current
        self.max_sessions = max_sessions
        self.max_chars = max_chars

    def search(
        self,
        project_path: str,
        keywords: Iterable[str],
        anchor_time: datetime,
        window: timedelta,
        limit: int,
    ) -> list[Message]:
        anchor_time = _utc_anchor(anchor_time)
        try:
            project_dir = find_project_dir(project_path, self.projects_root)
            if project_dir is None:
                return []
            # iterdir 会报告目录访问错误；glob 在部分 Python 版本会静默吞掉它。
            session_paths = [
                path for path in project_dir.iterdir()
                if path.suffix.lower() == ".jsonl"
            ]
        except OSError as exc:
            warnings.warn(
                f"无法读取对话目录（{type(exc).__name__}），检索结果可能不完整。",
                RuntimeWarning, stacklevel=2,
            )
            return []

        available = []
        for session in session_paths:
            try:
                available.append((session.stat().st_mtime, session))
            except OSError as exc:
                warnings.warn(
                    f"已跳过无法访问的对话记录 {session.name}（{type(exc).__name__}）。",
                    RuntimeWarning, stacklevel=2,
                )
        available.sort(key=lambda item: item[0], reverse=True)
        sessions = [session for _, session in available]
        if self.exclude_current:
            sessions = sessions[1:]

        keywords = list(keywords)
        picked: list[Message] = []
        readable_sessions = 0
        for session in sessions:
            if readable_sessions >= self.max_sessions:
                break
            try:
                relevant = select_relevant(
                    iter_messages(session),
                    keywords=keywords,
                    anchor=anchor_time,
                    window=window,
                    limit=limit,
                    max_chars=self.max_chars,
                )
            except OSError as exc:
                warnings.warn(
                    f"已跳过无法读取的对话记录 {session.name}（{type(exc).__name__}）。",
                    RuntimeWarning, stacklevel=2,
                )
                continue
            readable_sessions += 1
            picked.extend(relevant)
        # 默认源自身也遵守全局条数契约；调用方仍可再裁剪以防第三方违规。
        picked.sort(key=lambda m: m["timestamp"])
        return picked[-limit:] if limit > 0 else []
