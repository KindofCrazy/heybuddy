# Issue tracker: GitHub

HeyBuddy 的规格和任务保存在 `KindofCrazy/heybuddy` 的 GitHub Issues。技能要求“发布到 issue tracker”时创建 Issue，要求“读取 ticket”时读取对应 Issue。

优先使用已连接的 GitHub 工具；缺少所需操作时使用 gh。CLI 操作显式指定 `--repo KindofCrazy/heybuddy`。

## 常用操作

- 创建：`gh issue create --repo KindofCrazy/heybuddy --title "标题" --body-file <文件>`
- 读取：`gh issue view <编号> --repo KindofCrazy/heybuddy --json number,title,body,labels,comments`
- 列出：`gh issue list --repo KindofCrazy/heybuddy --state open --json number,title,body,labels`
- 标签：`gh issue edit <编号> --repo KindofCrazy/heybuddy --add-label <标签>`；移除使用 `--remove-label`。
- 评论：`gh issue comment <编号> --repo KindofCrazy/heybuddy --body-file <文件>`
- 关闭：`gh issue close <编号> --repo KindofCrazy/heybuddy`

多行正文写入临时文件再使用 --body-file。任务内容不得包含 API Key、私人会话、Core Memory 或原始 Provider 数据。

## 层级与依赖

拆分规格时使用 GitHub sub-issues；不可用时，在子任务正文顶部写 `Part of #<父编号>`，并在父 Issue 中维护任务列表。

阻塞关系使用 GitHub 原生 issue dependencies；不可用时，在正文顶部写 `Blocked by: #<编号>`。阻塞任务全部关闭后才视为可执行。

## Wayfinder

Map 是一个带 `wayfinder:map` 标签的 Issue，子任务按 research、prototype、grilling、task 类型标记。仅在实际使用这个工作流时创建相应标签。

从 Map 顺序中选择尚未关闭、无未解决阻塞且未被认领的子任务。开始前分配给执行者；结束时记录结论、关闭子任务，并更新 Map 中的决策及来源链接。

## PRs as a request surface

**PRs as a request surface: no.**
