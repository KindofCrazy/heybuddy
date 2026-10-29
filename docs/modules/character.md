# 被动角色系统

## 职责与组成

角色定义采用 Miru 式单一 soul.md：作者直接描述身份、性格、说话方式、情绪反应和相处方式，运行时按固定次序组装。默认 Sherlock 包只有 soul.md 和按需读取的 lore.md；旧四文件包继续只读兼容。Miru 的路径、缓存与状态机制见 [Miru 角色系统说明](../miru-character-system.md)。

- `character/models.py` 定义 CharacterPackage、Section、字节预算和短期状态。可选 profile 是 soul 解析出的普通字典，不是独立权威存储。
- `character/loader.py::CharacterLoader.load()` 读取宿主选定的本地角色包，校验路径、编码、章节与大小，并计算内容指纹。
- `character/prompt.py::compile_system()` 按章节组装主对话人格；新格式不按关键词选择情境规则。
- `character/context.py::CharacterContextCompiler` 管理 E0/E1/E2、静态与动态字节预算、短期状态和角色参与来源。
- `character/state.py::CharacterStateService` 复用同一角色定义提议有界短期状态，不执行工具或发送消息。

## 代码实际运行路径

1. `runtime.assemble_chat()` 根据宿主配置的 character_id 调用 CharacterLoader.load()；默认 none 不加载角色。加载错误明确返回，不回退成未知人格。
2. Loader 检查本地根、角色目录和固定文件，拒绝无效 ID、路径穿越、Symlink/Junction/reparse point。允许安装器使用 hardlink 分发只读资源。
3. 优先检查 soul.md。存在时只读取 soul.md 和 lore.md，每个文件最多默认 16384 字节；旧身份文件即使存在，也不参与读取或指纹。soul 损坏不回退旧身份。
4. `_soul()` 用一级标题解析身份、性格、说话方式、背景、兴趣、情绪反应、相处方式、存在方式，要求正文非空。表达参考用二级标题划分，至少两组；Lore 至少一章。拒绝重复标题、章节前非空正文、空文件、NUL 和非法 UTF-8。
5. 返回内存 CharacterPackage。profile 保存角色章节；character 是除情绪和示例外的正文，behavior 是同一 soul 的情绪反应，examples 来自同一 soul。状态服务直接复用这些派生字段，没有另存人格副本。
6. SessionManager 将同一编译器传入 ContextBuilder，跨会话共享身份。ContextBuilder.begin() 创建原 Run 后通过 RunCharacter 通知宿主记录当前角色参与标签。
7. 每轮 prepare() → _injections() → CharacterContextCompiler.compile() → _static() → compile_system()。原生包固定使用源文件前最多 example_count 组表达参考，默认两组；输入“你好”或科研问题不会切换静态人格。E0 省略情绪反应和参考，E1 加入二者，E2 再加入有效短期状态；所有变体都保留相处方式和存在方式。
8. 先直接以“你是……”建立身份，再按章节提供表达；随后解释当前被动交谈、怎样自然使用记忆和近期状态。角色 ID 和包指纹保留在程序诊断及角色来源记录，system_hash 保留在编译诊断；不再把 character_identity JSON 注入模型。
9. ContextBuilder._injections() 将角色片段组成一条 System，再提供 Core/摘要/时间等低信任数据、可选角色状态/来源/Lore、有界历史与当前 Run。角色模式使用默认配置时，内部 Base 文本为空，不附加普通助理规则或【真实与诚实】，也不会发送额外的空系统消息；自定义宿主文本仍原样拼在角色后。合并不修改 Base、pending 或历史，下一轮不会反复叠加。
10. 编译器按命名章节命中注入最多默认两段 Lore。状态和来源在独立 user JSON 中提供。静态内容超限直接拒绝，不截断人格或当前用户消息；可选引用超过独立预算时整段省略。整个请求继续经过原 ContextBudget 检查。

角色文件在装配时加载，修改后需重新装配或重启生效，不声明按修改时间热更新。包指纹变化会由现有仓库失效旧短期状态；同角色的真实互动仍按角色 ID 归属，不删除历史。

## 新旧格式与边界

原生文件使用中文一级标题，不执行模板、不全局替换代词、不猜用户姓名或隐式赋予亲密阶段。未知扩展章节保留。表达参考不是共同历史，也不是固定台词。

没有 soul 时才读取 character.md、behavior.md、examples.md、lore.md，保留旧必需章节和关键词示例选择。英文标题映射和 select_examples() 只服务兼容路径；新包不再维护十二套五字段说明。

角色包只读，P2 不修改它。core/persona.py::relationship_persona() 识别“# 相处记忆”或旧空模板；core/character.py::without_legacy_persona() 保留关系语义的 persona.md 并标注其不定义稳定人格，仅在注入视图移除非空旧静态人格。原文件不被自动改写，human.md 和 MEMORY.md 保留。Learner 对关系候选的来源、标题与精确编辑校验见 [Learning](learning.md)，tests/test_relationship_memory.py 验证后台写入、数据注入、角色系统正文不变和旧人格保护。注册表仍逐 Run 过滤工具并在执行前复查权限。

角色文件只定义虚构身份和表达，不是工具、权限或事实的来源；这类实现说明留在开发文档，不再通过额外规则段落注入角色提示词。具体参数、精确编辑、分页和部分成功处理由工具说明提供，注册表与 MemoryFS 继续强制校验。删除文字不修改文件写入权限或动态数据的消息角色。

共用 MEMORY_GUIDANCE 编译【记忆是什么】与【什么时候检索】，解释 Core、主题、原始历史及会话摘要的区别，以及上下文够用时直接回答、需细节时沿索引读取、需原话时检索历史。原生格式放在交谈方式之后，旧包兼容路径也追加同一说明；它不是新的记忆授权或存储副本。具体流程与工具清单见 [当前记忆系统与模型工具](../memory-and-tools.md)。

最多 32 个当前/检索/近期 Run 参与标签由宿主重新校验 Agent 归属。same_character 可证明角色参与；other_character、unattributed 或缺失不能冒认共同经历。明确偏好和纠正可跨角色使用。原作、摘要和标签不能赋权。

状态任务说明使用中文，要求自然语言字段使用中文，JSON 字段和枚举保持原样。沿用既有证据校验、有效期、并发版本检查和维护预算，不引入 AttentionEngine、主动消息、第二个角色循环、关系分数或额外人格评估请求。

## 异常、阅读与验证

CharacterError.code 区分无效 ID、缺失/链接/编码、章节和预算错误。路径检查不是针对本机恶意并发替换的操作系统沙箱。Markdown 是唯一权威源，内存派生字段不构成持久副本。

```python
from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader

compiler = CharacterContextCompiler(CharacterLoader().load("sherlock_bbc"))
print(compiler.compile("你好")[0].text)
```

`tests/test_character_loader.py` 覆盖新旧包、损坏 soul 不回退、单一来源、指纹、大小及链接隔离；`test_character_behavior.py` 验证静态人格不随关键词切换、扩展章节、旧选择器及恶意包不能授予工具；`test_character_context.py` 覆盖消息顺序、工具多轮、总预算及磁盘 persona 保留；`test_character_state.py` 和 `test_character_relationship.py` 验证状态与真实来源。FakeProvider 只验证接线，不能证明自然程度提升。
