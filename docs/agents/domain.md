# Domain Docs

HeyBuddy 使用 single-context 布局：根目录 `GLOSSARY.md` 保存领域术语，`docs/adr/` 保存设计决策。

## 阅读规则

探索代码前，读取已存在的 GLOSSARY.md；修改某个领域前，读取 docs/adr/ 中相关决策。文件不存在时直接继续。

命名、Issue、测试和文档沿用术语表中的含义。发现术语缺口时，在实际领域建模过程中补充；与 ADR 冲突时明确指出冲突及重新讨论的理由。

## 维护规则

术语表和 ADR 由 domain-modeling 等技能在术语或决策形成时按需创建。本次 setup 只配置位置与阅读规则。

现有 `docs/architecture.md` 和 `docs/modules/` 继续描述实际架构及执行路径；ADR 记录选择理由，不替代模块说明。
