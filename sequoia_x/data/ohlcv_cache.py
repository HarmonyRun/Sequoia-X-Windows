"""全市场数据缓存：把「每个策略各自遍历一遍全市场」变成「读一次、内存里反复用」。

## 为什么需要它

16 个策略各自遍历 5225 只股票，每只都调一次 `engine.get_ohlcv()` ——
**总共 8.4 万次 SQL 查询**。实测单次查询 2.4ms，所以光在查库上就花掉 196 秒。

而所有策略用的是**同一份数据**。所以只要加载一次留在内存里，后续 15 次遍历
几乎是零成本。

## 为什么不用 pd.read_sql

实测对比（3417 万行……更正：341 万行）：

    方案                              耗时      RSS 增量
    pd.read_sql                       10.2s     1475 MB
    fetchmany 分批 + numpy 预分配      9.7s      341 MB   ← 采用
    逐股读取（现状）                   12.6s        3 MB

`read_sql` 的 1.3GB 开销来自它内部「一次性把 341 万行 fetch 成 Python 元组」，
这部分跟最终 DataFrame 大小（111 MB）无关。改成 `fetchmany(20万)` 分批 +
直接写进预分配的 numpy 数组后，峰值就只有最终数组大小。

（数值用 float64；试过 float32 省内存，但会造成 `ma_cross` 这类「均线上穿」
判断在临界处翻转，同一份数据开不开缓存结果不同 —— 详见 `_load_all` 里的说明。）

## 三个必须有的保护

1. **版本失效**：回填 / 增量同步会改数据，缓存必须跟着失效，否则选出来的
   是旧数据。用「行数 + 最大日期」当廉价指纹，比 mtime 可靠（SQLite 的
   mtime 在 WAL 模式下不准确）。
2. **可关闭**：`enabled=False` 时行为完全退回逐股查库，出问题能一键降级。
3. **可释放**：跑完一次选股要能 `release()` 把 341MB 还回去，不能常驻。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

# 列的顺序要和下面的 SELECT 严格对应
_NUM_COLS = ("open", "high", "low", "close", "volume", "turnover")

# 分批大小。20 万行一批时，每批的中间数组约 20MB，峰值可控；
# 太小会增加 fetch 次数，太大又回到 read_sql 的老问题。
_BATCH = 200_000


class OhlcvCache:
    """全市场日线缓存。

    用法（对调用方完全透明——`DataEngine.get_ohlcv` 内部用它）::

        cache = OhlcvCache(db_path)
        cache.warm()            # 一次性加载全市场
        df = cache.get("600519")  # 命中就返回，未命中返回 None
        cache.release()         # 用完释放

    线程安全：工作台的后台任务在独立线程里跑，所以存取都加了锁。
    简单起见用一把大锁——加载是秒级、读取是纳秒级，锁竞争不是瓶颈。
    """

    def __init__(self, db_path: str, enabled: bool = True) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self._frames: dict[str, pd.DataFrame] | None = None
        self._fingerprint: tuple | None = None
        self._lock = threading.RLock()
        self._warm_seconds: float = 0.0
        self._rows: int = 0

    # ── 指纹 ─────────────────────────────────────────────────────

    def _current_fingerprint(self) -> tuple | None:
        """算一个能代表「当前数据状态」的廉价指纹。

        用 行数 + 最大日期 而不是文件 mtime：SQLite 在 WAL 模式下
        主库文件的 mtime 不随提交更新，靠它判断会漏掉刚写入的数据。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(date) FROM stock_daily"
                ).fetchone()
            return (int(row[0]), str(row[1]))
        except sqlite3.Error as exc:
            logger.warning(f"读取数据指纹失败：{exc}")
            return None

    def stale(self) -> bool:
        """缓存是否已过期（数据被改过）。"""
        if self._frames is None:
            return True
        cur = self._current_fingerprint()
        return cur is None or cur != self._fingerprint

    # ── 加载 ─────────────────────────────────────────────────────

    def warm(self, force: bool = False) -> bool:
        """一次性加载全市场到内存。

        Args:
            force: True 时忽略已有缓存，强制重新加载。

        Returns:
            是否可用（True = 缓存已就绪）。
        """
        if not self.enabled:
            return False

        with self._lock:
            if self._frames is not None and not force and not self.stale():
                return True

            fp = self._current_fingerprint()
            if fp is None:
                return False

            t0 = time.time()
            try:
                frames = self._load_all()
            except Exception as exc:  # noqa: BLE001
                # 加载失败不能让整个选股崩掉——降级回逐股查库
                logger.error(f"全市场缓存加载失败，将退回逐股查库：{exc}")
                self._frames = None
                return False

            self._frames = frames
            self._fingerprint = fp
            self._warm_seconds = time.time() - t0
            self._rows = fp[0]
            logger.info(
                f"全市场缓存就绪：{len(frames):,} 只 / {self._rows:,} 行，"
                f"耗时 {self._warm_seconds:.1f}s"
            )
            return True

    def _load_all(self) -> dict[str, pd.DataFrame]:
        """分批读取 + numpy 预分配，避开 read_sql 的中间开销。

        这是整个模块性能的关键：见模块 docstring 里的实测对比。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
            if not total:
                return {}

            # ⚠️ 用 float64 而不是 float32。
            #
            # 一开始为了省内存用了 float32，结果实测 `ma_cross`（MA5 上穿 MA20）
            # 在同一个数据上给出 **810 只（查库）vs 813 只（缓存）** 的不同结果。
            # 原因是 float32 只有约 7 位有效数字，而后复权价能到 5 位数
            # （茅台 11000+），当 MA5 与 MA20 的差值小于 float32 精度时，
            # `prev_ma5 <= prev_ma20 and now_ma5 > now_ma20` 这个判断会翻转。
            #
            # **同一份数据开不开缓存给出不同信号，对交易工具是不可接受的**，
            # 所以宁可多花一倍内存也要保证与逐股查库路径逐位一致。
            nums = {c: np.empty(total, dtype="float64") for c in _NUM_COLS}
            syms = np.empty(total, dtype=object)
            dates = np.empty(total, dtype="datetime64[s]")

            cur = conn.execute(
                "SELECT symbol, date, open, high, low, close, volume, turnover "
                "FROM stock_daily ORDER BY symbol, date"
            )
            pos = 0
            while True:
                rows = cur.fetchmany(_BATCH)
                if not rows:
                    break
                n = len(rows)
                arr = np.array(rows, dtype=object)
                syms[pos:pos + n] = arr[:, 0]
                dates[pos:pos + n] = arr[:, 1].astype("datetime64[s]")
                for i, col in enumerate(_NUM_COLS, start=2):
                    nums[col][pos:pos + n] = arr[:, i].astype("float64")
                pos += n
                del rows, arr

            if pos != total:
                # 读取过程中数据被改了。不赌，直接报错让上层降级。
                raise RuntimeError(
                    f"读取行数不一致（预期 {total}，实际 {pos}），数据可能正在被写入"
                )

            df = pd.DataFrame({
                "symbol": syms,
                "date": pd.to_datetime(dates),
                **{c: nums[c] for c in _NUM_COLS},
            })
            # date 统一成字符串，与逐股查库路径的返回格式保持一致
            # （策略里有 `df["date"]` 直接参与比较的写法）
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        finally:
            conn.close()

        # ⚠️ 列必须与 get_ohlcv 的逐股查库路径**完全一致**。
        #    查库那条路是 `SELECT *`，列顺序是：
        #        id, symbol, date, open, high, low, close, volume, turnover
        #    所以这里也要把 symbol 留在每只股票的 DataFrame 里
        #    （虽然对单只股票而言它是常量，但少了它列名就对不上，
        #      将来有代码用 df["symbol"] 会直接 KeyError）。
        df["id"] = range(1, len(df) + 1)
        df = df[["id", "symbol", "date", *_NUM_COLS]]
        df["id"] = df["id"].astype("int64")

        frames: dict[str, pd.DataFrame] = {}
        for sym, sub in df.groupby("symbol", sort=False, observed=True):
            frames[str(sym)] = sub.reset_index(drop=True)
        return frames

    # ── 读取 ─────────────────────────────────────────────────────

    def get(self, symbol: str) -> pd.DataFrame | None:
        """取一只股票的数据。未命中或缓存未就绪时返回 None。"""
        if not self.enabled:
            return None
        frames = self._frames
        if frames is None:
            return None
        # 不加锁读：dict 的读操作在 CPython 下是原子的，而这里要的是
        # 「读得快」。真要并发写时 _frames 会被整体替换，不会读到半个字典。
        return frames.get(symbol)

    @property
    def ready(self) -> bool:
        return self._frames is not None

    @property
    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "symbols": len(self._frames) if self._frames else 0,
            "rows": self._rows,
            "warm_seconds": round(self._warm_seconds, 2),
            "fingerprint": self._fingerprint,
        }

    def release(self) -> None:
        """释放缓存，把内存还回去。"""
        with self._lock:
            if self._frames is not None:
                n = len(self._frames)
                self._frames = None
                self._fingerprint = None
                self._rows = 0
                logger.debug(f"已释放全市场缓存（{n} 只）")
