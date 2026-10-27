# commit-msg

根据 Git 改动与当前对话生成准确的提交说明；明确要求提交时，执行范围清楚的本地提交。

## 安装与使用

1. 使用支持 Agent Skills 的 AI 客户端，并确保目标项目是 Git 仓库。
2. 将收到的整个文件夹命名为 `commit-msg`，放进该客户端的技能目录，保留内部相对路径。
3. 在目标项目的对话中说：`用 commit-msg 为已暂存的改动写提交说明`。

默认只生成说明，不暂存、不提交、不推送。需要执行提交时，明确说“把这些改动提交”。

必须能运行 Git。辅助脚本使用 Python 3.10+ 和标准库，无第三方依赖；没有 Python 时，助手仍可使用原生 Git 完成任务。无须配置本技能专用的 API 密钥、原作者路径或对话记录。

## 分享文件夹

分享时只需保留以下运行文件，不需要 Git 历史、测试文件、缓存或开发工具：

```text
commit-msg/
├── SKILL.md
├── references/
│   ├── commit.md
│   ├── facts.md
│   └── collection.md
└── scripts/
    ├── collect.py
    └── transcripts.py
```

当前对话和 Git 改动足以生成说明。历史检索是可选功能，默认只读取使用者本机的 Claude Code 记录；其他客户端不需要接入历史记录也能正常使用。
