#!/usr/bin/env python3
"""采集 commit 的 Git 证据；当前对话上下文由 Codex 直接提供。

两个子命令：
    overview            改动概览（供选文件用）
    facts    <path>...  选中文件的事实包（供分析用）
"""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path

# 命中即警告的敏感文件。理由：一旦提交，密钥就进入 git 历史，
# 即使下个提交删掉也仍保留，push 出去等同于泄露。
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", ".netrc"}
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".env")
# credentials / secrets 用前缀而非精确名：设计文档写的就是 credentials* /
# secrets*。这会让 credentials.md 这类文档文件误报，这是刻意的取舍 ——
# 误报只是多问一句，漏报的代价是密钥进 git 历史且不可撤销。
SENSITIVE_PREFIXES = (
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env.",
    "credentials", "secrets",
)
SENSITIVE_DIRS = (
    ".aws", ".ssh", "node_modules", "venv", ".venv",
    "__pycache__", "dist", "build",
)
MAX_FILE_BYTES = 5 * 1024 * 1024

# facts 单份载荷的字节上限。**行数闸门挡不住压缩产物**：min.js / 打包产物 /
# 压缩源码全都只有 1 行，`1 <= max_lines` 永远成立，于是整份倒进上下文。
# 实测（加这道闸之前）：1.6MB 单行文件输出 1,600,044 字节（约 40 万 token），
# 8MB 的输出 8,000,045 字节（约 200 万 token）—— 一次调用就能打爆上下文窗口。
# 而 overview 早把 too_large 算成 True 了，facts 却没看过它。
# 200KB ≈ 5 万 token：够装下正常源码文件的两份全文，又不至于让人肉疼。
MAX_FACTS_BYTES = 200_000
MAX_TOTAL_FACTS_BYTES = 200_000
CONFLICT_STATES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


class GitError(RuntimeError):
    pass


def _over_cap_message(size: int, max_bytes: int) -> str:
    return (f"（内容过大：{size} 字节，超过 {max_bytes} 字节上限，"
            f"已省略。需要时请单独查看该文件）")


def _capped(text: str, max_bytes: int) -> str:
    """载荷超过字节上限时替换为一句说明，不把内容倒进上下文。"""
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        return _over_cap_message(size, max_bytes)
    return text


def _truncation_notice(max_bytes: int) -> str:
    return f"\n\n（已截断；总输出超过 {max_bytes} 字节，请缩小范围后单独查看）"


def _bounded_output(text: str, max_bytes: int) -> str:
    """UTF-8 硬预算包含提示本身，截取不会留下半个字符。"""
    if max_bytes <= 0:
        raise ValueError("输出字节上限必须大于 0")
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return text
    notice = _truncation_notice(max_bytes)
    suffix = notice.encode("utf-8")
    if len(suffix) >= max_bytes:
        return suffix[:max_bytes].decode("utf-8", errors="ignore")
    return payload[:max_bytes - len(suffix)].decode("utf-8", errors="ignore") + notice


def run_git(args, cwd=None):
    """执行 git 命令。

    关键：强制 core.quotepath=false。默认情况下 git 会把非 ASCII 路径
    转义成 \\344\\275\\240 形式，中文文件名会彻底不可读。
    """
    args = list(args)
    if args and args[0] == "diff":
        args[1:1] = ["--no-ext-diff", "--no-textconv"]
    proc = subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.quotepath=false", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def is_sensitive(path: str) -> bool:
    p = Path(path)
    name = p.name.lower()
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(SENSITIVE_PREFIXES):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return any(part.lower() in SENSITIVE_DIRS for part in p.parts[:-1])


def _count_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _numstat(repo: Path, *extra):
    """把 git diff --numstat -z 解析成 ({path: (added, deleted)}, {二进制路径})。

    用 -z 而不是自己解析 ' => ' 记法：git 的重命名记法有三种形态
    （'old => new'、'{a => b}/f'、'pre{a => b}post'），靠字符串启发式还原
    会在路径自带花括号时取错配对、直接抛 IndexError；文件名本身含 ' => '
    时又会把键改写成另一条路径。-z 把每条记录的路径拆成独立字段，没有歧义。

    顺序陷阱：重命名记录里两个路径是**旧在前、新在后**，与 porcelain -z
    的「新在前」相反，别抄错。

    实测输出（照此比对）：
        普通      '100\t0\tbig.py\0'
        重命名    '2\t1\t\0old.txt\0new.txt\0'   -> 字段 ['2\t1\t','old.txt','new.txt','']
        二进制    '-\t-\timg.png\0'
    """
    out = run_git(["diff", "--numstat", "-M", "-z", *extra], cwd=repo)
    fields, result, binaries, i = out.split("\0"), {}, set(), 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        added, deleted, path = rec.split("\t", 2)
        if not path and i + 1 < len(fields):
            # 重命名：路径字段为空，旧、新各占一个独立字段
            i += 1
            path = fields[i]
            i += 1
        if added == "-":
            binaries.add(path)
            result[path] = (0, 0)
        else:
            result[path] = (int(added), int(deleted))
    return result, binaries


def _parse_porcelain_z(raw: str):
    """解析 git status --porcelain -z 输出。

    使用 -z 是因为文件名可能含空格或换行，按空白切会解析错。
    重命名条目形如：'R  new\\0old\\0'（新路径在前，旧路径在后）。
    """
    entries = []
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        xy, path = field[:2], field[3:]
        old_path = None
        if xy[0] in "RC" or xy[1] in "RC":
            if i < len(fields):
                old_path = fields[i]
                i += 1
        entries.append({"xy": xy, "path": path, "old_path": old_path})
    return entries


def _classify(xy: str) -> str:
    if xy in CONFLICT_STATES:
        return "conflicted"
    if xy == "??":
        return "untracked"
    if xy[0] in "RC" or xy[1] in "RC":
        return "renamed"
    if xy[0] == "A":
        return "added"
    if xy[0] == "D" or xy[1] == "D":
        return "deleted"
    return "modified"


def _index_modes(repo: Path) -> dict[str, str]:
    modes = {}
    for record in run_git(["ls-files", "--stage", "-z"], cwd=repo).split("\0"):
        if record:
            header, path = record.split("\t", 1)
            mode, _, stage = header.split()
            if stage == "0" or path not in modes:
                modes[path] = mode
    return modes


def _head_modes(repo: Path, paths) -> dict[str, str]:
    modes = {}
    for record in run_git(["ls-tree", "-r", "-z", "HEAD", "--", *paths], cwd=repo).split("\0"):
        if record:
            header, path = record.split("\t", 1)
            modes[path] = header.split()[0]
    return modes


def _kind(path: Path, mode: str | None) -> str:
    if mode == "160000":
        return "submodule"
    if mode == "120000" or path.is_symlink():
        return "symlink"
    return "directory" if path.is_dir() else "regular"


def overview(repo: Path) -> dict:
    """产出改动概览：分支、提交数、以及每个改动文件的状态与行数。"""
    repo = Path(repo)
    try:
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).strip()
    except GitError:
        branch = ""
    try:
        run_git(["rev-parse", "--verify", "HEAD"], cwd=repo)
        has_head = True
        commit_count = int(
            run_git(["rev-list", "--count", "HEAD"], cwd=repo).strip() or 0
        )
    except GitError:
        has_head = False
        commit_count = 0

    if has_head:
        head_stat, binaries = _numstat(repo, "HEAD")
    else:
        # 空仓库没有 HEAD，`git diff HEAD` 会直接 fatal。此刻唯一有意义的基准
        # 是「索引 vs 空树」，也就是首次提交会提交的内容。少了这一支，空仓库里
        # 每个已 add 的文件都报 +0，且会让 facts 把任意大的新文件当小文件全量输出。
        head_stat, binaries = _numstat(repo, "--cached")

    modes = _index_modes(repo)
    entries = {}
    for entry in _parse_porcelain_z(
        # 显式 --renames：run_git 只强制 core.quotepath，status 会跟随用户的
        # status.renames / diff.renames 配置，而 _numstat 用了 -M。两边不一致时
        # 重命名会被拆成「新增」+「删除 −0」，静默丢掉删除侧的行数。
        run_git(["status", "--porcelain", "-z", "-uall", "--renames"], cwd=repo)
    ):
        # 暂存删除后重建会同时输出 D 和 ??，保留两侧而不是重复文件。
        previous = entries.get(entry["path"])
        if previous is not None:
            if entry["xy"] == "??":
                previous["xy"] = previous["xy"][0] + "?"
            else:
                entry["xy"] = entry["xy"][0] + "?"
                entries[entry["path"]] = entry
        else:
            entries[entry["path"]] = entry

    files = []
    for entry in entries.values():
        path, xy = entry["path"], entry["xy"]
        status = _classify(xy)
        added, deleted = head_stat.get(path, (0, 0))
        binary = path in binaries
        full = repo / path
        kind = _kind(full, modes.get(path))
        if status == "untracked" and kind == "regular":
            # 未跟踪文件既不在 HEAD 里也不在索引里，两个 numstat 都问不到，
            # 行数和二进制与否都只能自己认
            binary = _is_binary(repo, path, has_head)
            added = 0 if binary else _count_lines(full)
            deleted = 0
        conflicted = xy in CONFLICT_STATES
        staged = xy[0] not in " ?"
        unstaged = xy[1] != " "
        files.append({
            "path": path,
            "old_path": entry["old_path"],
            "status": status,
            "index_status": xy[0],
            "worktree_status": xy[1],
            "staged": staged,
            "unstaged": unstaged,
            "conflicted": conflicted,
            "kind": kind,
            "added": added,
            "deleted": deleted,
            "binary": binary,
            "sensitive": is_sensitive(path),
            "too_large": kind == "regular" and full.is_file() and full.stat().st_size > MAX_FILE_BYTES,
            "partially_staged": staged and unstaged and not conflicted,
        })
    files.sort(key=lambda f: f["path"])
    return {
        "branch": branch,
        "has_head": has_head,
        "commit_count": commit_count,
        "files": files,
    }


def _is_binary(repo: Path, path: str, has_head: bool = True) -> bool:
    """判断文件是不是二进制。

    先问 git（--numstat 对二进制输出 '-\\t-\\t'），问不到再自己解码试。
    空仓库里没有 HEAD，`git diff HEAD` 会直接报错，所以要先看 has_head。
    """
    if has_head:
        out = run_git(["diff", "--numstat", "-M", "HEAD", "--", path], cwd=repo)
        lines = out.strip().splitlines()
        if lines and lines[0].startswith("-\t-\t"):
            return True
    full = repo / path
    if full.is_symlink() or not full.is_file():
        return False
    with full.open("rb") as fh:
        head = fh.read(8000)
    # 与 git 自身的 buffer_is_binary 对齐：只看前 8000 字节里有没有 NUL。
    # 不能只靠 decode —— NUL 是**合法** UTF-8，纯 NUL 文件能解码成功，
    # 然后被当作文本整份倒出来。
    if b"\0" in head:
        return True
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _blob_text(repo: Path, spec: str, max_bytes: int) -> tuple[str | None, int]:
    size = int(run_git(["cat-file", "-s", spec], cwd=repo).strip())
    return (run_git(["show", spec], cwd=repo) if size <= max_bytes else None), size


def _file_facts(repo, info, source, has_head, head_modes, index_modes,
                stats, binaries, max_lines, context, max_bytes):
    path = info["path"]
    full = repo / path
    old_path = info["old_path"] or path
    if source == "worktree" and old_path != path:
        old_full = repo / old_path
        if old_full.exists() or old_full.is_symlink():
            # 索引里的 rename 已在工作区变成保留/恢复旧文件，分别分析两侧。
            parts = []
            for current in (old_path, path):
                item = dict(info, path=current, old_path=None,
                            kind=_kind(repo / current, index_modes.get(current)))
                parts.append(_file_facts(repo, item, source, has_head, head_modes,
                                         index_modes, stats, binaries, max_lines, context, max_bytes))
            return "\n\n".join(parts)
    before_mode = head_modes.get(old_path)
    exists = path in index_modes if source == "index" else full.exists() or full.is_symlink()
    header = f"### {path}"
    if not exists and before_mode is None:
        return f"{header}（所选版本相对 HEAD 无改动）"

    scope = [old_path, path] if old_path != path else [path]
    baseline = ["--cached"] if source == "index" else ["HEAD"]
    diff = run_git(["diff", "-M", f"-U{context}", *baseline, "--", *scope], cwd=repo) \
        if has_head or source == "index" else ""
    added, deleted = stats.get(path, (0, 0))
    kinds = {"120000": "符号链接", "160000": "子模块"}
    mode_kind = kinds.get(before_mode) or kinds.get(index_modes.get(path))
    kind = mode_kind or {"symlink": "符号链接", "submodule": "子模块",
                         "directory": "目录"}.get(info["kind"])
    recreated = source == "worktree" and before_mode is not None and exists and path not in index_modes
    if kind and recreated:
        if full.is_symlink():
            detail = f"工作区类型：符号链接\n目标：{full.readlink()}"
        elif full.is_file():
            if _is_binary(repo, path, False):
                detail = "工作区类型：普通二进制文件，无法展示内容"
            else:
                size = full.stat().st_size
                payload = full.read_text(encoding="utf-8", errors="replace") \
                    if size <= max_bytes else _over_cap_message(size, max_bytes)
                detail = f"工作区类型：普通文本文件\n\n{payload}"
        else:
            detail = "工作区类型：目录，请单独核对 Git 元数据"
        return f"{header}（重建后的类型变化，HEAD 模式 {before_mode}）\n\n{_capped(detail, max_bytes)}"
    if kind:
        if source == "worktree" and not has_head:
            if full.is_symlink():
                detail = f"目标：{full.readlink()}"
            elif kind == "子模块" and full.is_dir():
                detail = "Subproject commit " + run_git(["rev-parse", "HEAD"], cwd=full).strip()
            elif kind == "符号链接" and full.is_file():
                # core.symlinks=false 时，Git 用普通文件保存链接目标文字。
                size = full.stat().st_size
                detail = full.read_text(encoding="utf-8", errors="replace") \
                    if size <= max_bytes else _over_cap_message(size, max_bytes)
            else:
                detail = "请根据当前工作区 Git 元数据单独核对"
        else:
            detail = diff or "所选版本相对 HEAD 无改动"
        return f"{header}（{kind}，仅展示 Git 元数据）\n\n{_capped(detail, max_bytes)}"

    if not exists:
        return f"{header}（已删除，−{deleted} 行）\n\n{_capped(diff, max_bytes)}"
    binary = path in binaries if source == "index" else _is_binary(repo, path, has_head)
    if binary:
        return f"{header}（二进制文件，无法展示内容）"
    if has_head and before_mode is not None and not diff and not recreated:
        return f"{header}（所选版本相对 HEAD 无改动）"

    if source == "index":
        after, after_size = _blob_text(repo, f":{path}", max_bytes)
    else:
        after_size = full.stat().st_size
        after = full.read_text(encoding="utf-8", errors="replace") if after_size <= max_bytes else None
    if before_mode is None:
        label = "新增文件（已暂存）" if info["staged"] else "新增文件"
        if after is None:
            return f"{header}（{label}）\n\n{_over_cap_message(after_size, max_bytes)}"
        line_count = len(after.splitlines())
        payload = after if line_count <= max_lines else "\n".join(after.splitlines()[:max_lines])
        notice = "" if line_count <= max_lines else f"（文件超过 {max_lines} 行，仅显示前 {max_lines} 行）\n\n"
        return f"{header}（{label}，{line_count} 行）\n\n{notice}{_capped(payload, max_bytes)}"

    if old_path != path:
        return f"{header}（重命名，原路径 {old_path}，+{added} −{deleted}）\n\n{_capped(diff, max_bytes)}"
    before, before_size = _blob_text(repo, f"HEAD:{old_path}", max_bytes)
    if recreated:
        # index 已删除的路径在 Git 看来未跟踪，diff HEAD 不读取重建后的内容。
        if before is None or after is None:
            return f"{header}（工作区文件已重建）\n\n{_over_cap_message(max(before_size, after_size), max_bytes)}"
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=context,
        ))
        if not diff:
            return f"{header}（所选版本相对 HEAD 无改动）"
        operations = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines()).get_opcodes()
        added = sum(j2 - j1 for op, i1, i2, j1, j2 in operations if op in ("insert", "replace"))
        deleted = sum(i2 - i1 for op, i1, i2, j1, j2 in operations if op in ("delete", "replace"))
    header = f"{header}（+{added} −{deleted}）"
    if before is not None and after is not None:
        before_lines, after_lines = len(before.splitlines()), len(after.splitlines())
        if before != after and before_lines <= max_lines and after_lines <= max_lines:
            return (f"{header}\n\n--- 改前完整内容（{before_lines} 行）---\n\n"
                    f"{_capped(before, max_bytes)}\n\n--- 改后完整内容（{after_lines} 行）---\n\n"
                    f"{_capped(after, max_bytes)}")
    return f"{header} 文件较大或包含元数据变化，仅提供 diff\n\n{_capped(diff, max_bytes)}"


def facts(repo: Path, paths, max_lines: int = 400, context: int = 10,
          max_bytes: int = MAX_FACTS_BYTES, source: str = "worktree",
          max_total_bytes: int = MAX_TOTAL_FACTS_BYTES) -> str:
    """按指定版本采集事实；默认保留整个工作区版本的既有调用语义。"""
    if source not in ("index", "worktree"):
        raise ValueError("source 必须是 index 或 worktree")
    if min(max_lines, max_bytes, max_total_bytes) <= 0 or context < 0:
        raise ValueError("行数与字节上限必须大于 0，context 不能为负数")
    repo = Path(repo)
    data = overview(repo)
    status = {f["path"]: f for f in data["files"]}
    selected = []
    for raw in paths:
        path = str(raw).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        info = status.get(path)
        if info is None:
            raise GitError(f"路径没有改动或不存在：{raw}")
        if info["conflicted"]:
            raise GitError(f"路径存在未解决的冲突，不能生成最终提交证据：{path}")
        if source == "index" and not info["staged"]:
            raise GitError(f"路径没有已暂存改动：{path}")
        if info not in selected:
            selected.append(info)
    has_head = data["has_head"]
    index_modes = _index_modes(repo)
    scope = list(dict.fromkeys(p for f in selected for p in (f["old_path"], f["path"]) if p))
    head_modes = _head_modes(repo, scope) if has_head else {}
    if source == "index" or not has_head:
        stats, binaries = _numstat(repo, "--cached")
    else:
        stats, binaries = _numstat(repo, "HEAD")
    chunks = ["证据版本：" + ("暂存区（HEAD → index）" if source == "index" else "工作区（HEAD → worktree）")]
    output_size = len(chunks[0].encode("utf-8"))
    for info in selected:
        chunk = _file_facts(repo, info, source, has_head, head_modes, index_modes,
                            stats, binaries, max_lines, context, max_bytes)
        chunks.append(chunk)
        output_size += 2 + len(chunk.encode("utf-8"))
        if output_size > max_total_bytes:
            break
    return _bounded_output("\n\n".join(chunks), max_total_bytes)


def main(argv=None) -> int:
    # 输出强制 UTF-8，避免中文路径或 U+2212 等字符在不同终端之间产生乱码。
    # backslashreplace 兜底：将来出现别的不可编码字符时降级显示，而不是崩。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")
        except (AttributeError, OSError, ValueError):
            pass  # 部分输出流不支持 reconfigure，保留原有设置

    parser = argparse.ArgumentParser(prog="collect", description="采集 commit 事实包")
    parser.add_argument("--repo", default=".", help="仓库路径，默认当前目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview", help="改动概览")

    p_facts = sub.add_parser("facts", help="选中文件的事实包")
    p_facts.add_argument("paths", nargs="+")
    p_facts.add_argument("--max-lines", type=int, default=400)
    p_facts.add_argument("--context", type=int, default=10)
    p_facts.add_argument("--max-bytes", type=int, default=MAX_FACTS_BYTES)
    p_facts.add_argument("--source", choices=("index", "worktree"), default="worktree",
                         help="证据版本：index 为暂存区，worktree 为整个文件工作区版本")
    p_facts.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_FACTS_BYTES)

    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.cmd == "overview":
            print(json.dumps(overview(repo), ensure_ascii=False, indent=2))
        else:
            print(facts(repo, args.paths, max_lines=args.max_lines,
                        context=args.context, max_bytes=args.max_bytes,
                        source=args.source, max_total_bytes=args.max_total_bytes), end="")
    except (GitError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
