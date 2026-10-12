"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


@contextmanager
def _connect(db_path: str):
    """打开 SQLite 连接，退出时显式关闭。

    必须显式 close：`with sqlite3.connect(...)` 只负责「成功提交 / 异常回滚」，
    并不关闭连接；而 sqlite3.Connection 存在引用环，要等 GC 才会释放。
    在 Windows 上这会表现为数据库文件被持续锁定（临时目录删不掉、文件无法
    移动或备份），并让长跑进程累积大量句柄。Linux 能删除已打开的文件，
    所以上游代码没有暴露这个问题。
    """
    conn = sqlite3.connect(db_path)
    try:
        with conn:  # 保持原有的提交/回滚语义
            yield conn
    finally:
        conn.close()


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。"""
    import baostock as bs
    bs.login()
    results = []
    for symbol, bs_code, start, end in tasks:
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="1",  # 后复权
        )
        if rs.error_code != "0":
            continue
        while rs.next():
            results.append([symbol] + rs.get_row_data())
    bs.logout()
    return results


# ── akshare（东方财富）取数 ──
#
# 东方财富接口对高频请求会瞬时断连：失败表现为 requests 抛
# ConnectionError("Remote end closed connection without response") 或
# ProxyError（本机若开了系统代理会经代理转发）。实测约 10%~30% 的请求会
# 随机失败，但**重试即可恢复**，所以下面所有取数都带指数退避重试。
# 注意：东方财富没有全市场历史批量接口，历史回填只能逐只拉取。

_AK_HIST_COLUMNS = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",   # 单位「手」
    "成交额": "turnover",  # 单位「元」
}

_AK_SPOT_COLUMNS = {
    "代码": "symbol",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "最新价": "close",
    "成交量": "volume",   # 单位「手」
    "成交额": "turnover",  # 单位「元」
}

_AK_ROWS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]


def _ak_retry(fn, what: str, retries: int = 4, base_delay: float = 1.0):
    """带指数退避地调用 akshare，全部失败返回 None。"""
    import time

    last: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
    logger.warning(f"[akshare] {what} 连续 {retries} 次失败：{type(last).__name__}: {last}")
    return None


def _ak_to_rows(df, symbol: str) -> pd.DataFrame:
    """把 akshare 的历史行情 DataFrame 规范化成 stock_daily 的列。

    成交量单位换算：akshare 是「手」，baostock 是「股」，统一 ×100，
    保持与项目原设计（及可能的回退）一致。所有策略只用同股票内部的
    量比，换算与否不影响信号，但统一单位更不容易踩坑。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_AK_ROWS)

    df = df.rename(columns=_AK_HIST_COLUMNS)
    need = ["date", "open", "high", "low", "close", "volume", "turnover"]
    if not set(need).issubset(df.columns):
        logger.warning(f"[{symbol}] akshare 返回列不匹配：{list(df.columns)}")
        return pd.DataFrame(columns=_AK_ROWS)

    out = df[need].copy()
    out["symbol"] = symbol
    out["date"] = out["date"].astype(str)
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["volume"] = out["volume"] * 100
    out = out.dropna(subset=["close"])
    out = out[out["volume"] > 0]
    return out[_AK_ROWS]


def _ak_fetch_history(
    symbol: str, start: str, end: str, retries: int = 4, delay: float = 0.0
) -> pd.DataFrame | None:
    """拉单只股票的后复权日 K（akshare）。

    返回 None 表示**请求失败**；返回空 DataFrame 表示请求成功但该区间没有
    数据（例如区间全是非交易日）——两者必须区分，否则「无新数据」会被
    当成失败而反复重试。
    """
    import time

    import akshare as ak

    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust="hfq",
            )
            if df is None:
                return pd.DataFrame()
            return df
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return None


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.data_source: str = (settings.data_source or "baostock").lower()
        self.ak_workers: int = max(1, int(settings.ak_workers or 4))
        self.ak_delay: float = max(0.0, float(settings.ak_delay or 0.0))
        self._init_db()
        logger.info(f"数据源：{self.data_source}")

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with _connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        if self.data_source == "akshare":
            return self._sync_today_akshare()

        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(8, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with _connect(self.db_path) as conn:
            for d in df["date"].unique().tolist():
                conn.execute("DELETE FROM stock_daily WHERE date = ?", (d,))
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi", chunksize=500)
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        if self.data_source == "akshare":
            self._backfill_akshare(symbols)
            return

        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    bs.logout()
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            bs.logout()
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    continue

                if not rows:
                    skipped += 1
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with _connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            bs.logout()

        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        if self.data_source == "akshare":
            return self._get_all_symbols_akshare()

        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])  # 提取纯数字代码
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def get_local_symbols(self) -> list[str]:
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]

    # ── akshare（东方财富）实现 ──

    def _write_rows(self, df: pd.DataFrame) -> int:
        """把规范化的行写入 stock_daily（同 symbol+date 冲突则忽略）。"""
        if df is None or df.empty:
            return 0
        with _connect(self.db_path) as conn:
            df.to_sql(
                "stock_daily", conn, if_exists="append",
                index=False, method="multi", chunksize=500,
            )
            conn.commit()
        return len(df)

    def _get_all_symbols_akshare(self) -> list[str]:
        """通过 akshare 获取全市场 A 股代码列表。"""
        import akshare as ak

        df = _ak_retry(lambda: ak.stock_info_a_code_name(), "全市场代码表")
        if df is None or df.empty:
            logger.error("获取股票列表失败（akshare）")
            return []

        codes = df["code"].astype(str).str.zfill(6)
        # 只保留 A 股：沪市 6xx / 科创板 688、深市 0xx、创业板 3xx、北交所 4xx/8xx
        codes = [c for c in codes.tolist() if c[0] in "60384"]
        logger.info(f"获取股票列表完成，共 {len(codes)} 只")
        return codes

    def _last_trading_day(self, cutoff) -> str:
        """返回 <= cutoff 的最近一个交易日（akshare 交易日历）。

        拿不到日历时退化为 cutoff 本身。用交易日而非自然日做「已是最新」的
        判定，否则周末/节假日会把所有股票都判成有缺口，每次运行都白跑一遍。
        """
        import akshare as ak

        cal = _ak_retry(lambda: ak.tool_trade_date_hist_sina(), "交易日历", retries=3)
        if cal is not None and not cal.empty:
            days = pd.to_datetime(cal["trade_date"]).dt.date
            days = [d for d in days if d <= cutoff]
            if days:
                return max(days).strftime("%Y-%m-%d")
        logger.warning("交易日历获取失败，改用自然日作为回填终点")
        return cutoff.strftime("%Y-%m-%d")

    def _backfill_akshare(self, symbols: list[str]) -> None:
        """通过 akshare（东方财富）回填历史日 K 线（后复权）。

        容错机制：
        - 单只失败重试 4 次（指数退避）；东方财富会随机瞬时断连，重试即可恢复
        - ``ak_workers`` 个线程并发（默认 4，不宜更高）
        - 已入库的自动 skip，中断后可重跑续传
        - 多轮补齐：一轮跑完仍有缺口就再来一轮，最多 5 轮
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import date, datetime, timedelta

        # 收盘前（15:00）东方财富返回的当日 K 线是未完成的，拉进来会污染信号。
        # 因此以「最近一个已收盘的交易日」为回填终点，当天数据交给收盘后的每日同步。
        now = datetime.now()
        cutoff = date.today() if now.hour >= 15 else date.today() - timedelta(days=1)
        target_end = self._last_trading_day(cutoff)
        logger.info(f"回填终点（最近已完成交易日）：{target_end}")

        max_rounds = 5

        for rnd in range(1, max_rounds + 1):
            todo: list[tuple[str, str]] = []
            for symbol in symbols:
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= target_end:
                    continue
                start = self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
                todo.append((symbol, start))

            if not todo:
                logger.info(f"回填完成：全部 {len(symbols)} 只已是最新")
                return

            logger.info(
                f"第 {rnd}/{max_rounds} 轮：待补 {len(todo)} 只，"
                f"{self.ak_workers} 线程并发，间隔 {self.ak_delay}s..."
            )
            ok = failed = 0
            done = 0

            with ThreadPoolExecutor(max_workers=self.ak_workers) as pool:
                futures = {
                    pool.submit(
                        _ak_fetch_history, symbol, start, target_end, 4, self.ak_delay
                    ): symbol
                    for symbol, start in todo
                }
                for fut in as_completed(futures):
                    symbol = futures[fut]
                    done += 1
                    try:
                        raw = fut.result()
                    except Exception:  # noqa: BLE001
                        raw = None

                    if raw is None:
                        failed += 1  # 请求本身失败
                    else:
                        rows = _ak_to_rows(raw, symbol)
                        if rows.empty:
                            ok += 1  # 区间内确实没有新数据（如全为非交易日）
                        else:
                            try:
                                self._write_rows(rows)
                                ok += 1
                            except sqlite3.IntegrityError:
                                ok += 1  # 已存在，算成功
                            except Exception as exc:  # noqa: BLE001
                                failed += 1
                                logger.warning(f"[{symbol}] 写入失败：{exc}")

                    if done % 200 == 0:
                        logger.info(f"  进度 {done}/{len(todo)}  成功 {ok} 失败 {failed}")

            logger.info(f"第 {rnd} 轮结束：成功 {ok} 失败 {failed}")
            if failed == 0:
                logger.info("回填完成，无缺口")
                return

        logger.warning(f"{max_rounds} 轮后仍有缺口，可再次运行 --backfill 续传")

    def _sync_today_akshare(self) -> int:
        """通过 akshare 拉取当日全市场快照，写入 stock_daily。

        用「全市场快照」而不是逐只查询：一次调用覆盖所有股票（约 56 页），
        比逐只拉 5000+ 次快两个数量级。
        """
        from datetime import date, datetime

        import akshare as ak

        now = datetime.now()
        today_str = date.today().strftime("%Y-%m-%d")

        # 交易日校验：非交易日快照返回的是上一交易日的数据，写进去会造出假的交易日
        cal = _ak_retry(lambda: ak.tool_trade_date_hist_sina(), "交易日历")
        if cal is not None and not cal.empty:
            trade_days = set(pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d"))
            if today_str not in trade_days:
                logger.info(f"{today_str} 非交易日，跳过当日同步")
                return 0

        # 收盘前快照是未完成的 K 线，写进去会污染信号
        if now.hour < 15:
            logger.info(f"当前 {now:%H:%M} 尚未收盘，跳过当日同步（避免写入不完整 K 线）")
            return 0

        spot = _ak_retry(lambda: ak.stock_zh_a_spot_em(), "全市场快照", retries=5)
        if spot is None or spot.empty:
            logger.info("无新数据（快照获取失败）")
            return 0

        df = spot.rename(columns=_AK_SPOT_COLUMNS)
        need = ["symbol", "open", "high", "low", "close", "volume", "turnover"]
        if not set(need).issubset(df.columns):
            logger.error(f"快照列不匹配：{list(spot.columns)}")
            return 0

        out = df[need].copy()
        out["symbol"] = out["symbol"].astype(str).str.zfill(6)
        out["date"] = today_str
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out["volume"] = out["volume"] * 100  # 手 -> 股
        out = out.dropna(subset=["close"])
        out = out[out["volume"] > 0]
        out = out[_AK_ROWS]

        if out.empty:
            logger.info("无新数据（快照为空）")
            return 0

        count = len(out)
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM stock_daily WHERE date = ?", (today_str,))
            out.to_sql(
                "stock_daily", conn, if_exists="append",
                index=False, method="multi", chunksize=500,
            )
            conn.commit()

        logger.info(f"sync_today_bulk(akshare): 写入 {count} 条数据（{today_str}）")
        return count
