# 与选定版本一致的 Git 证据

## 选择来源与路径

`facts` 的来源由任务决定，不由文件当前是否存在决定。目录与简称先从改动列表展开成精确路径；重命名只传新路径，脚本通过 `old_path` 读取旧侧。执行提交时的范围仍包含新旧路径，见 [commit.md](commit.md)。

| 来源 | 比较 | 适用任务 |
|---|---|---|
| `--source index` | HEAD → index | 已暂存内容，保留部分暂存选择；首次提交以空树为基准 |
| `--source worktree`（默认） | HEAD → 整个文件的工作区版本 | 用户选择完整文件版本，含未跟踪文件；不是 index → worktree |

```text
python "<skill-dir>/scripts/collect.py" --repo "<repo>" facts --source index -- "<path>"
python "<skill-dir>/scripts/collect.py" --repo "<repo>" facts --source worktree -- "<path>"
```

同一来源下没有改动的路径不应冒充已完成变更。只写说明与执行提交使用相同的证据要求，但前者不修改索引。

暂存删除后重建、或已暂存重命名后恢复旧路径时，工作区版本可能与索引版本相反。`facts` 会按工作区实际存在的路径分别核对；不能直接沿用索引中的删除或重命名结论。

## 输出粒度与预算

小文本给改前/改后全文，大文本给 diff；新增文件默认可能只显示前 400 行。需要更多内容时，在 `--` 前增加 `--context 30` 或 `--max-lines 2000`，并保持来源不变。

- `--max-lines` 默认 400，只决定全文、截断全文或 diff 的粒度。
- `--max-bytes` 默认 200000，对每份载荷限制 UTF-8 字节数；超限整份省略并提示，不靠提高行数阈值绕过。
- `--max-total-bytes` 默认 200000，限制一次 facts 的全部输出，包括标题与提示。累计预算不足时会截断并标记；多文件分析应按需分批。

看到省略或截断提示时，按选定版本单独查看尚未覆盖的内容，不把片段描述成完整审查。压缩单行文件同样受字节限制。

二进制、子模块、符号链接和模式变化以 Git 元数据为依据。可用 `git --literal-pathspecs ls-files --stage -- <paths...>` 核对索引模式，结合所选来源的 raw diff 或普通 diff 确认变化；不沿符号链接读取目标文件。

## 原生 Git 降级

Python 不可用时继续使用 Git，不自动安装解释器或依赖。先用 `git status --porcelain=v1 -z -uall --renames` 区分索引状态 X 与工作区状态 Y，并以 NUL 分隔处理路径；重命名记录为新路径后跟旧路径。Git 自身失败时说明错误，不能当作没有改动。

所有含文件路径的 Git 命令使用独立参数和 `git --literal-pathspecs`：

```text
git --literal-pathspecs diff --cached --no-ext-diff --no-textconv -- "<path>"
git --literal-pathspecs diff --no-ext-diff --no-textconv HEAD -- "<path>"
git --literal-pathspecs diff --no-ext-diff --no-textconv -- "<path>"
```

三条分别用于 HEAD → index、HEAD → worktree、index → worktree；重命名 diff 的路径包含旧、新两侧。必要时读取 `git --literal-pathspecs show :<path>` 的索引内容或 `git --literal-pathspecs show HEAD:<path>` 的原内容，每个 revision/path 表达式作为单独参数；这类表达式是 revision 参数，不移到 `--` 后。

索引已删除而工作区又存在的路径，Git 会视为未跟踪，`diff HEAD` 可能仍只报告删除。此时将 HEAD 中的原内容、模式与当前工作区文件单独对照；重命名的旧路径若被恢复，也核对旧侧，避免把已撤销的改动写入说明。

首次提交没有 HEAD：`diff --cached` 仍可分析索引；完整工作区版本应根据状态读取对应普通文本文件，并核对索引模式和工作区差异。未跟踪文件不会出现在普通 Git diff 中，需要按选定范围单独读取。原生输出也可能很大，限制展示量并明确哪些内容尚未检查。
