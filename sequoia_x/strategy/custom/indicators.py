"""规则 DSL 的指标计算层：把规则里的 `ind` 翻译成 pandas Series。

这一层只负责「算出一个序列」，不做比较、不做逻辑组合——那些在 evaluator.py。
分开的好处是：指标可以单独测试（对照手算），也便于将来加新指标。

约定（非常重要，所有函数必须遵守）：
1. 返回的 Series **与输入的 df 等长、索引一致**，前面预热不够的位置填 NaN
2. **绝不修改传入的 df**（策略会复用同一个 df 算多个指标）
3. 任何参数问题抛 IndicatorError，由 evaluator 决定是跳过还是报错
"""

from __future__ import annotations

import pandas as pd

# 允许出现在 period 类参数上的取值范围。
# 上限 250 是实测约束：本地库每只股票有 670 个交易日，但新股可能只有几天，
# 超过 250 的窗口在本项目的数据跨度（约 2.6 年）里意义不大。
MIN_PERIOD = 1
MAX_PERIOD = 250

# 截面指标：需要全市场数据，不能在单只股票上算（由 evaluator 特殊处理）
CROSS_SECTION_INDICATORS = {"rank_pct"}

# 单只股票即可计算的指标白名单。evaluator 用它做校验。
SERIES_INDICATORS = {
    # 价格与成交
    "open", "high", "low", "close", "volume", "amount", "pct_chg",
    # 均线
    "ma", "ema", "vol_ma",
    # 技术指标
    "macd_dif", "macd_dea", "rsi", "boll_upper", "boll_lower", "boll_mid",
    "boll_width", "atr",
    # 区间高低点
    "highest", "lowest", "hhv_close", "llv_close",
    # 形态与统计
    "consecutive_up", "consecutive_down", "vol_ratio", "pct_chg_nd",
    "drawdown_from_high", "gap_up", "is_yang", "is_yin", "close_position",
    # 常量
    "const",
}

ALL_INDICATORS = SERIES_INDICATORS | CROSS_SECTION_INDICATORS


class IndicatorError(ValueError):
    """指标名或参数非法。"""


def _period(params: dict, default: int, name: str = "period") -> int:
    """取并校验 period 类参数。"""
    raw = params.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise IndicatorError(f"{name} 必须是整数，收到 {raw!r}") from None
    if not (MIN_PERIOD <= value <= MAX_PERIOD):
        raise IndicatorError(f"{name} 必须在 {MIN_PERIOD}~{MAX_PERIOD} 之间，收到 {value}")
    return value


def _num(params: dict, name: str, default: float) -> float:
    """取并校验数值参数。"""
    raw = params.get(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise IndicatorError(f"{name} 必须是数字，收到 {raw!r}") from None


def compute(name: str, df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """计算单个指标，返回与 df 等长的 Series。

    Args:
        name: 指标名（见 SERIES_INDICATORS）。
        df: 含 open/high/low/close/volume/turnover 的日线，按日期升序。
        params: 指标参数，如 {"period": 20}。

    Returns:
        与 df 等长、索引一致的 Series。预热期或数据不足处为 NaN。

    Raises:
        IndicatorError: 指标名未知，或参数越界。
    """
    p = dict(params or {})

    if name == "const":
        # 常量：整列同一个值。用 close 的长度和索引，保证对齐。
        return pd.Series(_num(p, "value", 0.0), index=df.index, dtype="float64")

    if name not in SERIES_INDICATORS:
        raise IndicatorError(
            f"未知指标 {name!r}。可用指标：{', '.join(sorted(SERIES_INDICATORS))}"
        )

    # ── 原始字段直出 ──
    if name in ("open", "high", "low", "close", "volume"):
        return df[name].astype("float64")

    if name == "amount":
        # 本地库里成交额字段叫 turnover（历史沿革），对外统一叫 amount
        col = "turnover" if "turnover" in df.columns else "amount"
        return df[col].astype("float64")

    close = df["close"].astype("float64")

    # ── 当日涨跌幅 ──
    if name == "pct_chg":
        return close.pct_change() * 100

    # ── 均线类 ──
    if name == "ma":
        return close.rolling(_period(p, 20)).mean()
    if name == "ema":
        return close.ewm(span=_period(p, 20), adjust=False).mean()
    if name == "vol_ma":
        return df["volume"].astype("float64").rolling(_period(p, 20)).mean()

    # ── MACD ──
    if name in ("macd_dif", "macd_dea"):
        fast = _period(p, 12, "fast")
        slow = _period(p, 26, "slow")
        signal = _period(p, 9, "signal")
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        if name == "macd_dif":
            return dif
        return dif.ewm(span=signal, adjust=False).mean()

    # ── RSI（Wilder 平滑）──
    if name == "rsi":
        period = _period(p, 14)
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        # ⚠️ 不能写 avg_loss.replace(0, pd.NA)：浮点 Series 混入 pd.NA 会整体
        #    升格成 object dtype，后面取标量会拿到 NAType、astype(float) 直接抛错。
        #    用 mask 挖掉分母为 0 的位置，保持 float64。
        flat = (avg_gain == 0) & (avg_loss == 0)
        safe_loss = avg_loss.mask(avg_loss == 0)
        rs = avg_gain / safe_loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.mask(avg_loss == 0, 100.0)   # 分母为 0（连续上涨）-> RSI 100
        rsi = rsi.mask(flat)                    # 完全无波动 -> 无定义
        return rsi.mask(delta.isna()).astype("float64")

    # ── 布林带 ──
    if name in ("boll_upper", "boll_lower", "boll_mid", "boll_width"):
        period = _period(p, 20)
        std_mult = _num(p, "std", 2.0)
        mid = close.rolling(period).mean()
        std = close.rolling(period).std(ddof=0)
        upper = mid + std * std_mult
        lower = mid - std * std_mult
        if name == "boll_mid":
            return mid
        if name == "boll_upper":
            return upper
        if name == "boll_lower":
            return lower
        # 带宽：相对中轨的宽度。0 附近表示挤压
        width = (upper - lower) / mid.replace(0, pd.NA)
        return width.astype("float64")

    # ── ATR ──
    if name == "atr":
        period = _period(p, 14)
        high = df["high"].astype("float64")
        low = df["low"].astype("float64")
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()

    # ── 区间高低点 ──
    if name == "highest":
        return df["high"].astype("float64").rolling(_period(p, 20)).max()
    if name == "lowest":
        return df["low"].astype("float64").rolling(_period(p, 20)).min()
    if name == "hhv_close":
        return close.rolling(_period(p, 20)).max()
    if name == "llv_close":
        return close.rolling(_period(p, 20)).min()

    # ── 连续涨跌天数 ──
    if name in ("consecutive_up", "consecutive_down"):
        diff = close.diff()
        up = diff > 0
        # 连续计数：把「不满足」的位置作为分组边界，组内行号就是连续长度
        groups = (~up).cumsum() if name == "consecutive_up" else up.cumsum()
        return up.groupby(groups).cumsum().astype("float64")

    # ── 量比 ──
    if name == "vol_ratio":
        period = _period(p, 20)
        vol = df["volume"].astype("float64")
        avg = vol.rolling(period).mean()
        return (vol / avg.replace(0, pd.NA)).astype("float64")

    # ── 最近 N 日累计涨跌幅 ──
    if name == "pct_chg_nd":
        period = _period(p, 20)
        return (close / close.shift(period) - 1) * 100

    # ── 距 N 日最高价的回撤（负值）──
    if name == "drawdown_from_high":
        period = _period(p, 20)
        high = close.rolling(period).max()
        return ((close / high) - 1) * 100

    # ── 向上跳空幅度 ──
    if name == "gap_up":
        # 相对昨日最高价：今日开盘跳空多少。未跳空时为 0（不是负值），
        # 这样 "gap_up > 0" 就能直接表达「有跳空」。
        prev_high = df["high"].astype("float64").shift(1)
        pct = (df["open"].astype("float64") / prev_high - 1) * 100
        return pct.clip(lower=0).fillna(0.0)

    # ── K 线形态 ──
    if name == "is_yang":
        return (close > df["open"].astype("float64")).astype("float64")
    if name == "is_yin":
        return (close < df["open"].astype("float64")).astype("float64")
    if name == "close_position":
        # 收盘在当日振幅中的位置：0=最低，1=最高。一字板无振幅 -> 0.5
        high = df["high"].astype("float64")
        low = df["low"].astype("float64")
        span = high - low
        return ((close - low) / span.where(span > 0, pd.NA)).fillna(0.5).astype("float64")

    raise IndicatorError(f"指标 {name!r} 已列入白名单但未实现（属于内部不一致）")
