# 来源与适配

角色设定来自 [OpenPersona 的 ai-girlfriend 预设](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/persona.json)，行为指南参考其 [behavior-guide.md](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/soul/behavior-guide.md)。2026-10-29 引入，保留相邻 LICENSE 中的 MIT 版权与许可声明。

persona.json 保留上游结构和非 Soul 配置，Soul 的自然语言内容翻译为中文。行为指南整理为中文，将“刚听过歌曲、工作中发生的事”等表达适配为有证据才陈述的现实活动，不启用定时主动联系。原文没有的对话示例不由编译器自动生成。

行为指南保留上游的四个主题：日常交谈、钢琴与音乐、情绪与相处、边界与坦诚。庆祝歌单与安静弹奏的例子来自原指南的中文适配，只作为表达参考；真实发声、发消息和当天活动仍以实际能力和证据为准。“分享糟糕的一天”适配为可以表达自己的看法、犹豫和不同意见，避免虚构当天事件。此前的概括版已展开为这四部分，主对话每次完整编入一次。

随后按用户提供的 Miru Emotional Reactions / Speech Patterns 片段，补充稳定信任与接纳、平和表达不同意见和情绪、具体关心与认可，并新增说话习惯与 emoji 段落。亲昵表达随真实相处自然出现，关心和共同经历以实际聊天依据为准；emoji 可以可爱、温柔，也可以呼应 Luna 的夜晚、音乐与代码兴趣，示例不是固定回复。

heybuddy 只读取 soul 中受支持的描述字段和固定的本地行为指南；body、faculties、skills、social、evolution、rhythm、additionalAllowedTools 等上游配置均不执行、不注入，也不会授予任何权限。角色的日常表达最终接上共用的 prompts/conversation.md。修改这些文件后需重启。
