"""规则 DSL 的解释器：把规则树编译成「每只股票是否命中」的判定函数。

设计要点
--------
1. **不做代码生成**。大模型只输出 JSON 规则，这里用受限的操作符白名单解释执行。
   生成代码的方案被否掉了：能读写文件、发网络请求，且引用不存在的列会让整条
   流水线挂掉。

2. **左值取当前值，右值也取当前值**。像 ``ma5 cross_above ma20`` 这类比较，
   只在**最后一根 K 线**上判断（而不是对整段序列做向量化 then取尾部）——
   因为 ``cross_above`` 需要「昨日」和「今日」两个点，逐点向量化算出来的
   是整段历史上的所有金叉，取尾部虽然结果一样，但中间量的语义容易混淆。
   这里显式取 ``iloc[-1]`` 和 ``iloc[-2]``，可读性优先。

3. **单只股票失败只跳过该股**，与项目既有约定一致（见 7 个原策略的写法）。

4. **市值条件降级**：本地没有市值数据，配了就跳过该条件并记警告，
   不让整个策略失败——schema 里预留了这个字段，等数据接进来直接生效。
"""

from __future__ import annotations

import math

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.custom.indicators import (
    ALL_INDICATORS,
    CROSS_SECTION_INDICATORS,
    IndicatorError,
    compute,
)

logger = get_logger(__name__)

# 比较符白名单。每个都对应一个明确的「在最后一根 K 线上怎么判」的语义。
OPERATORS = {
    ">", "<", ">=", "<=", "==", "!=",
    "cross_above", "cross_below",
    "between",
    "rising", "falling",
    "above_for", "below_for",
}

# 逻辑节点名
LOGIC_KEYS = {"all", "any", "not"}

# 校验上限（防止大模型生成病态结构把 CPU 打满）
MAX_DEPTH = 5
MAX_LEAVES = 20


class RuleError(ValueError):
    """规则结构非法。消息可直接展示给用户。"""


# ══════════════════════════════════════════════════════════════════
#  校验
# ══════════════════════════════════════════════════════════════════

def validate(rule: dict) -> list[str]:
    """校验规则，返回警告列表；结构非法则抛 RuleError。

    警告用于「能跑但有隐患」的情况，比如市值条件会被忽略。

    Args:
        rule: 完整的规则字典（含 name / conditions 等）。

    Returns:
        警告信息列表（可能为空）。

    Raises:
        RuleError: 结构非法，消息里说明具体原因。
    """
    if not isinstance(rule, dict):
        raise RuleError("规则必须是一个 JSON 对象")

    name = rule.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError("规则缺少 name（策略名）")
    if len(name.strip()) > 20:
        raise RuleError(f"策略名不能超过 20 个字，当前 {len(name.strip())} 个")

    cond = rule.get("conditions")
    if not isinstance(cond, dict) or not cond:
        raise RuleError("规则缺少 conditions（选股条件），或它是空的")

    depth = _check_node(cond, 1)
    if depth > MAX_DEPTH:
        raise RuleError(f"条件嵌套太深（{depth} 层），最多 {MAX_DEPTH} 层")

    leaves = _count_leaves(cond)
    if leaves > MAX_LEAVES:
        raise RuleError(f"条件太多（{leaves} 个），最多 {MAX_LEAVES} 个")

    warnings: list[str] = []

    # 股票池
    uni = rule.get("universe")
    if uni is not None:
        if not isinstance(uni, dict):
            raise RuleError("universe 必须是一个对象")
        boards = uni.get("boards")
        if boards is not None:
            if not isinstance(boards, list) or not all(isinstance(b, str) for b in boards):
                raise RuleError("universe.boards 必须是字符串数组")
            unknown = [b for b in boards if b not in _ALL_BOARDS]
            if unknown:
                raise RuleError(
                    f"universe.boards 里有未知板块：{', '.join(unknown)}。"
                    f"可用：{', '.join(_ALL_BOARDS)}"
                )
        if uni.get("market_cap"):
            # 预留字段：本地没有市值数据
            warnings.append(
                "市值条件已忽略：本地行情库没有市值数据"
                "（schema 已预留该字段，等接入市值表后会自动生效）"
            )

    # 截面排名
    rank = rule.get("rank")
    if rank is not None:
        if not isinstance(rank, dict):
            raise RuleError("rank 必须是一个对象")
        by = rank.get("by")
        if not isinstance(by, str) or not by:
            raise RuleError("rank.by 必须是非空的指标名")
        if by not in ALL_INDICATORS:
            raise RuleError(f"rank.by 是未知指标 {by!r}")
        top = rank.get("top")
        if not isinstance(top, int) or not (1 <= top <= 500):
            raise RuleError("rank.top 必须是 1~500 的整数")

    # 排序
    srt = rule.get("sort")
    if srt is not None:
        if not isinstance(srt, dict) or not isinstance(srt.get("key"), str):
            raise RuleError("sort 必须是形如 {\"key\": \"amount\"} 的对象")
        if srt["key"] not in ALL_INDICATORS:
            raise RuleError(f"sort.key 是未知指标 {srt['key']!r}")

    limit = rule.get("limit", 100)
    if not isinstance(limit, int) or not (1 <= limit <= 500):
        raise RuleError("limit 必须是 1~500 的整数")

    return warnings


_ALL_BOARDS = ("沪市主板", "深市主板", "创业板", "科创板", "深市中小板", "北交所")


# 一元操作符：只比较「左值自身的前后变化」，不需要 right 字段。
# 早期版本无条件要求 left/op/right 三个字段都在，导致 {"left":"close","op":"rising"}
# 这种完全合法的写法被判定为「缺少 right」，而求值层根本不会去读 right。
UNARY_OPERATORS = {"rising", "falling"}


def _check_node(node, depth: int) -> int:
    """递归检查条件树，返回最大深度。"""
    if not isinstance(node, dict):
        raise RuleError("条件必须是 JSON 对象")

    # 逻辑节点
    logic_hits = [k for k in LOGIC_KEYS if k in node]
    if logic_hits:
        if len(logic_hits) > 1:
            raise RuleError(f"一个条件里不能同时出现 {'、'.join(logic_hits)}")
        kind = logic_hits[0]
        if kind == "not":
            sub = node["not"]
            if not isinstance(sub, dict):
                raise RuleError("not 后面必须跟一个条件对象")
            return _check_node(sub, depth + 1)
        subs = node[kind]
        if not isinstance(subs, list) or not subs:
            raise RuleError(f"{kind} 后面必须是至少含一个条件的数组")
        return max(_check_node(s, depth + 1) for s in subs)

    # 叶子节点
    for field in ("left", "op"):
        if field not in node:
            raise RuleError(f"条件缺少 {field} 字段：{_brief(node)}")

    op = node["op"]
    if op not in OPERATORS:
        raise RuleError(
            f"不支持的操作符 {op!r}。可用：{', '.join(sorted(OPERATORS))}"
        )

    _check_operand(node["left"], "left")

    if op in UNARY_OPERATORS:
        # 一元操作符不读 right，所以也不该要求它存在
        if "right" in node:
            raise RuleError(f"{op} 是一元操作符，不应带 right 字段")
        period = node.get("period", 2)
        if isinstance(period, bool) or not isinstance(period, int) or not (1 <= period <= 250):
            raise RuleError(f"{op} 的 period 必须是 1~250 的整数，收到 {node.get('period')!r}")
        return depth

    if "right" not in node:
        raise RuleError(f"条件缺少 right 字段：{_brief(node)}")

    if op == "between":
        right = node["right"]
        if not isinstance(right, list) or len(right) != 2:
            raise RuleError("between 的 right 必须是含两个元素的数组，如 [0, 6]")
        _check_operand(right[0], "right[0]")
        _check_operand(right[1], "right[1]")
    else:
        _check_operand(node["right"], "right")

    if op in ("above_for", "below_for"):
        period = node.get("period", 2)
        if isinstance(period, bool) or not isinstance(period, int) or not (1 <= period <= 250):
            raise RuleError(f"{op} 的 period 必须是 1~250 的整数，收到 {node.get('period')!r}")

    return depth


def _check_operand(operand, where: str) -> None:
    """检查操作数（左边或右边）。

    统一写法是 ``{"ind": "ma", "period": 5}`` 或 ``{"ind": "const", "value": 1.5}``；
    也允许写成 ``"ma"``（不带参数的裸指标名，等价于 ``{"ind":"ma"}``）。

    ⚠️ 早期版本允许 ``{"const": 1.5}`` 这种「用字段名代替 ind」的写法，
    结果 ``_resolve`` 里 ``operand["ind"]`` 直接 KeyError 崩掉——而校验层
    没拦住它。现在统一要求 ind，校验和求值用的是同一套判断。
    """
    if isinstance(operand, bool):
        raise RuleError(f"{where} 不能是布尔值")

    # 裸数字：常量
    if isinstance(operand, (int, float)):
        return

    # 裸字符串：指标名
    if isinstance(operand, str):
        if operand not in ALL_INDICATORS:
            raise RuleError(
                f"{where} 是未知指标 {operand!r}。可用指标：{_indicator_hint()}"
            )
        return

    if not isinstance(operand, dict):
        raise RuleError(
            f"{where} 必须是指标名、指标对象或数字，收到 {type(operand).__name__}"
        )

    ind = operand.get("ind")
    if not isinstance(ind, str) or not ind:
        # 给出针对性的提示：这是最容易写错的地方
        hint = ""
        if "const" in operand:
            hint = "。常量要写成 {\"ind\": \"const\", \"value\": 1.5}"
        raise RuleError(f"{where} 缺少 ind 字段（当前 {_brief(operand)}）{hint}")

    if ind not in ALL_INDICATORS:
        raise RuleError(f"{where} 是未知指标 {ind!r}。可用指标：{_indicator_hint()}")

    if ind == "const":
        value = operand.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuleError(
                f"{where} 的 const 必须带数值型 value，"
                f"如 {{\"ind\": \"const\", \"value\": 1.5}}"
            )
        return

    # period 类参数：**必须在这一层就校验范围**。
    # 只靠 compute() 运行期检查的话，用户会等到跑完几千只股票才发现参数写错了。
    for pname in ("period", "fast", "slow", "signal"):
        if pname in operand:
            raw = operand[pname]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise RuleError(f"{where} 的 {pname} 必须是整数，收到 {raw!r}")
            if not (1 <= raw <= 250):
                raise RuleError(f"{where} 的 {pname} 必须在 1~250 之间，收到 {raw}")
    if "std" in operand:
        raw = operand["std"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RuleError(f"{where} 的 std 必须是数字，收到 {raw!r}")
        if not (0.1 <= float(raw) <= 10):
            raise RuleError(f"{where} 的 std 必须在 0.1~10 之间，收到 {raw}")


def _indicator_hint() -> str:
    """把可用指标列出来给用户/大模型看，太长时分组显示。"""
    items = sorted(ALL_INDICATORS)
    return ", ".join(items)


def _count_leaves(node) -> int:
    """统计叶子（实际比较）节点数。"""
    if not isinstance(node, dict):
        return 0
    for kind in LOGIC_KEYS:
        if kind in node:
            if kind == "not":
                return _count_leaves(node["not"])
            return sum(_count_leaves(s) for s in node[kind])
    return 1


def _brief(node) -> str:
    """把节点压成一行短描述，用于报错信息。"""
    try:
        text = str(node)
    except Exception:  # noqa: BLE001
        return "<无法序列化>"
    return text[:80] + ("…" if len(text) > 80 else "")


# ══════════════════════════════════════════════════════════════════
#  求值
# ══════════════════════════════════════════════════════════════════

def _operand_parts(operand) -> tuple[str, dict]:
    """把操作数拆成 (指标名, 参数字典)。

    **这是校验层和求值层唯一的解析入口**，保证两边的理解一致。
    早期版本在 _resolve / _at / _trend 里各写了一遍 ``operand["ind"]``，
    三处的容错程度不同，导致「校验通过但求值崩溃」。
    """
    if isinstance(operand, str):
        return operand, {}
    if isinstance(operand, bool):
        raise RuleError("布尔值不能作为操作数")
    if isinstance(operand, (int, float)):
        # 裸数字等价于常量指标
        return "const", {"value": float(operand)}
    if not isinstance(operand, dict):
        raise RuleError(f"操作数类型非法：{type(operand).__name__}")

    ind = operand.get("ind")
    if not isinstance(ind, str) or not ind:
        hint = ""
        if "const" in operand:
            hint = "；常量要写成 {\"ind\": \"const\", \"value\": 1.5}"
        raise RuleError(f"操作数缺少 ind 字段：{_brief(operand)}{hint}")
    params = {k: v for k, v in operand.items() if k != "ind"}
    return ind, params


def _resolve(operand, df: pd.DataFrame, cache: dict) -> float | None:
    """把一个操作数算成**最后一根 K 线上的标量**。

    Returns:
        标量；指标为 NaN 或数据不足时返回 None（调用方据此判定为不满足）。
    """
    name, params = _operand_parts(operand)

    if name == "const":
        try:
            return float(params.get("value", 0.0))
        except (TypeError, ValueError):
            raise RuleError(f"const 的 value 必须是数字：{params.get('value')!r}") from None

    if name in CROSS_SECTION_INDICATORS:
        # 截面指标在单股上下文中无法计算，evaluator 外层会先注入现成值
        key = f"__cross__{name}"
        if key in cache:
            return cache[key]
        raise RuleError(f"截面指标 {name!r} 不能在单只股票上计算")

    cache_key = (name, tuple(sorted(params.items())))
    if cache_key not in cache:
        cache[cache_key] = compute(name, df, params)

    series = cache[cache_key]
    if len(series) == 0:
        return None
    value = series.iloc[-1]
    if value is None or pd.isna(value):
        return None
    return float(value)


def evaluate(rule: dict, df: pd.DataFrame, extra: dict | None = None) -> bool:
    """判断单只股票是否命中规则。

    Args:
        rule: 已通过 validate() 的规则。
        df: 该股票的日线（升序）。
        extra: 外层预计算的截面值，如 {"rank_pct:period=20": 88.5}。

    Returns:
        命中返回 True。任何指标算出 NaN 都视为「不满足」（而不是报错）。
    """
    if df is None or len(df) < 2:
        return False
    cache: dict = dict(extra or {})
    return _eval_node(rule["conditions"], df, cache)


def _eval_node(node: dict, df: pd.DataFrame, cache: dict) -> bool:
    """递归求值。"""
    for kind in LOGIC_KEYS:
        if kind in node:
            if kind == "not":
                return not _eval_node(node["not"], df, cache)
            values = [_eval_node(sub, df, cache) for sub in node[kind]]
            return all(values) if kind == "all" else any(values)

    op = node["op"]
    left = _resolve(node["left"], df, cache)
    if left is None:
        return False

    if op == "between":
        lo = _resolve(node["right"][0], df, cache)
        hi = _resolve(node["right"][1], df, cache)
        if lo is None or hi is None:
            return False
        return min(lo, hi) <= left <= max(lo, hi)

    if op in ("rising", "falling"):
        return _trend(left, node, df, cache, rising=(op == "rising"))

    right = _resolve(node["right"], df, cache)
    if right is None:
        return False

    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    if op == "<=":
        return left <= right
    if op == "==":
        return abs(left - right) < 1e-9
    if op == "!=":
        return abs(left - right) >= 1e-9
    if op == "cross_above":
        return _cross(node, df, cache, up=True)
    if op == "cross_below":
        return _cross(node, df, cache, up=False)
    if op == "above_for":
        return _held(node, df, cache, above=True)
    if op == "below_for":
        return _held(node, df, cache, above=False)

    raise RuleError(f"操作符 {op!r} 未实现（validate 应该已经拦住了）")


def _at(operand, df: pd.DataFrame, offset: int, cache: dict):
    """取倒数第 offset+1 根 K 线上的标量（offset=0 是今天）。"""
    name, params = _operand_parts(operand)

    if name == "const":
        return float(params.get("value", 0.0))

    if name in CROSS_SECTION_INDICATORS:
        return cache.get(f"__cross__{name}")

    cache_key = (name, tuple(sorted(params.items())))
    if cache_key not in cache:
        cache[cache_key] = compute(name, df, params)
    series = cache[cache_key]

    idx = len(series) - 1 - offset
    if idx < 0:
        return None
    value = series.iloc[idx]
    if value is None or pd.isna(value):
        return None
    return float(value)


def _cross(node: dict, df: pd.DataFrame, cache: dict, up: bool) -> bool:
    """判断上穿/下穿：昨日 left≤right 且今日 left>right（上穿），反之亦然。"""
    l_now = _at(node["left"], df, 0, cache)
    r_now = _at(node["right"], df, 0, cache)
    l_prev = _at(node["left"], df, 1, cache)
    r_prev = _at(node["right"], df, 1, cache)
    if None in (l_now, r_now, l_prev, r_prev):
        return False
    if up:
        return l_prev <= r_prev and l_now > r_now
    return l_prev >= r_prev and l_now < r_now


def _held(node: dict, df: pd.DataFrame, cache: dict, above: bool) -> bool:
    """判断「连续 N 天保持在某一边」。"""
    period = int(node.get("period", 2))
    for offset in range(period):
        left = _at(node["left"], df, offset, cache)
        right = _at(node["right"], df, offset, cache)
        if left is None or right is None:
            return False
        if above and not (left > right):
            return False
        if not above and not (left < right):
            return False
    return True


def _trend(left: float, node: dict, df: pd.DataFrame, cache: dict,
           rising: bool) -> bool:
    """判断左值连续 N 天递增/递减。

    注意 ``left`` 是今日值（已由调用方解析并确认非 NaN），还要往前取 N 天比较。
    """
    period = int(node.get("period", 2))
    operand = node["left"]

    # 常量谈「递增」没有意义
    name, params = _operand_parts(operand)
    if name == "const":
        return False
    if name in CROSS_SECTION_INDICATORS:
        return False

    cache_key = (name, tuple(sorted(params.items())))
    if cache_key not in cache:
        cache[cache_key] = compute(name, df, params)
    series = cache[cache_key]

    need = period
    if len(series) < need + 1:
        return False
    window = series.iloc[-(need + 1):]
    if window.isna().any():
        return False
    diffs = window.diff().iloc[1:]
    return bool((diffs > 0).all()) if rising else bool((diffs < 0).all())
