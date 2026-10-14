"""量价齐升策略：连续三日收阳、成交量逐日放大并创 20 日新高。

「量价齐升」是资金持续流入最直接的证据：价格连续走高且每一天的成交量
都比前一天大，说明买盘在不断加码而不是缩量拉升。同时用累计涨幅上限
把已经涨过头的票排除掉，避免追高。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class VolumePriceRiseStrategy(BaseStrategy):
    """量价齐升策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 连续 3 日收阳（close > open）
    2. 这 3 日成交量逐日递增（vol[-3] < vol[-2] < vol[-1]）
    3. 3 日累计涨幅在 3% ~ 20% 之间（排除涨幅不足和涨过头）
    4. 今日收盘创 20 日新高

    Attributes:
        webhook_key: 路由到 'vol_price' 专属飞书机器人。
    """

    webhook_key: str = "vol_price"
    _MIN_BARS: int = 21
    _MIN_GAIN: float = 0.03
    _MAX_GAIN: float = 0.20

    def run(self) -> list[str]:
        """遍历全市场，返回满足量价齐升的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                open_ = df["open"]
                volume = df["volume"]

                last = len(df) - 1
                vals = [
                    close.iloc[last], close.iloc[last - 1], close.iloc[last - 2],
                    close.iloc[last - 3],
                    open_.iloc[last], open_.iloc[last - 1], open_.iloc[last - 2],
                    volume.iloc[last], volume.iloc[last - 1], volume.iloc[last - 2],
                    volume.iloc[last - 3],
                ]
                if any(pd.isna(v) for v in vals):
                    continue

                # 条件 1：连续 3 日收阳
                three_up = (
                    vals[0] > vals[4] and vals[1] > vals[5] and vals[2] > vals[6]
                )
                # 条件 2：成交量逐日递增
                volume_rising = vals[7] > vals[8] > vals[9] > vals[10]
                # 条件 3：3 日累计涨幅在合理区间
                base = vals[3]
                if base <= 0:
                    continue
                gain = vals[0] / base - 1
                gain_ok = self._MIN_GAIN <= gain <= self._MAX_GAIN
                # 条件 4：收盘创 20 日新高
                high20 = close.rolling(20).max().iloc[last]
                if pd.isna(high20):
                    continue
                new_high = vals[0] >= high20

                if three_up and volume_rising and gain_ok and new_high:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] VolumePriceRiseStrategy 计算失败：{exc}")
                continue

        logger.info(f"VolumePriceRiseStrategy 选出 {len(selected)} 只股票")
        return selected
