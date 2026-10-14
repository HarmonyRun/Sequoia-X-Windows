"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)

# 市值快照的进程级冷却：失败一次后，这段时间内不再尝试。
#
# 为什么需要它：市值只用于**排序**，不影响选出的股票集合。而这个快照接口
# 在部分网络环境下是必然失败的，每次选股都去等一遍超时纯属浪费。
# 30 分钟足够覆盖「工作台连续点几次重新计算信号」和「一次日常选股」的场景，
# 又不会把临时性网络抖动记太久。
_SPOT_COOLDOWN = 30 * 60
_SPOT_UNAVAILABLE_UNTIL = 0.0


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（今日 close > 今日 open），且必须真涨（今日 close > 昨日 close）

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）

    def _get_market_caps(self, symbols: list[str]) -> dict[str, float]:
        """查询候选股票的流通市值。

        数据源跟随 ``engine.data_source``：
        - akshare 系（东方财富/腾讯/新浪/auto）：取东方财富全市场快照的「流通市值」列
        - baostock：流通股本 = 成交量 / (换手率% / 100)，流通市值 = 流通股本 × 不复权收盘价

        注意市值只用于**排序**（不参与过滤），所以快照拿不到时策略仍然出同样的
        候选集合，只是顺序不同——属于可接受的降级。
        """
        if getattr(self.engine, "data_source", "baostock") != "baostock":
            return self._get_market_caps_akshare(symbols)

        from datetime import date

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        market_caps: dict[str, float] = {}

        bs.login()
        try:
            for symbol in symbols:
                bs_code = self.engine._to_baostock_code(symbol)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "close,volume,turn",
                    start_date=today_str,
                    end_date=today_str,
                    frequency="d",
                    adjustflag="3",  # 不复权，真实价格
                )
                while rs.next():
                    row = rs.get_row_data()
                    try:
                        close = float(row[0])
                        volume = float(row[1])
                        turn = float(row[2])
                        if turn > 0:
                            circulating_shares = volume / (turn / 100)
                            market_caps[symbol] = circulating_shares * close
                    except (ValueError, ZeroDivisionError):
                        continue
        finally:
            bs.logout()

        return market_caps

    def _get_market_caps_akshare(self, symbols: list[str]) -> dict[str, float]:
        """从东方财富全市场快照直接读「流通市值」。

        这里做了两层优化，因为**市值只用于排序、不参与过滤**（见
        ``_get_market_caps`` 的说明），拿不到完全不影响选股结果：

        1. **单次尝试，不重试**。原实现走 ``_ak_retry(retries=3)``，而这个
           接口在当前网络环境下是**必然失败**的（``82.push2.eastmoney.com``
           连不上），重试 3 次 + 指数退避要白等 20~40 秒，而换来的只是
           「排序退化为按代码」——和单次失败的结果完全一样。
        2. **进程内记住失败**。同一进程里本次拿不到，下一次跑策略（换股池、
           重算信号）也不会拿到，没必要每次都再等一遍超时。

        实测：改动前 turtle_trade 单次耗时 23 秒（几乎全在等超时），
        改动后首次约 8 秒（一次网络超时），后续同进程内直接跳过。
        """
        import time

        import akshare as ak

        global _SPOT_UNAVAILABLE_UNTIL
        now = time.time()
        if now < _SPOT_UNAVAILABLE_UNTIL:
            # 上次刚失败过，直接跳过，别再等一次超时
            return {}

        try:
            spot = ak.stock_zh_a_spot_em()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[akshare] 全市场快照（市值）获取失败：{type(exc).__name__}。"
                f"海龟策略本次不做市值排序（不影响选出的股票集合，只是顺序不同）；"
                f"{_SPOT_COOLDOWN // 60} 分钟内不再重试。"
            )
            _SPOT_UNAVAILABLE_UNTIL = now + _SPOT_COOLDOWN
            return {}

        if spot is None or spot.empty or "流通市值" not in spot.columns:
            logger.warning("akshare 快照为空或缺少流通市值列，海龟策略不做市值排序")
            _SPOT_UNAVAILABLE_UNTIL = now + _SPOT_COOLDOWN
            return {}

        want = set(symbols)
        caps: dict[str, float] = {}
        for code, cap in zip(spot["代码"].astype(str).str.zfill(6), spot["流通市值"]):
            if code in want:
                try:
                    caps[code] = float(cap)
                except (TypeError, ValueError):
                    continue
        return caps

    def run(self) -> list[str]:
        """
        遍历全市场，返回满足海龟突破条件的股票代码列表。
        """
        symbols = self.engine.get_local_symbols()
        candidates: list[str] = []

        for symbol in symbols:
            try:
                df = self.engine.get_ohlcv(symbol)
                if len(df) < self._MIN_BARS:
                    continue

                # 向量化：前20日 high 的滚动最大值（不含当日，shift(1) 后取 rolling(20)）
                df["high_20"] = df["high"].shift(1).rolling(20).max()

                last = df.iloc[-1]
                prev = df.iloc[-2]  # 获取昨日数据，用于对比

                if pd.isna(last["high_20"]):
                    continue

                # 核心条件 1：突破前 20 天最高点
                breakout = last["close"] > last["high_20"]
                # 核心条件 2：流动性过亿
                liquid = last["turnover"] > 100_000_000

                # 【新增防守条件】拒绝郑州煤电式的高开低走大阴线！
                is_yang = last["close"] > last["open"]   # 实体必须是阳线（红柱）
                is_up = last["close"] > prev["close"]    # 必须是真涨，不能是假阳线

                if breakout and liquid and is_yang and is_up:
                    candidates.append(symbol)

            except Exception as exc:
                logger.warning(f"[{symbol}] TurtleTradeStrategy 计算失败：{exc}")
                continue

        # 按流通市值从大到小排序
        if candidates:
            market_caps = self._get_market_caps(candidates)
            candidates.sort(key=lambda s: market_caps.get(s, 0), reverse=True)

        logger.info(f"TurtleTradeStrategy 选出 {len(candidates)} 只股票")
        return candidates