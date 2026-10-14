"""布林带突破策略：收盘价上穿布林上轨，且发生在带宽挤压之后。

布林带收缩（squeeze）代表波动率被压缩到极低水平，多空分歧收敛；
此时价格向上突破上轨，往往是新一轮趋势启动的起点。反过来，如果带宽
本来就在高位，价格贴着上轨走只是趋势中继，追进去容易吃回撤。
所以这里额外要求「带宽处于近 60 日低位」来过滤掉后者。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BollBreakoutStrategy(BaseStrategy):
    """布林带突破策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 收盘价上穿布林上轨(20, 2)：昨日收盘 <= 昨日上轨，今日收盘 > 今日上轨
    2. 带宽 (上轨 - 下轨) / 中轨 处于近 60 日低位（低于 40% 分位，即挤压后突破）
    3. 中轨向上（MA20 相对 5 日前抬升，确认不是下跌途中的反抽）

    Attributes:
        webhook_key: 路由到 'boll_breakout' 专属飞书机器人。
    """

    webhook_key: str = "boll_breakout"
    _MIN_BARS: int = 80        # 20 日窗口 + 60 日带宽分位 + 余量
    _BANDWIDTH_QUANTILE: float = 0.4

    def run(self) -> list[str]:
        """遍历全市场，返回满足布林带挤压后突破的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                mid = close.rolling(20).mean()
                std = close.rolling(20).std()
                upper = mid + 2 * std
                lower = mid - 2 * std
                # 带宽：归一化到中轨，避免高价股和低价股带宽不可比
                bandwidth = (upper - lower) / mid

                last = len(df) - 1
                vals = [
                    close.iloc[last], close.iloc[last - 1],
                    upper.iloc[last], upper.iloc[last - 1],
                    mid.iloc[last], mid.iloc[last - 5],
                    bandwidth.iloc[last],
                ]
                if any(pd.isna(v) for v in vals):
                    continue

                # 条件 1：收盘价上穿布林上轨
                cross_up = vals[1] <= vals[3] and vals[0] > vals[2]

                # 条件 2：带宽处于近 60 日低位（挤压）
                bw_window = bandwidth.iloc[max(0, last - 59): last + 1].dropna()
                if bw_window.empty:
                    continue
                squeezed = vals[6] <= bw_window.quantile(self._BANDWIDTH_QUANTILE)

                # 条件 3：中轨向上
                rising = vals[4] > vals[5]

                if cross_up and squeezed and rising:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] BollBreakoutStrategy 计算失败：{exc}")
                continue

        logger.info(f"BollBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected
