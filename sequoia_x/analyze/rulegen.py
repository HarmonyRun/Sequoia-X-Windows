"""自然语言 → 选股规则：让大模型把中文描述翻译成 DSL JSON。

工作方式
--------
1. 把 DSL 的完整规格（指标白名单、操作符、示例）写进 system prompt
2. 让模型输出**纯 JSON**，不要解释文字
3. 拿到结果后**本地校验**（`evaluator.validate`）——模型的输出不可信
4. 校验失败就把错误信息回灌给模型，让它自己改，最多重试 N 次

为什么必须本地校验 + 重试
-------------------------
实测大模型经常犯这些错：
- 编造指标名（`avg_price`、`turnover_rate` 这些不存在的）
- 把 `{"ind":"const","value":1.5}` 写成 `{"const":1.5}`
- 该用 `cross_above` 的地方用 `>`（语义完全不同）
- 把 period 写成 500（超出 250 上限）

所以「解析失败 → 把错误喂回去 → 重试」是必需的，而不是可选的优化。
最多重试 2 次（加上首次共 3 次调用），避免死循环烧 token。
"""

from __future__ import annotations

import json
import re

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.custom.evaluator import RuleError, validate

logger = get_logger(__name__)

MAX_ATTEMPTS = 3
# 生成规则用低温度，减少胡编
TEMPERATURE = 0.2

# 指标清单按类别组织，写进提示词让模型知道有什么可用
_INDICATOR_DOC = """
【价格与成交】
  open / high / low / close      当日开高低收
  volume                         当日成交量
  amount                         当日成交额
  pct_chg                        当日涨跌幅（%）

【均线类】都支持 period 参数（默认 20）
  ma          收盘价 N 日均线
  ema         收盘价 N 日指数均线
  vol_ma      成交量 N 日均值

【技术指标】
  macd_dif / macd_dea           参数 fast(12) slow(26) signal(9)
  rsi                           参数 period(14)，0~100
  boll_upper / boll_lower / boll_mid   参数 period(20) std(2)
  boll_width                    布林带宽 (上轨-下轨)/中轨
  atr                           真实波幅，参数 period(14)

【区间高低点】参数 period
  highest      N 日内最高价
  lowest       N 日内最低价
  hhv_close    N 日内收盘最高
  llv_close    N 日内收盘最低

【形态与统计】
  consecutive_up / consecutive_down   连涨/连跌天数
  vol_ratio      当日量 / N 日均量（参数 period，默认 20）
  pct_chg_nd     最近 N 日累计涨跌幅（%，参数 period）
  drawdown_from_high  距 N 日最高价的回撤（%，负值）
  gap_up         向上跳空幅度（%），未跳空为 0
  is_yang / is_yin    1=阳线 / 1=阴线
  close_position 收盘在当日振幅中的位置（0~1，越大越靠上）

【截面排名】
  rank_pct       全市场百分位排名（0~100），参数 by=<指标名>

【常量】
  必须写成 {"ind": "const", "value": 1.5}
"""

_OPERATOR_DOC = """
比较符：
  >  <  >=  <=          数值比较
  cross_above           左值**上穿**右值（昨日左<=右 且 今日左>右）——「金叉」用这个
  cross_below           左值**下穿**右值
  between               左值在 [右值1, 右值2] 之间
  rising / falling      左值连续 N 天递增/递减（用 period 指定天数，默认 2）
                        注意：这是一元操作符，**不要写 right**
  above_for / below_for 左值连续 N 天大于/小于右值

逻辑组合：
  {"all": [条件1, 条件2]}    全部满足
  {"any": [条件1, 条件2]}    任一满足
  {"not": 条件}             取反
"""

_EXAMPLE = """
示例输入：找连续三天缩量回调、今天放量收阳的票
示例输出：
{
  "name": "缩量回调放量收阳",
  "description": "连续三天缩量回调后放量收阳",
  "conditions": {"all": [
    {"left": "close", "op": ">", "right": "open"},
    {"left": "vol_ratio", "op": ">", "right": {"ind": "const", "value": 1.5}},
    {"left": "pct_chg", "op": "between", "right": [{"ind": "const", "value": 0}, {"ind": "const", "value": 6}]},
    {"left": "close", "op": ">", "right": {"ind": "ma", "period": 60}}
  ]}
}

示例输入：创业板里 RSI 超卖后开始回升的
示例输出：
{
  "name": "创业板RSI超卖反弹",
  "universe": {"boards": ["创业板"]},
  "conditions": {"all": [
    {"left": {"ind": "rsi", "period": 14}, "op": "<", "right": {"ind": "const", "value": 35}},
    {"left": {"ind": "rsi", "period": 14}, "op": "rising", "period": 2},
    {"left": "close", "op": ">", "right": "open"}
  ]}
}

示例输入：5日线上穿20日线并且放量
示例输出：
{
  "name": "均线金叉放量",
  "conditions": {"all": [
    {"left": {"ind": "ma", "period": 5}, "op": "cross_above", "right": {"ind": "ma", "period": 20}},
    {"left": "volume", "op": ">", "right": {"ind": "vol_ma", "period": 20}}
  ]}
}
"""

SYSTEM_PROMPT = f"""你是一个量化选股规则翻译器。用户会用中文描述选股想法，
你要把它翻译成一个 JSON 规则对象。**只输出 JSON，不要任何解释文字、不要 markdown 代码块**。

## 输出格式

{{
  "name": "策略名（中文，不超过 20 字）",
  "description": "一句话说明这个策略在找什么",
  "universe": {{ "boards": ["创业板"], "exclude_st": true }},
  "conditions": <条件树>
}}

`universe` 可选。不写就是全市场。可用板块：沪市主板、深市主板、创业板、科创板、深市中小板、北交所

## 条件树

叶子条件：{{"left": <操作数>, "op": "<比较符>", "right": <操作数>}}
操作数：指标名（字符串）、{{"ind": "指标名", ...参数}}、或常量 {{"ind": "const", "value": 1.5}}

## 可用指标

{_INDICATOR_DOC}

## 可用操作符

{_OPERATOR_DOC}

## 示例

{_EXAMPLE}

## 硬性要求

1. **只能使用上面列出的指标名和操作符**，不要发明新的
2. 常量一律写成 {{"ind": "const", "value": 数字}}
3. 「金叉/上穿」要用 `cross_above`，不要用 `>`
4. period 类参数必须在 1~250 之间
5. 条件总数不超过 20 个，嵌套不超过 5 层
6. **如果用户的要求用现有指标无法表达，也仍然返回 JSON**，把 name 设为
   "无法实现"、description 里说明缺什么能力，conditions 里放一个最简单的条件。
   不要编造不存在的指标。
"""


class GenerateError(RuntimeError):
    """生成失败。消息可直接展示给用户。"""


def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象。

    模型经常不守规矩，外面套 ```json 代码块、或者前后带一句「好的，这是规则：」。
    所以不能直接 json.loads，要先做容错提取。
    """
    raw = (text or "").strip()

    # 去掉 markdown 代码块围栏
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    # 直接解析
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass

    # 退而求其次：截取第一个 { 到最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidate = raw[start:end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass

    raise GenerateError(
        f"大模型没有返回合法的 JSON。原始输出前 200 字：{raw[:200]}"
    )


def generate_rule(
    settings: Settings,
    description: str,
    engine=None,
) -> dict:
    """把中文描述翻译成规则。

    Args:
        settings: 配置（要用其中的大模型设置）。
        description: 用户的中文描述。
        engine: 可选。传入时会在生成后**试跑几只股票**，确认规则真的能跑通
            （有些规则能通过校验但算出全是 NaN）。

    Returns:
        ``{"rule": <规则字典>, "warnings": [...], "attempts": 次数,
           "dry_run": {...} | None}``

    Raises:
        GenerateError: 模型没配好、调用失败，或重试耗尽仍无法产出合法规则。
    """
    from sequoia_x.analyze.llm import LLMError, chat, is_configured

    if not is_configured(settings):
        raise GenerateError(
            "还没有配置大模型。请到「设置 → 大模型分析」填好接口地址、模型名和 API Key。"
        )

    text = (description or "").strip()
    if not text:
        raise GenerateError("请先用中文描述你想要什么样的股票")
    if len(text) > 500:
        raise GenerateError("描述太长了（超过 500 字），请精简一下")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info(f"生成规则：第 {attempt}/{MAX_ATTEMPTS} 次调用大模型")
        try:
            reply = chat(settings, messages, timeout=120)
        except LLMError as exc:
            raise GenerateError(f"调用大模型失败：{exc}") from exc

        try:
            rule = _extract_json(reply)
        except GenerateError as exc:
            last_error = str(exc)
            # 把解析失败也回灌，让模型下次输出干净的 JSON
            messages.append({"role": "assistant", "content": reply[:500]})
            messages.append({
                "role": "user",
                "content": f"你的输出不是合法 JSON：{exc}\n"
                           f"请只输出 JSON 对象本身，不要 markdown 代码块、不要解释文字。",
            })
            continue

        # 本地校验——模型的输出不可信
        try:
            warnings = validate(rule)
        except RuleError as exc:
            last_error = str(exc)
            logger.warning(f"第 {attempt} 次生成的规则校验失败：{exc}")
            messages.append({"role": "assistant", "content": json.dumps(rule, ensure_ascii=False)})
            messages.append({
                "role": "user",
                "content": f"这个规则校验失败了：{exc}\n"
                           f"请修正后重新输出完整的 JSON。注意只能用上面列出的指标名和操作符。",
            })
            continue

        # 通过校验就试跑一下，确认真的能算（有些规则语法对但算出全 NaN）
        dry = None
        if engine is not None:
            dry = _dry_run(engine, rule)
            if dry["checked"] > 0 and dry["hits"] == 0 and dry["nan"] == dry["checked"]:
                # 所有股票都算不出值，大概率是引用了一个在本地数据里
                # 永远为 NaN 的组合。这不一定是错，但值得提醒。
                warnings = list(warnings) + [
                    f"试跑 {dry['checked']} 只股票全部算不出结果，"
                    f"可能是条件过严或引用了不常用的指标"
                ]

        logger.info(f"规则生成成功（第 {attempt} 次尝试）：{rule.get('name')}")
        return {
            "rule": rule,
            "warnings": warnings,
            "attempts": attempt,
            "dry_run": dry,
        }

    raise GenerateError(
        f"连续 {MAX_ATTEMPTS} 次都没能生成合法规则。最后一次的错误：{last_error}"
    )


def _dry_run(engine, rule: dict, sample: int = 120) -> dict:
    """抽样试跑，返回命中数 / 算不出值的数量。

    用途：给用户一个「这条规则大概会选出多少只」的预期，
    并发现「语法正确但实际算不出东西」的规则。
    """
    from sequoia_x.strategy.custom.evaluator import evaluate

    try:
        symbols = engine.get_local_symbols()[:sample]
    except Exception:  # noqa: BLE001
        return {"checked": 0, "hits": 0, "errors": 0, "nan": 0, "samples": []}

    hits, errors, nan = [], 0, 0
    for sym in symbols:
        try:
            df = engine.get_ohlcv(sym)
            if df is None or len(df) < 2:
                nan += 1
                continue
            if evaluate(rule, df):
                hits.append(sym)
        except Exception:  # noqa: BLE001
            errors += 1

    return {
        "checked": len(symbols),
        "hits": len(hits),
        "errors": errors,
        "nan": nan,
        "samples": hits[:8],
    }
