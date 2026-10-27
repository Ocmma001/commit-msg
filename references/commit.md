# 执行本地提交

## 提交前检查

仅实际提交或 amend 需要检查 `git var GIT_AUTHOR_IDENT` 和 `git var GIT_COMMITTER_IDENT`；缺失时索取真实身份，授权配置后默认使用仓库级设置，不改全局配置。游离 HEAD 时说明新提交不属于分支。

检查冲突及进行中的 merge、rebase、cherry-pick、revert。用 `git rev-parse --git-path <name>` 定位 `MERGE_HEAD`、`rebase-merge`、`rebase-apply`、`CHERRY_PICK_HEAD`、`REVERT_HEAD` 等状态，不硬编码 `.git/...`。常规提交不自行继续或中止这些操作；用户明确要求完成它们时遵循相应任务。

## 范围与暂存区

先记录 HEAD（首次提交记为无 HEAD）、`git status --porcelain=v1 -z -uall --renames` 和 `git diff --cached --name-status -z`，明确提交的是索引还是整个工作区文件。所有带文件路径的 Git 操作统一使用 `git --literal-pathspecs ... -- <paths...>`，路径使用独立参数，避免文件名中的匹配字符或 `:(...)` 被当作 pathspec。

区分两组精确路径：`scope-paths` 是完整选定范围，重命名包含旧路径的删除和新路径的新增；`add-paths` 只包含范围内仍存在于索引或工作区的路径。旧路径若已被暂存删除，不再向 add 传入它；新路径需要暂存时仍须精确 add。工作区已删除但仍在索引的路径要保留在 `add-paths`，才能暂存删除。暂存后复核新旧两侧是否完整。

| 情况 | 方法 |
|---|---|
| 提交当前暂存区 | 不执行 add；检查完整 cached diff，然后 `git --literal-pathspecs commit -F <message-file>` |
| 提交整个文件，暂存区无范围外内容 | `git --literal-pathspecs add -A -- <add-paths...>`，检查完整 cached diff，再普通 commit；`add-paths` 为空时跳过 add |
| 提交整个文件，暂存区还有范围外内容 | 精确 add 后，`git --literal-pathspecs commit --only -F <message-file> -- <scope-paths...>`，保留其他暂存内容；此模式取所选路径的工作区版本，不能用于保留其部分暂存 |
| 仅提交暂存区子集，其他暂存内容须保留 | 不用 `--only`（它取工作区内容）。说明索引范围，让用户明确是否提交完整暂存区；不自行 reset、stash 或改变其他暂存选择 |

首次提交同样核对所有暂存内容。工作区有变化不保证最终提交非空，例如暂存后又恢复了原内容；新提交的最终 diff 为空时报告无可提交内容，不自动创建空提交。

检查阶段与提交之间若文件或索引发生变化，重新查看最终待提交内容；不要沿用旧 message 宣称新的变更已验证。

## 执行与失败

用可靠的文件写入接口生成 UTF-8 message 临时文件，保留真实换行。不把 message 拼进 shell 命令，不改变换行、签名或 hook 配置。调用 `git --literal-pathspecs commit -F <message-file>`，按上表决定是否加 `--only` 与路径。

失败时报告具体输出（隐藏凭据），检查 HEAD 和状态后再判断是否确实没有提交。hook 可能已修改文件：保留这些变化，说明情况，不自动重复暂存或加 `--no-verify`。若用户已授权修复失败原因，可修复后重新核对内容再重试；不无限重试或为通过 hook 删除规则。

成功后检查 `git log -1 --format=%h%n%B`、`git diff-tree --root --no-commit-id --name-status -r HEAD` 和状态，核实实际提交及剩余改动。明确授权生成 merge commit 时按其父提交检查范围，不能把默认 diff-tree 的空输出当作没有提交内容。若范围不符，立即说明，不自动 reset 或 amend。完成或失败后清理自己创建的临时 message 文件。

## 返回结果

报告短 SHA 和标题，必要时说明剩余改动。本地 commit 不等于 push，不默认推送。

有上游时可用 `git rev-list --left-right --count HEAD...@{upstream}` 报告领先/落后数量；在 PowerShell 中引用含 `@{...}` 的参数。比较基于本地远端跟踪引用，未 fetch 时不能宣称是远端实时状态。无上游时说明“已本地提交，未配置上游”，不把有 remote 等同于有 upstream。

## 用户要求修改已有 message

此分支从入口任务分类直接进入，工作区干净也可执行。只有明确要求修改已有说明时才 amend；仅要求草拟候选时输出说明即可。

先确认 HEAD 存在且是用户指定的目标，用 `git log -1 --format=%H%n%B` 读取原说明，检查 `git show --format=fuller --stat HEAD` 及该提交的实际 diff。首次提交用相对空树的 diff；目标为 merge commit 时检查相应父提交的差异。新说明描述的是这个提交，不能根据无关 index 或 worktree 改动补充事实。指定更早的提交时不把 amend HEAD 当作替代操作，按用户已有授权处理，缺少历史修改范围的决定时再询问。

记录旧 HEAD、旧 tree、暂存区及工作区状态，按已有授权用 `git --literal-pathspecs commit --amend --only -F <message-file>` 仅改 message，不执行 add、不传文件路径。完成后核对新说明、tree 与旧提交一致，并检查原有暂存和工作区改动仍保留；hook 改动或异常按失败处理规则报告。

本地远端引用不包含该提交，或没有 remote，都不足以证明提交从未分享。若用户说明已推送或共享，解释重写历史的影响后按已有授权处理；若是否共享影响决定且信息不足，再询问。不自动 force push。
