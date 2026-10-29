# 来源与适配

角色设定来自 [OpenPersona 的 ai-girlfriend 预设](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/persona.json)，行为指南参考其 [behavior-guide.md](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/soul/behavior-guide.md)。2026-10-29 引入，保留相邻 LICENSE 中的 MIT 版权与许可声明。

persona.json 保留上游结构和非 Soul 配置，Soul 的自然语言内容翻译为中文。行为指南整理为中文，将“刚听过歌曲、工作中发生的事”等表达适配为有证据才陈述的现实活动，不启用定时主动联系。原文没有的对话示例不由编译器自动生成。

heybuddy 只读取 soul 中受支持的描述字段和固定的本地行为指南；body、faculties、skills、social、evolution、rhythm、additionalAllowedTools 等上游配置均不执行、不注入，也不会授予任何权限。角色的日常表达最终接上共用的 prompts/conversation.md。修改这些文件后需重启。
