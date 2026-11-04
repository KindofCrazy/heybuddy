# Skills：按需读取的操作知识

## 职责与架构组成

`src/personal_agent/skills/loader.py::SkillLoader` 读取宿主技能。Harness 注入目录摘要并注册 skill_list、skill_load、skill_read_resource。Skill 表达操作方法，Memory 表达长期理解，Persona 表达角色；技能不授予工具权限。

## 机制流程

1. 宿主先安装经过选择的操作指南，程序读取名称与用途，形成一个有限目录。
2. 模型看到任务和目录后，按需要选择指南，再读取正文或同目录的补充资料。
3. 指南提供做事方法，但不直接执行动作；模型仍须通过已授权工具完成实际工作。
4. 读取结果作为参考资料进入后续交互。指南不能扩大权限、改写角色身份或绕过安全检查。

## 代码实际运行路径

1. Harness 使用 AGENT_SKILLS_ROOT 或用户数据目录 HeyBuddy/skills 创建 Loader，scan 扫描 */SKILL.md。
2. metadata 使用 YAML safe loader，拒绝别名等非法输入；scan 验证唯一名称、说明和文件限制，建立 catalog。
3. context 只返回名称与说明，不注入所有正文。
4. 模型通过 skill_load(name) 读取正文与 hash；结果经过普通 Registry、Journal、下一轮上下文路径。
5. skill_read_resource(name, path) 只读取同技能目录内的文本资源。真正行动仍要调用其他工具，经过权限、policy、预算和 Hook。

## 异常与边界

最多 100 项、单文件 32 KiB、说明最多 500 字符。资源只允许 md/txt/json，拒绝穿越、绝对路径和链接绕过。目录摘要受贡献器和窗口总预算限制。模型无安装、删除、修改技能接口，不执行附带脚本。hash 是内容标识，不是来源认证。

## 阅读与验证

宿主创建 skills/report/SKILL.md，含 name/description Frontmatter 与步骤；模型先读目录，再按需 load。`uv run pytest tests/test_harness.py -k skill`；外部工具协议另见 [MCP](skills-mcp.md)。

## 未来改进

- 检查扫描元数据和后来读取的正文版本一致性。
- 按相关性有界选择技能目录，减少固定上下文成本。
- 展示来源与版本，用任务效果评测技能，而非仅统计读取次数。
