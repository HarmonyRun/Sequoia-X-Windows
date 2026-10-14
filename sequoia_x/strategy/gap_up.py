"""向上跳空缺口策略：今日最低价高于昨日最高价，且缺口幅度温和。

跳空高开是隔夜出现利好的直接体现，整个缺口区间内没有任何成交，
上方到缺口前的区域不存在套牢盘，属于典型的强势结构。但跳空过大
（接近涨停开盘）往往意味着当天就是情绪顶，所以这里把缺口幅度限制
在 1% ~ 9% 之间，只取温和高开且放量收阳的品种。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class GapUpStrategy(BaseStrategy):
    """向上跳空缺口策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 今日最低价 > 昨日最高价（形成向上的跳空缺口，全天未回补）
    2. 跳空幅度 (今日开盘 / 昨日最高 - 1) 在 1% ~ 9% 之间
    3. 成交量 > 20 日均量 × 1.5（跳空需要放量确认）
    4. 当日收阳（close > open，高开高走而非高开低走）

    Attributes:
        webhook_key: 路由到 'gap_up' 专属飞书机器人。
    """

    webhook_key: str = "gap_up"
    _MIN_BARS: int = 21
    _MIN_GAP: float = 0.01
    _MAX_GAP: float = 0.09
    _VOLUME_MULT: float = 1.5

    def run(self) -> list[str]:
        """遍历全市场，返回满足向上跳空缺口的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                last = len(df) - 1
                today_low = df["low"].iloc[last]
                today_high = df["high"].iloc[last]
                today_open = df["open"].iloc[last]
                today_close = df["close"].iloc[last]
                today_volume = df["volume"].iloc[last]
                prev_high = df["high"].iloc[last - 1]
                vol_ma20 = df["volume"].rolling(20).mean().iloc[last]

                vals = [
                    today_low, today_high, today_open, today_close,
                    today_volume, prev_high, vol_ma20,
                ]
                if any(pd.isna(v) for v in vals):
                    continue
                if prev_high <= 0 or vol_ma20 <= 0:
                    continue

                # 条件 1：向上跳空且全天未回补
                gap_exists = today_low > prev_high
                # 条件 2：缺口幅度温和
                gap_pct = today_open / prev_high - 1
                gap_ok = self._MIN_GAP <= gap_pct <= self._MAX_GAP
                # 条件 3：放量
                volume_ok = today_volume > vol_ma20 * self._VOLUME_MULT
                # 条件 4：收阳
                bullish = today_close > today_open

                if gap_exists and gap_ok and volume_ok and bullish:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] GapUpStrategy 计算失败：{exc}")
                continue

        logger.info(f"GapUpStrategy 选出 {len(selected)} 只股票")
        return selected
