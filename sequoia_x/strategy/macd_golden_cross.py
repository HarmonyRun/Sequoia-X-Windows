"""MACD 金叉策略：DIF 上穿 DEA，且发生在零轴上方（强势区金叉）。

零轴下方的金叉往往是下跌中继，零轴上方的金叉才表示上升趋势中的再次启动，
所以这里额外要求 DIF > 0 来过滤弱势信号。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MacdGoldenCrossStrategy(BaseStrategy):
    """MACD 零轴上方金叉策略。

    选股条件（全部向量化）：
    1. DIF 上穿 DEA（昨日 DIF <= DEA，今日 DIF > DEA）
    2. DIF > 0（金叉发生在零轴上方，即强势区）
    3. 当日成交量 > 20 日均量（放量确认，排除缩量假金叉）

    Attributes:
        webhook_key: 路由到 'macd_cross' 专属飞书机器人。
    """

    webhook_key: str = "macd_cross"

    def run(self) -> list[str]:
        """遍历全市场，返回满足 MACD 零轴上金叉的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                # EMA26 + DEA9 需要足够预热，留 60 个交易日
                if len(df) < 60:
                    continue

                close = df["close"]

                # 标准 MACD 参数：EMA12 / EMA26 / DEA(9)
                ema_fast = close.ewm(span=12, adjust=False).mean()
                ema_slow = close.ewm(span=26, adjust=False).mean()
                dif = ema_fast - ema_slow
                dea = dif.ewm(span=9, adjust=False).mean()

                last = len(df) - 1
                vals = [
                    dif.iloc[last], dea.iloc[last],
                    dif.iloc[last - 1], dea.iloc[last - 1],
                ]
                if any(pd.isna(v) for v in vals):
                    continue

                cross = vals[3] <= vals[1] and vals[0] > vals[1]
                above_zero = vals[0] > 0

                vol_ma20 = df["volume"].rolling(20).mean().iloc[last]
                if pd.isna(vol_ma20):
                    continue
                volume_ok = df["volume"].iloc[last] > vol_ma20

                if cross and above_zero and volume_ok:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] MacdGoldenCrossStrategy 计算失败：{exc}")
                continue

        logger.info(f"MacdGoldenCrossStrategy 选出 {len(selected)} 只股票")
        return selected
