"""均线粘合突破策略：四条均线极度收敛后放量向上突破。

MA5/MA10/MA20/MA30 几乎纠缠在一起，说明短中期的持仓成本被压缩到同一
水平，市场进入方向选择前的静默期。一旦放量向上突破这四条线，往往就是
趋势启动的第一根阳线，且上方没有套牢盘压制。
"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class MaSqueezeBreakoutStrategy(BaseStrategy):
    """均线粘合突破策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 四线粘合：MA5/MA10/MA20/MA30 的极值差 / 均值 < 3%
    2. 今日收盘 > 四线最大值（放量向上突破）
    3. 成交量 > 20 日均量 × 1.5（突破必须有量能配合）

    Attributes:
        webhook_key: 路由到 'ma_squeeze' 专属飞书机器人。
    """

    webhook_key: str = "ma_squeeze"
    _MIN_BARS: int = 35          # 最长均线 30 日 + 余量
    _SQUEEZE_MAX: float = 0.03   # 四线极值差 / 均值 的上限
    _VOLUME_MULT: float = 1.5

    def run(self) -> list[str]:
        """遍历全市场，返回满足均线粘合后放量突破的股票代码列表。"""
        symbols = self.engine.get_local_symbols()
        selected: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                close = df["close"]
                ma5 = close.rolling(5).mean()
                ma10 = close.rolling(10).mean()
                ma20 = close.rolling(20).mean()
                ma30 = close.rolling(30).mean()
                vol_ma20 = df["volume"].rolling(20).mean()

                last = len(df) - 1
                mas = [
                    ma5.iloc[last], ma10.iloc[last],
                    ma20.iloc[last], ma30.iloc[last],
                ]
                if any(pd.isna(v) for v in mas) or pd.isna(vol_ma20.iloc[last]):
                    continue

                ma_max = max(mas)
                ma_min = min(mas)
                ma_mean = sum(mas) / len(mas)
                if ma_mean <= 0:
                    continue

                # 条件 1：四线粘合
                squeeze = (ma_max - ma_min) / ma_mean < self._SQUEEZE_MAX
                # 条件 2：收盘突破四线最大值
                breakout = close.iloc[last] > ma_max
                # 条件 3：放量
                volume_ok = (
                    df["volume"].iloc[last] > vol_ma20.iloc[last] * self._VOLUME_MULT
                )

                if squeeze and breakout and volume_ok:
                    selected.append(symbol)

            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{symbol}] MaSqueezeBreakoutStrategy 计算失败：{exc}")
                continue

        logger.info(f"MaSqueezeBreakoutStrategy 选出 {len(selected)} 只股票")
        return selected
