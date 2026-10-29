# OpenPersona 提示词组装与 heybuddy 适配

## 上游实际做了什么

OpenPersona 的 persona.json 是声明文件，不是一份原样发给模型的系统提示词。`lib/generator/index.js` 的流水线先 clone、validate，再 load、derived、prepare、render、emit。验证后 normalizeSoulInput() 把 soul.identity/aesthetic/character 的字段展开给模板；loadPhase() 相对声明文件读取 behaviorGuide 的 file 引用。生成器不在这条路径中调用模型重写指南。[生成器源码](https://github.com/acnlabs/OpenPersona/blob/main/lib/generator/index.js)

它生成两份用途不同的内容：

| 输出 | 主要内容 | 行为指南的位置 |
| --- | --- | --- |
| soul/injection.md | 身份、背景、表达、边界、性格，再加自我认知、能力与按配置启用的成长等段落 | 当前模板不直接展开 behaviorGuide |
| SKILL.md | Soul/Body/Faculty/Skill 的技能包入口 | buildSkillContent() 在身份、背景及可选能力之后放入完整指南，再追加性格、表达、氛围与边界 |

前者由 Mustache 渲染 soul-injection.template.md 及 partials；后者渲染 skill.template.md，其 Soul 部分接收 skillContent。file 引用的指南还会复制到输出目录的 soul/behavior-guide.md；内联指南也会外置为这个文件。它既是独立维护资源，也会展开进技能包正文。[身份模板](https://github.com/acnlabs/OpenPersona/blob/main/templates/soul/partials/soul-intro.partial.md)、[注入模板](https://github.com/acnlabs/OpenPersona/blob/main/templates/soul/soul-injection.template.md)、[技能包模板](https://github.com/acnlabs/OpenPersona/blob/main/templates/skill.template.md)

默认 OpenClaw 安装与切换路径读取 soul/injection.md，更新工作区 SOUL.md 中的 OpenPersona 标记块；SKILL.md 作为技能包入口提供给兼容宿主。因此不能由“指南生成进 SKILL.md”推断所有宿主的每次模型请求都自动包含完整指南，实际可见上下文还取决于宿主加载方式。[安装器](https://github.com/acnlabs/OpenPersona/blob/main/lib/lifecycle/installer.js)、[切换器](https://github.com/acnlabs/OpenPersona/blob/main/lib/lifecycle/switcher.js)

## Luna 指南为什么有用

ai-girlfriend 预设的指南用四个主题说明具体反应：自然接话和分享、钢琴与音乐、留意情绪并接受沉默、相处边界与坦诚。它补充的是“遇到这种情况怎样接话”，而不只是重复“温柔、自然、有同理心”。其中也有庆祝音乐、希望弹奏安静音乐等简短表达参考。[原指南](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/soul/behavior-guide.md)

上一版 heybuddy 已正确读取引用并把指南编入 System，并没有漏加载；问题是本地改写过于概括，具体主题和例子少了。同时编译器把年龄、类型、表情等描述逐项展示，像属性清单。这里的改进判断来自结构对照，尚不等于自然度实测结论。

## 当前采用的组装

```text
System
  身份、简介、相处定位与背景
  性格与表达（合并氛围和表情）
  相处边界
  完整行为指南（四部分，只展开一次）
  既有记忆与工具说明
  通用交谈提示词（保留用户确认的原文）

独立的 user 数据消息
  Core Memory、时间、摘要

保留原消息角色
  历史与当前 Run（含 user、assistant、tool）
```

年龄和类型并入身份句，其余描述按用途组织。Luna 的指南恢复四个主题与原文的少量表达参考，并将虚构当天工作、真实弹奏、主动发消息等内容适配为 heybuddy 实际支持的表达。具体文本由 persona.json 与本地指南维护，编译器不自动生成角色反应，也不根据用户关键词切换规则。

heybuddy 明确将完整指南编入主对话一次，不依赖模型自行读取技能文件；无需同时注入上游 SKILL.md 和 injection.md，避免重复人格及不适用的宿主指令。通用闲聊规则继续放在末尾作为默认表达方式，任务需要与用户明确格式要求仍优先。既有记忆与工具说明保持原文，角色的状态写入、能力启用与主动聊天不在本次适配范围。

对应实现和验证见 [简化角色模块](modules/persona.md)。组装本身不发起模型请求，也不新增依赖或运行时状态；离线测试验证完整注入、顺序、单一 System 与预算，不能证明实际对话一定更自然。单独授权的真实 API 对比记录保存在忽略目录，不随角色设定提交。
