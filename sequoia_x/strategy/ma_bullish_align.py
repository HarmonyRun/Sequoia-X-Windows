"""均线多头排列策略：短中长期均线依次向上排列，且股价站上最短均线。

这是 A 股最常用的趋势判定方式之一。多头排列意味着不同周期的持仓成本
依次抬升，市场处于健康上升趋势中。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaBullishAlignStrategy(BaseStrategy):
    """均线多头排列策略。

    选股条件（全部向量化）：
    1. MA5 > MA10 > MA20 > MA60（均线依次排列）
    2. 收盘价 > MA5（股价在最短均线之上）
    3. MA20 相对 5 日前是抬升的（趋势仍在延续，排除走平/转头）

    Attributes:
        webhook_key: 路由到 'ma_bullish' 专属飞书机器人。
    """

    webhook_key: str = "ma_bullish"

    def run(self) -> list[str]:
        """遍历全市场，返回满足均线多头排列的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                # 需要 60 日均线 + 5 日前的 MA20，所以至少 65 个交易日
                if len(df) < 65:
                    continue

                close = df["close"]
                ma5 = close.rolling(5).mean()
                ma10 = close.rolling(10).mean()
                ma20 = close.rolling(20).mean()
                ma60 = close.rolling(60).mean()

                last_idx = len(df) - 1
                # 用 .iloc 取标量，避免把 Series 带进布尔比较
                if pd.isna(ma60.iloc[last_idx]) or pd.isna(ma20.iloc[last_idx - 5]):
                    continue

                aligned = (
                    ma5.iloc[last_idx] > ma10.iloc[last_idx]
                    and ma10.iloc[last_idx] > ma20.iloc[last_idx]
                    and ma20.iloc[last_idx] > ma60.iloc[last_idx]
                )
                above_short = close.iloc[last_idx] > ma5.iloc[last_idx]
                rising = ma20.iloc[last_idx] > ma20.iloc[last_idx - 5]

                if aligned and above_short and rising:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] MaBullishAlignStrategy 计算失败：{exc}")
                continue

        logger.info(f"MaBullishAlignStrategy 选出 {len(selected)} 只股票")
        return selected
