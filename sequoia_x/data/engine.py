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

# 按日期查（「库里有哪些交易日」「比某天更新的交易日有几个」）如果没有这个索引
# 会全表扫 340 万行，实测 1.75 秒；建了之后 0.24 秒。Web 工作台的数据覆盖页要用。
_CREATE_DATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily (date);
"""

# 记录每只股票的数据来源。不同源的后复权基准不同，同一只股票必须始终用同一个源，
# 否则历史与增量之间会出现假跳空。换源时必须「整只重拉」（见 _rebase_symbol）。
_CREATE_SOURCE_SQL = """
CREATE TABLE IF NOT EXISTS symbol_source (
    symbol     TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    updated_at TEXT
);
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

# ── 多数据源取数（东方财富 / 腾讯 / 新浪）──
#
# 三个源都通过 akshare 调用，对外统一约定：
#   * 价格一律**后复权**（hfq），与项目原设计（baostock adjustflag=1）一致
#   * volume 一律「股」、turnover 一律「元」
#   * 返回列固定为 [symbol, date, open, high, low, close, volume, turnover]
#   * 返回 None 表示**请求失败**（连接异常/限流）；返回空 DataFrame 表示
#     请求成功但该区间确实没有数据（如全为非交易日）——两者必须区分
#
# ⚠️ 三个源的后复权基准互不相同。实测同一只股票同一天（600519 2026-10-09）：
#     东方财富 7951.96 / 新浪 11218.61 / 腾讯 8911.19
#    成交量、成交额三家一致，但价格基准差了 40%。
#    因此**同一只股票绝不能跨源拼接**：每只股票用哪个源，记录在 symbol_source 表，
#    增量更新只认这个源。（跨股票用不同源是安全的——所有策略都只用同股票内部的比值。）

_AK_HIST_COLUMNS = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",   # 单位「手」
    "成交额": "turnover",  # 单位「元」
}

_AK_ROWS = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]

# 支持的数据源（akshare 系）与默认 auto 顺序
AKSHARE_SOURCES = ("eastmoney", "tencent", "sina")
DEFAULT_AUTO_ORDER = "eastmoney,tencent,sina"

# 健康探测用的样本股票（贵州茅台：流动性最好，任何源都一定有数据）
_PROBE_SYMBOL = "600519"


def _prefixed(symbol: str) -> str:
    """纯数字代码转带市场前缀：600519 -> sh600519；000001 -> sz000001。"""
    return ("sh" if symbol.startswith(("6", "9")) else "sz") + symbol


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


def _finish_rows(out: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """统一收尾：补 symbol、规范日期、转数值、剔除无效行。"""
    if out is None or out.empty:
        return pd.DataFrame(columns=_AK_ROWS)
    out = out.copy()
    out["symbol"] = symbol
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"])
    out = out[out["volume"] > 0]
    return out[_AK_ROWS]


def _rows_eastmoney(df, symbol: str) -> pd.DataFrame:
    """东方财富历史行情 -> 规范列。成交量单位是「手」，需 ×100 换成「股」。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=_AK_ROWS)
    df = df.rename(columns=_AK_HIST_COLUMNS)
    need = ["date", "open", "high", "low", "close", "volume", "turnover"]
    if not set(need).issubset(df.columns):
        logger.warning(f"[{symbol}] 东方财富返回列不匹配：{list(df.columns)}")
        return pd.DataFrame(columns=_AK_ROWS)
    out = df[need].copy()
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100
    return _finish_rows(out, symbol)


def _rows_prefixed(df, symbol: str, value_col: str) -> pd.DataFrame:
    """腾讯/新浪历史行情 -> 规范列。成交量已是「股」，成交额在 amount 列。

    注意这两家自带的 ``turnover`` 列是**换手率**，不是成交额；直接用成交额
    覆盖它，避免出现重复列（重复列会让 pandas 把单列当成 DataFrame）。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_AK_ROWS)
    if value_col not in df.columns:
        logger.warning(f"[{symbol}] 返回列缺少 {value_col}：{list(df.columns)}")
        return pd.DataFrame(columns=_AK_ROWS)
    out = df.copy()
    out["turnover"] = pd.to_numeric(out[value_col], errors="coerce")  # 成交额覆盖换手率
    need = ["date", "open", "high", "low", "close", "volume", "turnover"]
    if not set(need).issubset(out.columns):
        logger.warning(f"[{symbol}] 返回列不匹配：{list(df.columns)}")
        return pd.DataFrame(columns=_AK_ROWS)
    return _finish_rows(out[need], symbol)


def _fetch_eastmoney(symbol, start, end, retries=4, delay=0.0):
    """东方财富（akshare stock_zh_a_hist）。"""
    import time

    import akshare as ak

    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol, period="daily",
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust="hfq",
            )
            return pd.DataFrame() if df is None else df
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return None


def _fetch_tencent(symbol, start, end, retries=4, delay=0.0):
    """腾讯（akshare stock_zh_a_hist_tx）。"""
    import time

    import akshare as ak

    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=_prefixed(symbol),
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust="hfq",
            )
            return pd.DataFrame() if df is None else df
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return None


def _fetch_sina(symbol, start, end, retries=4, delay=0.0):
    """新浪（akshare stock_zh_a_daily）。"""
    import time

    import akshare as ak

    for attempt in range(retries):
        if delay:
            time.sleep(delay)
        try:
            df = ak.stock_zh_a_daily(
                symbol=_prefixed(symbol),
                start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                adjust="hfq",
            )
            return pd.DataFrame() if df is None else df
        except Exception:  # noqa: BLE001
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
    return None


def fetch_from_source(source: str, symbol: str, start: str, end: str,
                      retries: int = 4, delay: float = 0.0) -> pd.DataFrame | None:
    """按数据源拉一只股票的后复权日 K，返回规范列。

    **返回 None 表示请求失败**（连接异常/限流），空 DataFrame 表示请求成功但
    区间内没有数据。这个区分至关重要：auto 模式靠它判断某个源是不是被限流了，
    如果把失败当成「无数据」，就会误以为源是好的而一直不切换。
    """
    if source == "eastmoney":
        raw = _fetch_eastmoney(symbol, start, end, retries, delay)
        return None if raw is None else _rows_eastmoney(raw, symbol)
    if source == "tencent":
        raw = _fetch_tencent(symbol, start, end, retries, delay)
        return None if raw is None else _rows_prefixed(raw, symbol, "amount")
    if source == "sina":
        raw = _fetch_sina(symbol, start, end, retries, delay)
        return None if raw is None else _rows_prefixed(raw, symbol, "amount")
    raise ValueError(f"未知数据源：{source}")


def probe_source(source: str, timeout_retries: int = 2) -> bool:
    """探测某个数据源是否可用（用样本股票拉最近几天数据）。"""
    from datetime import date, timedelta

    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=20)).strftime("%Y-%m-%d")
    try:
        rows = fetch_from_source(source, _PROBE_SYMBOL, start, end, timeout_retries, 0.0)
    except Exception:  # noqa: BLE001
        return False
    return rows is not None and not rows.empty



class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.data_source: str = (settings.data_source or "baostock").lower().strip()
        self.ak_workers: int = max(1, int(settings.ak_workers or 4))
        self.ak_delay: float = max(0.0, float(settings.ak_delay or 0.0))
        self.rebase_stale_days: int = max(1, int(settings.rebase_stale_days or 2))
        self.auto_order: list[str] = self._parse_auto_order(
            getattr(settings, "auto_source_order", None)
        )
        self._init_db()
        logger.info(f"数据源：{self.data_source}" + (
            f"（auto 顺序：{self.auto_order}）" if self.data_source == "auto" else ""
        ))

    @staticmethod
    def _parse_auto_order(raw: str | None) -> list[str]:
        """解析 auto 模式的数据源顺序，过滤掉不认识的名字。"""
        text = raw or DEFAULT_AUTO_ORDER
        order = [s.strip().lower() for s in text.split(",") if s.strip()]
        order = [s for s in order if s in AKSHARE_SOURCES]
        return order or list(AKSHARE_SOURCES)

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_DATE_INDEX_SQL)
            conn.execute(_CREATE_SOURCE_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    # ── 数据源记录 ──

    def _get_symbol_source(self, symbol: str) -> str | None:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source FROM symbol_source WHERE symbol = ?", (symbol,)
            ).fetchone()
        return row[0] if row else None

    def _set_symbol_source(self, symbol: str, source: str) -> None:
        from datetime import datetime

        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO symbol_source (symbol, source, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET source = excluded.source, "
                "updated_at = excluded.updated_at",
                (symbol, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()

    def get_source_summary(self) -> dict[str, int]:
        """统计各数据源覆盖了多少只股票。"""
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM symbol_source GROUP BY source"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

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
        """增量同步：把每只股票缺的最新交易日补上（后复权），写入 SQLite。

        - baostock 源：多进程并行拉取
        - akshare 系（东方财富/腾讯/新浪/auto）：逐只按缺失区间拉取。

        注意 akshare 系**不能用「全市场快照」**：快照给的是不复权的实时价，
        而库里的历史是后复权价，直接写进去会造出巨大的假跳空（茅台差 5 倍）。
        所以增量更新走的是「按缺失日期拉后复权」这条与回填完全一致的路。
        """
        if self.data_source != "baostock":
            symbols = self.get_local_symbols()
            if not symbols:
                logger.warning("本地无股票数据，请先执行 --backfill")
                return 0
            before = self._count_rows()
            self._update_symbols(symbols)
            added = self._count_rows() - before
            logger.info(f"增量同步完成，新增 {added} 行")
            return added

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
        if self.data_source != "baostock":
            self._run_pipeline(symbols)
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
        """获取全市场 A 股代码列表（按数据源走 baostock 或 akshare 系）。"""
        if self.data_source != "baostock":
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

    # ── 多数据源实现（东方财富 / 腾讯 / 新浪 / auto）──

    def _count_rows(self) -> int:
        with _connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]

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

    def _delete_symbol(self, symbol: str) -> None:
        """删除某只股票的全部数据（换源整只重拉前调用）。"""
        with _connect(self.db_path) as conn:
            conn.execute("DELETE FROM stock_daily WHERE symbol = ?", (symbol,))
            conn.execute("DELETE FROM symbol_source WHERE symbol = ?", (symbol,))
            conn.commit()

    def _get_all_symbols_akshare(self) -> list[str]:
        """通过 akshare 获取全市场 A 股代码列表。

        这个接口走的是东方财富的「代码表」而不是行情 API，实测行情 API 被限流时
        它仍然可用。万一它也挂了，退化为「只更新本地已有股票」，不至于完全停摆。
        """
        import akshare as ak

        df = _ak_retry(lambda: ak.stock_info_a_code_name(), "全市场代码表")
        if df is None or df.empty or "code" not in df.columns:
            local = self.get_local_symbols()
            logger.error(f"获取股票列表失败（akshare），退化为本地已有的 {len(local)} 只")
            return local

        codes = df["code"].astype(str).str.zfill(6)
        # 只保留 A 股：沪市 6xx / 科创板 688、深市 0xx、创业板 3xx、北交所 4xx/8xx
        codes = [c for c in codes.tolist() if c[0] in "60384"]
        logger.info(f"获取股票列表完成，共 {len(codes)} 只")
        return codes

    # ── 交易日历 ──

    def _trade_days(self) -> list:
        """交易日历（进程内缓存一次）。"""
        cached = getattr(self, "_trade_days_cache", None)
        if cached is None:
            import akshare as ak

            cal = _ak_retry(lambda: ak.tool_trade_date_hist_sina(), "交易日历", retries=3)
            if cal is None or cal.empty:
                cached = []
            else:
                cached = sorted(pd.to_datetime(cal["trade_date"]).dt.date.tolist())
            self._trade_days_cache = cached
        return cached

    def _last_trading_day(self, cutoff) -> str:
        """返回 <= cutoff 的最近一个交易日。

        用交易日而不是自然日做「已是最新」的判定，否则周末/节假日会把所有股票
        都判成有缺口，每次运行都白跑一遍。拿不到日历时退化为 cutoff 本身。
        """
        days = [d for d in self._trade_days() if d <= cutoff]
        if days:
            return max(days).strftime("%Y-%m-%d")
        logger.warning("交易日历不可用，改用自然日作为回填终点")
        return cutoff.strftime("%Y-%m-%d")

    def _stale_trading_days(self, last_date: str, target_end: str) -> int:
        """last_date 到 target_end 之间还差几个交易日。"""
        from datetime import date

        days = self._trade_days()
        if not days:
            return 0  # 拿不到日历就不换源，保守处理
        d0 = date.fromisoformat(last_date)
        d1 = date.fromisoformat(target_end)
        return sum(1 for d in days if d0 < d <= d1)

    # ── 数据源调度 ──

    def _source_candidates(self) -> list[str]:
        """本次运行可用的数据源候选（按优先级）。"""
        if self.data_source == "auto":
            return list(self.auto_order)
        if self.data_source in AKSHARE_SOURCES:
            return [self.data_source]
        return []

    def _plan_symbol(self, symbol, target_end, active, healthy, candidates):
        """决定某只股票「用哪些源、从哪天开始拉」。

        返回 (symbol, start, sources, rebase)；不需要处理则返回 None。
        ``sources`` 是**候选源列表，按顺序试**——某个源对这只股票恰好没有数据
        （实测 689009 在新浪拿不到、腾讯能拿到），就自动换下一个源。

        关键规则——**同一只股票绝不跨源拼接**：
        - 库里没有任何数据：所有可用源都可以试（跨股票用不同源是安全的）
        - 已有数据且记录了源：只认这个源（除非它长期不可用，见下）
        - 已有数据但没记录源（老库迁移）：按主源处理
        - 记录的源不可用，且已经陈旧到 rebase_stale_days 个交易日：
          换源「整只重拉」（先删后拉，保证同股票内部自洽），同样可以按顺序试
        """
        from datetime import date, timedelta

        last = self._get_last_date(symbol)
        if last and last >= target_end:
            return None

        start = self.start_date
        if last:
            start = (date.fromisoformat(last) + timedelta(days=1)).strftime("%Y-%m-%d")

        src = self._get_symbol_source(symbol)
        if src is None and last is not None:
            src = candidates[0]  # 老数据没有来源记录，按主源处理

        if src is None:
            return (symbol, start, list(healthy), False)  # 全新股票：哪个源都行
        if src in healthy:
            return (symbol, start, [src], False)          # 沿用原源
        if last is not None and self._stale_trading_days(last, target_end) >= self.rebase_stale_days:
            logger.debug(f"[{symbol}] 原数据源 {src} 不可用且已陈旧，换源整只重拉")
            return (symbol, self.start_date, list(healthy), True)
        return None

    def _store(
        self,
        symbol,
        rows,
        source: str,
        rebase: bool = False,
        replace_range: tuple | None = None,
    ) -> None:
        """把某只股票的数据和它的来源写在同一个事务里。

        顺序很重要：**先记来源，再写数据**。如果反过来（先写数据、后记来源），
        进程在两步之间被杀就会留下「有数据但没有来源记录」的股票，下次增量会按
        主源去拉，而这份数据其实来自另一个源 —— 同股跨源拼接，假跳空。

        反过来先记来源则最多留下「有来源、没数据」，那只股票会被当成全新股票
        重新拉取，不会污染任何已有数据。

        Args:
            rebase: 整只重拉——先删掉这只股票的全部历史再写。
            replace_range: 只覆盖这个日期区间 ``(start, end)``。
                必须用它而不是直接 INSERT：``stock_daily`` 上有 ``UNIQUE(symbol, date)``，
                区间内只要有一天已存在，普通 INSERT 就会抛 IntegrityError，
                整个事务（包括这一批的所有行）会被回滚，结果一行都写不进去。
        """
        from datetime import datetime

        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO symbol_source (symbol, source, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET source = excluded.source, "
                "updated_at = excluded.updated_at",
                (symbol, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            if rebase:
                conn.execute("DELETE FROM stock_daily WHERE symbol = ?", (symbol,))
            elif replace_range:
                conn.execute(
                    "DELETE FROM stock_daily WHERE symbol = ? AND date >= ? AND date <= ?",
                    (symbol, replace_range[0], replace_range[1]),
                )
            if not rows.empty:
                rows.to_sql(
                    "stock_daily", conn, if_exists="append",
                    index=False, method="multi", chunksize=500,
                )
            conn.commit()

    def _fetch_and_store(
        self,
        symbol,
        start,
        sources,
        target_end,
        rebase=False,
        replace_range=None,
    ) -> bool:
        """按候选源顺序拉一段并写入，第一个成功的源就用它。

        ``sources`` 是列表：某个源对这只股票恰好没有数据（返回 None）时自动换
        下一个源。**只有「整只重拉」或「全新股票」才会给出多个候选源**——
        已有数据的股票只给一个源，避免同股跨源拼接。
        """
        for source in sources:
            rows = fetch_from_source(source, symbol, start, target_end, 4, self.ak_delay)
            if rows is None:
                logger.debug(f"[{symbol}] 数据源 {source} 请求失败，尝试下一个源")
                continue
            try:
                self._store(symbol, rows, source, rebase, replace_range)
            except sqlite3.IntegrityError:
                pass  # 已存在，算成功
            return True
        return False

    def _process_batch(self, todo: list) -> tuple[int, int]:
        """并发处理一批，返回 (成功, 失败)。

        每一项是 ``(symbol, start, sources, rebase, target_end[, replace_range])``，
        第 6 项只有「覆盖指定区间」时才有。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        ok = failed = done = 0
        with ThreadPoolExecutor(max_workers=self.ak_workers) as pool:
            futures = {}
            for item in todo:
                symbol, start, sources, rebase, target_end = item[:5]
                replace_range = item[5] if len(item) > 5 else None
                futures[pool.submit(
                    self._fetch_and_store, symbol, start, sources,
                    target_end, rebase, replace_range,
                )] = symbol
            for fut in as_completed(futures):
                symbol = futures[fut]
                done += 1
                try:
                    good = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{symbol}] 处理异常：{exc}")
                    good = False
                if good:
                    ok += 1
                else:
                    failed += 1
                if done % 200 == 0:
                    logger.info(f"  进度 {done}/{len(todo)}  成功 {ok} 失败 {failed}")
        return ok, failed

    def _run_pipeline(self, symbols: list[str], rounds: int = 5) -> None:
        """回填 / 增量的统一入口：多轮补齐 + auto 自动换源。

        - 只处理「缺最新交易日数据」的股票，中断后可重跑续传
        - 每轮结束后如果失败偏多，就重新探测数据源并切换到可用的那个
        """
        from datetime import date, datetime, timedelta

        candidates = self._source_candidates()
        if not candidates:
            logger.warning(f"数据源 {self.data_source} 不支持增量回填（只有 baostock 走原路径）")
            return

        # 收盘前（15:00）拿到的当日 K 线是未完成的，会污染信号
        now = datetime.now()
        cutoff = date.today() if now.hour >= 15 else date.today() - timedelta(days=1)
        target_end = self._last_trading_day(cutoff)
        logger.info(f"目标交易日：{target_end}；候选数据源：{candidates}")

        healthy = [s for s in candidates if probe_source(s)]
        if not healthy:
            logger.error("所有候选数据源都探测失败，本次不跑（稍后重跑即可续传）")
            return
        logger.info(f"可用数据源：{healthy}")
        active = healthy[0]

        for rnd in range(1, rounds + 1):
            todo = []
            for symbol in symbols:
                plan = self._plan_symbol(symbol, target_end, active, healthy, candidates)
                if plan is not None:
                    todo.append(plan + (target_end,))
            if not todo:
                logger.info(f"完成：全部 {len(symbols)} 只已是最新")
                return

            rebases = sum(1 for t in todo if t[3])
            logger.info(
                f"第 {rnd}/{rounds} 轮：待处理 {len(todo)} 只"
                + (f"（其中 {rebases} 只因原数据源不可用将换源整只重拉）" if rebases else "")
                + f"，{self.ak_workers} 线程并发，间隔 {self.ak_delay}s，当前源 {active}"
            )
            ok, failed = self._process_batch(todo)
            logger.info(f"第 {rnd} 轮结束：成功 {ok} 失败 {failed}")

            if failed == 0:
                logger.info("回填完成，无缺口")
                return

            # auto 模式：失败偏多说明当前源在限流/异常，重新探测并换源
            if len(candidates) > 1 and failed >= max(5, int(len(todo) * 0.3)):
                logger.warning(f"失败偏多（{failed}/{len(todo)}），重新探测数据源...")
                healthy = [s for s in candidates if probe_source(s)]
                if not healthy:
                    logger.error("所有数据源都不可用，本轮中止（可稍后重跑续传）")
                    return
                if healthy[0] != active:
                    logger.warning(f"数据源从 {active} 切换到 {healthy[0]}")
                active = healthy[0]

        logger.warning(f"{rounds} 轮后仍有缺口，可再次运行续传")

    def _update_symbols(self, symbols: list[str]) -> None:
        """增量更新指定股票（把缺的最新交易日补上）。"""
        self._run_pipeline(symbols)

    def market_breadth(self, date: str) -> dict:
        """某天的全市场涨跌统计，给大模型分析当「事实依据」。

        没有这些数字，大模型只能对着几个股票代码空谈；有了涨跌家数、涨跌停家数、
        总成交额，它才能结合当天的市场情绪点评策略选出来的票。

        涨跌幅用「当天收盘 / 上一交易日收盘 - 1」算。涨停用 ±9.8% 近似
        （ST 股其实是 ±5%，这里不区分——这个数字只作为情绪参考，不参与选股）。

        Args:
            date: 目标交易日（YYYY-MM-DD）。

        Returns:
            统计字典；目标日期没有数据时返回 ``{}``。
        """
        with _connect(self.db_path) as conn:
            prev_row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE date < ?", (date,)
            ).fetchone()
            prev = prev_row[0] if prev_row else None
            if not prev:
                return {}
            rows = conn.execute(
                "SELECT a.close, b.close, a.turnover "
                "FROM stock_daily a JOIN stock_daily b ON b.symbol = a.symbol "
                "WHERE a.date = ? AND b.date = ?",
                (date, prev),
            ).fetchall()

        if not rows:
            return {}

        up = down = flat = 0
        limit_up = limit_down = 0
        turnover = 0.0
        total_pct = 0.0
        counted = 0
        for close, prev_close, amount in rows:
            if not prev_close:
                continue
            pct = (close / prev_close - 1) * 100
            total_pct += pct
            counted += 1
            if pct > 0.05:
                up += 1
            elif pct < -0.05:
                down += 1
            else:
                flat += 1
            if pct >= 9.8:
                limit_up += 1
            elif pct <= -9.8:
                limit_down += 1
            if amount:
                turnover += amount

        return {
            "date": date,
            "prev_date": prev,
            "total": counted,
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "turnover_yi": round(turnover / 1e8, 1),   # 亿元
            "avg_pct": round(total_pct / counted, 2) if counted else 0.0,
        }

    # ── 手动数据操作（Web 工作台用）──

    def _probe_candidates(self) -> tuple[list[str], list[str]]:
        """探测数据源，返回 ``(候选列表, 可用列表)``。两个都可能是空列表。"""
        candidates = self._source_candidates()
        if not candidates:
            logger.warning(f"数据源 {self.data_source} 不支持增量回填（只有 baostock 走原路径）")
            return [], []
        healthy = [s for s in candidates if probe_source(s)]
        if not healthy:
            logger.error("所有候选数据源都探测失败，本次不跑（稍后重跑即可续传）")
        return candidates, healthy

    def _target_end(self) -> str:
        """本次回填的目标交易日。

        收盘前（15:00）拿到的当日 K 线是未完成的，会污染信号，所以取昨天。
        """
        from datetime import date, datetime, timedelta

        now = datetime.now()
        cutoff = date.today() if now.hour >= 15 else date.today() - timedelta(days=1)
        return self._last_trading_day(cutoff)

    def force_reload(self, symbols: list[str]) -> dict:
        """整只重拉：先删掉这些股票的全部历史，再从可用源重新拉一遍。

        只有**拿到新数据之后**才会删——``_store`` 里的「先删后写」在同一个事务里，
        所以万一所有数据源都失败，原数据一行都不会丢。换源也是安全的：整只重拉
        之后同股票内部仍然自洽（所有策略用的都是同股票内部的比值）。
        """
        if not symbols:
            return {"total": 0, "ok": 0, "failed": 0}
        _candidates, healthy = self._probe_candidates()
        if not healthy:
            return {"total": len(symbols), "ok": 0, "failed": len(symbols)}

        target_end = self._target_end()
        logger.info(f"整只重拉 {len(symbols)} 只，目标交易日 {target_end}，可用源 {healthy}")
        todo = [(s, self.start_date, list(healthy), True, target_end) for s in symbols]
        ok, failed = self._process_batch(todo)
        logger.info(f"整只重拉结束：成功 {ok} 失败 {failed}")
        return {"total": len(symbols), "ok": ok, "failed": failed}

    def fill_range(self, symbols: list[str], start: str, end: str) -> dict:
        """覆盖式补齐指定日期区间：先删该区间再重写，已有的行会被刷新。

        同一只股票只允许用它记在 ``symbol_source`` 里的那个源——跨源拼出来的
        历史会有假跳空。记录里的源不可用就**跳过**这只股票，绝不换源补一段。
        """
        if not symbols:
            return {"total": 0, "ok": 0, "failed": 0, "skipped": 0}
        _candidates, healthy = self._probe_candidates()
        if not healthy:
            return {"total": len(symbols), "ok": 0, "failed": len(symbols), "skipped": 0}

        todo, skipped = [], 0
        for symbol in symbols:
            src = self._get_symbol_source(symbol)
            if src is None:
                sources = list(healthy)   # 全新股票：哪个源都行
            elif src in healthy:
                sources = [src]           # 必须沿用原源
            else:
                logger.warning(f"[{symbol}] 原数据源 {src} 不可用，跳过（不换源补区间）")
                skipped += 1
                continue
            todo.append((symbol, start, sources, False, end, (start, end)))

        logger.info(
            f"补区间 {start} ~ {end}：待处理 {len(todo)} 只，跳过 {skipped} 只，可用源 {healthy}"
        )
        ok, failed = self._process_batch(todo)
        logger.info(f"补区间结束：成功 {ok} 失败 {failed} 跳过 {skipped}")
        return {"total": len(symbols), "ok": ok, "failed": failed, "skipped": skipped}

    def probe_all_sources(self) -> list[dict]:
        """逐个探测候选数据源，给 Web 工作台的「探测健康度」用。"""
        import time as _time

        labels = {
            "eastmoney": "东方财富", "tencent": "腾讯",
            "sina": "新浪", "baostock": "baostock",
        }
        out = []
        for source in self._source_candidates():
            if source not in AKSHARE_SOURCES:
                out.append({"source": source, "label": labels.get(source, source),
                            "ok": None, "ms": None, "error": "该源不支持增量回填"})
                continue
            t0 = _time.time()
            try:
                ok = bool(probe_source(source))
                err = None if ok else "探测失败（连接异常或被限流）"
            except Exception as exc:  # noqa: BLE001
                ok, err = False, f"{type(exc).__name__}: {exc}"
            out.append({
                "source": source, "label": labels.get(source, source),
                "ok": ok, "ms": int((_time.time() - t0) * 1000), "error": err,
            })
        return out

    def migrate_source_records(self, source: str = "eastmoney") -> int:
        """给「已有数据但没有来源记录」的老股票补上来源。

        建 symbol_source 表之前入库的数据没有来源信息，如果不补，auto 模式可能
        把它们当成全新股票而用别的源去续拉，造成同股票跨源拼接。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily "
                "WHERE symbol NOT IN (SELECT symbol FROM symbol_source)"
            ).fetchall()
            symbols = [r[0] for r in rows]
            if symbols:
                from datetime import datetime

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.executemany(
                    "INSERT OR REPLACE INTO symbol_source (symbol, source, updated_at) "
                    "VALUES (?, ?, ?)",
                    [(s, source, now) for s in symbols],
                )
                conn.commit()
        if symbols:
            logger.info(f"已为 {len(symbols)} 只老数据补记数据源：{source}")
        return len(symbols)
