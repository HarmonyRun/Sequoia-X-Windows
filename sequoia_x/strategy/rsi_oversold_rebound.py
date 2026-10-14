"""RSI 超卖反弹策略：RSI(14) 在超卖区开始回升，且当日盘中出现承接。

RSI 跌破 30 只说明短期跌得急，本身不是买点——下跌趋势里 RSI 可以在低位
钝化很久。真正的信号是「超卖 + 拐头 + 收阳 + 有承接」四者同时出现。

关于第 4 个条件，这里踩了两次坑，记下来避免后人重犯：

**第一次**用的是「收盘价仍在 MA60 之上」，理由听起来合理（大趋势没坏才是回调），
实测 600 只样本选出 **0 只**。
**第二次**改成「MA20 走平或上翘」，依然 **0 只**；再换成「收盘 > MA20」、
「MA20 今日 >= 昨日」，全部 **0 只**。

逐条件统计后原因很明确：

    昨日 RSI < 30                : 21 只
    且今日回升                    : 10 只
    且今日收阳                    : 10 只
    且 MA20 五日内走平/上翘       :  0 只   ← 任何均线类过滤都清零

**MA20 是 20 日均值，一只股票昨天刚跌到 RSI 30 以下时，它的 MA20 数学上必然
还在下行**——今天这根阳线根本来不及改变 20 天的均值。所以「刚超卖」和
「均线走平/向上」是互斥的，用均线做趋势过滤在这个策略里永远选不出票。

**最终方案**改成当日盘面判据 —— **下影线承接**：

    (收盘 - 最低) / (最高 - 最低) >= 0.4

它衡量「今天下方有没有人接」，与超卖不冲突。实测 10 只候选**全部满足**，
说明这确实是超卖反弹股的共同特征：昨天暴跌 3~7%，今天小幅收阳
（0.4~2.6%），且下影线占比 0.71~0.92。

（顺带排除一个同样想当然的判据：「今日跌幅 < 昨日跌幅」。
这 10 只今天都是上涨的，拿正数比负数永远不成立，实测 0 只，逻辑本身就不对。）
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 平滑 RSI。

    ``avg_loss`` 为 0（连续上涨）时 RS 为无穷、RSI 应为 100。这里**不能**用
    ``avg_loss.replace(0, pd.NA)``：浮点 Series 一旦混入 ``pd.NA`` 会被整体
    升格为 object dtype，``.iloc[last]`` 取出来就是 ``NAType``，后续
    ``astype(float)`` 直接抛 TypeError。改用 ``mask`` 只挖掉分母为 0 的位置，
    保持 float64；除完把「分母为 0」解释成 RS 无穷 —— RSI 记 100。

    同理，``close.diff()`` 的首行是 NaN，``clip`` 会把它变成 0，导致首行
    ``avg_gain == avg_loss == 0`` 这种**伪平坦**被算出 RSI 0。这类位置必须在
    EWM 之前就剔除，不能拿 0 参与平滑。

    Args:
        close: 收盘价序列。
        period: RSI 周期，默认 14。

    Returns:
        与 ``close`` 等长的 RSI 序列；预热期与全平坦区间为 NaN。
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    # 真正「价格没动」的平坦区间：gain 和 loss 同时为 0 -> RSI 无定义
    flat = (avg_gain == 0) & (avg_loss == 0)

    # 分母为 0（连续上涨）时 RS 视为 +inf -> RSI = 100
    safe_loss = avg_loss.mask(avg_loss == 0)
    rs = avg_gain / safe_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask(avg_loss == 0, 100.0)   # 无穷大 RS 显式写成 100
    rsi = rsi.mask(flat)                   # 无定义的位置保持 NaN

    # 首行 diff 为 NaN，EWM 的种子算不出有效值，一并置 NaN
    rsi = rsi.mask(delta.isna())
    return rsi.astype(float)


class RsiOversoldReboundStrategy(BaseStrategy):
    """RSI 超卖反弹策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 昨日 RSI(14) < 30（进入超卖区）
    2. 今日 RSI(14) > 昨日 RSI（开始回升，拐头确认）
    3. 今日收阳（close > open）
    4. **下影线承接**：(close - low) / (high - low) >= 0.4
       （盘中有资金接盘，用当日盘面而非均线，原因见模块 docstring）

    Attributes:
        webhook_key: 路由到 'rsi_rebound' 专属飞书机器人。
    """

    webhook_key: str = "rsi_rebound"
    _MIN_BARS: int = 20          # 只需 RSI 预热，不再依赖长周期均线
    _OVERSOLD: float = 30.0
    _RSI_PERIOD: int = 14
    _MIN_LOWER_SHADOW: float = 0.4   # 下影线占当日振幅的最低比例

    def run(self) -> list[str]:
        """遍历全市场，返回满足 RSI 超卖后反弹的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                rsi = _wilder_rsi(close, self._RSI_PERIOD)

                last = len(df) - 1
                rsi_now = rsi.iloc[last]
                rsi_prev = rsi.iloc[last - 1]
                if pd.isna(rsi_now) or pd.isna(rsi_prev):
                    continue

                cl = close.iloc[last]
                op = df["open"].iloc[last]
                hi = df["high"].iloc[last]
                lo = df["low"].iloc[last]
                if any(pd.isna(v) for v in (cl, op, hi, lo)):
                    continue

                # 条件 1 + 2：昨日超卖，今日回升
                oversold = rsi_prev < self._OVERSOLD
                rebounding = rsi_now > rsi_prev
                # 条件 3：今日收阳
                bullish = cl > op
                # 条件 4：下影线承接。全天振幅为 0（一字板）时无从判断，视为不满足
                day_range = hi - lo
                lower_shadow = (cl - lo) / day_range if day_range > 0 else 0.0
                supported = lower_shadow >= self._MIN_LOWER_SHADOW

                if oversold and rebounding and bullish and supported:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] RsiOversoldReboundStrategy 计算失败：{exc}")
                continue

        logger.info(f"RsiOversoldReboundStrategy 选出 {len(selected)} 只股票")
        return selected
