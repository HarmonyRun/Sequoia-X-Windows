"""自定义策略模块：用受限规则 DSL 描述选股逻辑，由解释器执行。

为什么要做 DSL 而不是让大模型写代码
------------------------------------
用户可以用自然语言描述选股想法（「找连续三天缩量回调、今天放量收阳的票」），
大模型把它翻译成结构化的规则 JSON，再由这里的解释器执行。

**不让大模型直接生成 Python**：
1. 生成的代码能读写文件、发网络请求、改数据库
2. 引用不存在的模块/列名会让整条流水线挂掉
3. 用户看不懂代码，但能看懂「MA5 上穿 MA20 且 成交量 > 20日均量×1.5」
4. 生成的规则用户能直接改数字，不必重新问大模型

格式定义见 `docs/自定义策略DSL.md`。
"""

from sequoia_x.strategy.custom.store import (
    CustomStrategyStore,
    delete_rule,
    list_rules,
    load_rule,
    save_rule,
)

__all__ = [
    "CustomStrategyStore",
    "delete_rule",
    "list_rules",
    "load_rule",
    "save_rule",
]
