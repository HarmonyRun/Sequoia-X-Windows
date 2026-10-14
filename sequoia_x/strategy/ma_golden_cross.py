"""双均线金叉策略：MA5 上穿 MA20，且成交量高于 20 日均量。

与 ma_volume.py 的区别在于「放量」的门槛：ma_volume 要求 1.5 倍量能的
强放量，更适合捕捉启动瞬间；这里只要量能不萎缩（> 1 倍均量）就认，
用于覆盖那些温和放量、缓步上行的品种，两个策略的信号互补。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaGoldenCrossStrategy(BaseStrategy):
    """双均线金叉策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. MA5 上穿 MA20：昨日 MA5 <= MA20，今日 MA5 > MA20
    2. 当日成交量 > 20 日均量（量能配合，排除缩量假金叉）

    Attributes:
        webhook_key: 路由到 'ma_cross' 专属飞书机器人。
    """

    webhook_key: str = "ma_cross"
    _MIN_BARS: int = 25  # 20 日均线 + 前一日判断 + 余量

    def run(self) -> list[str]:
        """遍历全市场，返回满足 MA5 上穿 MA20 且放量的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                ma5 = close.rolling(5).mean()
                ma20 = close.rolling(20).mean()
                vol_ma20 = df["volume"].rolling(20).mean()

                last = len(df) - 1
                vals = [
                    ma5.iloc[last], ma20.iloc[last],
                    ma5.iloc[last - 1], ma20.iloc[last - 1],
                    vol_ma20.iloc[last],
                ]
                if any(pd.isna(v) for v in vals):
                    continue

                # 条件 1：MA5 上穿 MA20
                golden_cross = vals[3] <= vals[1] and vals[0] > vals[1]
                # 条件 2：量能不低于 20 日均量
                volume_ok = df["volume"].iloc[last] > vals[4]

                if golden_cross and volume_ok:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] MaGoldenCrossStrategy 计算失败：{exc}")
                continue

        logger.info(f"MaGoldenCrossStrategy 选出 {len(selected)} 只股票")
        return selected
