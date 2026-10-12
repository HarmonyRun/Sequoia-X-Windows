"""Sequoia-X Web 工作台（零第三方依赖，纯 Python 标准库实现）。

为什么不用 FastAPI/Flask：这台机器装依赖要走代理，而工作台只是本机自用，
标准库的 ThreadingHTTPServer 完全够用，且不引入任何安装风险、不污染
pyproject.toml 的依赖清单。

启动：
    python -m webui.server
或
    run-webui.cmd

默认只监听 127.0.0.1:8899（仅本机可访问，不对外暴露）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── 把仓库根目录加进 sys.path，并切到根目录（.env 与 data/ 都是相对路径） ──
REPO = Path(__file__).resolve().parent.parent
os.chdir(REPO)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from sequoia_x.core.config import get_settings  # noqa: E402
from sequoia_x.data.engine import DataEngine  # noqa: E402
from sequoia_x.pipeline import run_strategies, strategy_meta  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = REPO / "data"
LOGS_DIR = REPO / "logs"
SIGNALS_CACHE = DATA_DIR / "signals_cache.json"
NAME_CACHE = DATA_DIR / "name_cache.json"

HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBUI_PORT", "8899"))

log = logging.getLogger("webui")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ══════════════════════════════════════════════════════════════════
#  基础工具
# ══════════════════════════════════════════════════════════════════

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 板块划分（按代码前缀）。顺序重要：先匹配的先命中。
_BOARD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("688", "689"), "科创板"),
    (("600", "601", "603", "605"), "沪市主板"),
    (("000", "001"), "深市主板"),
    (("002", "003"), "深市中小板"),
    (("300", "301", "302"), "创业板"),
    (("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873", "874", "875", "920"), "北交所"),
]


def board_of(symbol: str) -> str:
    for prefixes, name in _BOARD_RULES:
        if symbol.startswith(prefixes):
            return name
    return "其它"


def _json_default(obj):
    if isinstance(obj, (datetime,)):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(obj, "item"):  # numpy 标量
        return obj.item()
    return str(obj)


# ══════════════════════════════════════════════════════════════════
#  股票名称表（缓存到 data/name_cache.json，重启后离线也能用）
# ══════════════════════════════════════════════════════════════════

_name_lock = threading.Lock()
_name_map: dict[str, str] | None = None
_name_loaded_at: float = 0.0


def get_name_map(force: bool = False) -> dict[str, str]:
    """返回 {代码: 名称}。优先读本地缓存，超过 7 天才重新拉。"""
    global _name_map, _name_loaded_at
    with _name_lock:
        if _name_map is not None and not force:
            return _name_map

        # 1) 本地缓存
        if NAME_CACHE.exists() and not force:
            try:
                payload = json.loads(NAME_CACHE.read_text(encoding="utf-8"))
                if payload.get("names"):
                    _name_map = payload["names"]
                    _name_loaded_at = payload.get("saved_at", 0)
                    age_days = (time.time() - _name_loaded_at) / 86400
                    if age_days < 7:
                        log.info(f"股票名称表：用本地缓存 {len(_name_map)} 条（{age_days:.1f} 天前）")
                        return _name_map
            except Exception as exc:  # noqa: BLE001
                log.warning(f"读取名称缓存失败：{exc}")

        # 2) 在线拉一次
        try:
            import akshare as ak

            df = ak.stock_info_a_code_name()
            names = {
                str(c).zfill(6): str(n)
                for c, n in zip(df["code"], df["name"])
            }
            if names:
                _name_map = names
                _name_loaded_at = time.time()
                NAME_CACHE.parent.mkdir(parents=True, exist_ok=True)
                NAME_CACHE.write_text(
                    json.dumps({"saved_at": _name_loaded_at, "names": names}, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info(f"股票名称表：在线获取 {len(names)} 条")
                return _name_map
        except Exception as exc:  # noqa: BLE001
            log.warning(f"在线获取股票名称表失败：{exc}")

        # 3) 兜底：旧的本地缓存（哪怕过期）
        if _name_map is None:
            _name_map = {}
        return _name_map


# ══════════════════════════════════════════════════════════════════
#  引擎与数据查询
# ══════════════════════════════════════════════════════════════════

_engine_lock = threading.Lock()
_engine: DataEngine | None = None


def get_engine() -> DataEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = DataEngine(get_settings())
        return _engine


_coverage_lock = threading.Lock()
_coverage_cache: dict | None = None
_coverage_at: float = 0.0

_dbstats_cache: dict | None = None
_dbstats_at: float = 0.0


def get_coverage(force: bool = False) -> dict:
    """每只股票的行数、最新日期、数据源、板块。带 300 秒缓存。

    重建一次要扫 340 万行（约 0.8 秒），所以缓存久一点；需要立刻刷新时用
    ``force=True``（例如回填任务跑完之后）。
    """
    global _coverage_cache, _coverage_at
    with _coverage_lock:
        if _coverage_cache is not None and not force and time.time() - _coverage_at < 300:
            return _coverage_cache

        import sqlite3

        settings = get_settings()
        db = REPO / settings.db_path
        conn = sqlite3.connect(str(db))
        try:
            all_dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM stock_daily ORDER BY date"
            ).fetchall()]
            rows = conn.execute(
                "SELECT symbol, COUNT(*), MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()
            sources = dict(conn.execute(
                "SELECT symbol, source FROM symbol_source"
            ).fetchall())
        finally:
            conn.close()

        names = get_name_map()
        # stale_days = 「库里比它更新的交易日有几个」。用「日期 → 序号」查表算，
        # 不要写成 sum(1 for d in all_dates if d > last)——那是 5224×669 次比较，
        # 每次刷新要 2.4 秒。查表是 O(1)。
        date_idx = {d: i for i, d in enumerate(all_dates)}
        last_pos = len(all_dates) - 1
        items = []
        for symbol, cnt, last in rows:
            later = last_pos - date_idx.get(last, last_pos)
            items.append({
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "last_date": last,
                "stale_days": later,
                "rows": cnt,
                "source": sources.get(symbol, ""),
                "board": board_of(symbol),
            })
        items.sort(key=lambda x: (-x["stale_days"], x["symbol"]))

        boards: dict[str, int] = {}
        for it in items:
            boards[it["board"]] = boards.get(it["board"], 0) + 1

        src_count: dict[str, int] = {}
        for it in items:
            src_count[it["source"]] = src_count.get(it["source"], 0) + 1

        date_count: dict[str, int] = {}
        for it in items:
            date_count[it["last_date"]] = date_count.get(it["last_date"], 0) + 1

        _coverage_cache = {
            "items": items,
            "all_dates": all_dates,
            "boards": boards,
            "sources": src_count,
            "last_dates": date_count,
            "max_date": all_dates[-1] if all_dates else None,
        }
        _coverage_at = time.time()
        return _coverage_cache


def db_stats() -> dict:
    """库统计。COUNT(*) 要扫 340 万行（约 0.6 秒），所以缓存 30 秒。"""
    global _dbstats_cache, _dbstats_at
    if _dbstats_cache is not None and time.time() - _dbstats_at < 30:
        return _dbstats_cache

    import sqlite3

    settings = get_settings()
    db = REPO / settings.db_path
    conn = sqlite3.connect(str(db))
    try:
        symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
        lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
    finally:
        conn.close()
    size_mb = round(db.stat().st_size / 1048576, 1) if db.exists() else 0.0
    _dbstats_cache = {
        "path": settings.db_path,
        "size_mb": size_mb,
        "symbols": symbols,
        "target_symbols": 5224,
        "rows": rows,
        "min_date": lo,
        "max_date": hi,
    }
    _dbstats_at = time.time()
    return _dbstats_cache


# schtasks 的返回码含义（十进制）。0x41303 等其实都是「成功」类别，
# 直接按数字判断会误报成「失败」。
_SCHTASKS_RESULT = {
    "0": ("成功", True),
    "267009": ("正在运行", True),   # 0x41301 SCHED_S_TASK_RUNNING
    "267011": ("尚未运行", None),   # 0x41303 SCHED_S_TASK_HAS_NOT_RUN
    "267014": ("已被终止", False),  # 0x41306 SCHED_S_TASK_TERMINATED
    "267010": ("已禁用", None),     # 0x41302 SCHED_S_TASK_DISABLED
    "267012": ("无更多运行", None),  # 0x41304 SCHED_S_TASK_NO_MORE_RUNS
    "267013": ("未计划", None),     # 0x41305 SCHED_S_TASK_NOT_SCHEDULED
}


def _result_text(raw: str | None) -> tuple[str | None, bool | None]:
    """把 schtasks 的「上次结果」翻译成人话 + 是否算成功。"""
    if raw is None or raw == "":
        return None, None
    key = raw.strip()
    if key in _SCHTASKS_RESULT:
        return _SCHTASKS_RESULT[key]
    # 非零且不在表里，才是真失败
    try:
        return ("失败", int(key) == 0)
    except ValueError:
        return (key, None)


# schtasks 输出的键名（小写）：中英文都认
_TASK_KEYS = {
    "state": ("status", "状态"),
    "next_run": ("next run time", "下次运行时间"),
    "last_run": ("last run time", "上次运行时间"),
    "last_result": ("last result", "上次结果"),
}

# 「状态」字段的可能取值（小写）→ 统一成英文枚举，前端按英文判断
_STATE_WORDS = {
    "ready": "Ready", "running": "Running", "disabled": "Disabled",
    "queued": "Queued", "unknown": "Unknown",
    "就绪": "Ready", "正在运行": "Running", "已禁用": "Disabled",
    "排队中": "Queued", "未知": "Unknown",
}

_tasks_cache: list[dict] | None = None
_tasks_at: float = 0.0


def _decode_output(raw: bytes) -> str:
    """探测 schtasks 输出的编码：逐个试，哪个能解出可识别的键名就用哪个。"""
    for enc in ("utf-8", "gbk", "mbcs", "latin-1"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        low = text.lower()
        if "status" in low or "状态" in text:
            return text
    return raw.decode("utf-8", errors="replace")


def _task_field(key: str) -> str | None:
    """把一行输出的键名映射到字段名（大小写不敏感、中英文都认）。"""
    k = key.strip().lower()
    for field, aliases in _TASK_KEYS.items():
        if k in aliases:
            return field
    return None


# ── 停牌核查 ──
# 库里总有十几只股票的「最新日期」落后于大盘。这既可能是真停牌，也可能是
# 我们的数据源漏了数据，光看数据库分不出来。用新浪实时行情可以确定：
# 状态码字段 00 = 正常交易，03 = 停牌；停牌股 open/high/low/volume 全是 0。
_SINA_HQ = "http://hq.sinajs.cn/list="
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}


def _sina_code(symbol: str) -> str:
    if symbol[0] in ("6", "5", "9"):
        return "sh" + symbol
    if symbol[0] in ("0", "2", "3"):
        return "sz" + symbol
    return "bj" + symbol


def check_suspended(symbols: list[str]) -> list[dict]:
    """核查这些股票是「真停牌」还是「我们漏了数据」。

    返回每只股票一条记录：``suspended=True`` 表示停牌（数据没问题），
    ``suspended=False`` 表示当天有成交——那就是我们库里缺数据，需要回填。
    """
    import requests

    out: list[dict] = []
    for i in range(0, len(symbols), 60):
        chunk = symbols[i: i + 60]
        try:
            resp = requests.get(
                _SINA_HQ + ",".join(_sina_code(s) for s in chunk),
                headers=_SINA_HEADERS, timeout=15,
            )
            resp.encoding = "gbk"
            text = resp.text
        except Exception as exc:  # noqa: BLE001
            out.extend({"symbol": s, "suspended": None, "note": f"查询失败：{exc}"} for s in chunk)
            continue

        parsed: dict[str, list[str]] = {}
        for line in text.strip().splitlines():
            if '="' not in line:
                continue
            code = line.split("hq_str_")[1].split("=")[0][2:]
            parsed[code] = line.split('"')[1].split(",")

        for s in chunk:
            f = parsed.get(s)
            if not f or len(f) < 33 or not f[0]:
                out.append({"symbol": s, "name": "", "suspended": None,
                            "note": "行情无此代码（可能已退市）"})
                continue
            try:
                volume = float(f[8] or 0)
                price = float(f[3] or 0)
                prev = float(f[2] or 0)
            except ValueError:
                volume, price, prev = 0.0, 0.0, 0.0
            flag = f[32]
            suspended = flag == "03" or (volume == 0 and price in (0.0, prev))
            out.append({
                "symbol": s,
                "name": f[0],
                "suspended": suspended,
                "flag": flag,
                "quote_date": f[30],
                "price": price,
                "prev_close": prev,
                "volume": volume,
                "note": "停牌（当日无成交）" if suspended else "当日有成交——库里可能漏了数据",
            })
    return out


def list_tasks() -> list[dict]:
    """通过 schtasks 读计划任务状态（只读，不修改）。

    schtasks 的输出**语言和编码都不稳定**：同一台机器上，有时给英文键名
    （``Status: Ready``），有时给中文（``状态: 就绪``）；编码可能是 gbk 也可能是
    utf-8。所以这里不硬编码，而是先探测编码，再中英文键名都认，最后还有一层
    「按值判断」的兜底。结果缓存 20 秒，避免每次打开页面都去起一个进程。
    """
    global _tasks_cache, _tasks_at
    if _tasks_cache is not None and time.time() - _tasks_at < 20:
        return _tasks_cache

    import subprocess

    names = ["Sequoia-X收盘后回填", "Sequoia-X每日选股"]
    out = []
    for name in names:
        info = {"name": name, "state": "未知", "next_run": None,
                "last_run": None, "last_result": None, "last_result_text": None}
        try:
            proc = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/fo", "LIST", "/v"],
                capture_output=True, timeout=20,
            )
            text = _decode_output(proc.stdout or b"")
            for raw in text.splitlines():
                line = raw.strip()
                if not line or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                field = _task_field(key)
                if field:
                    info[field] = val.strip()

            # 兜底：个别系统上「状态」那行的键名认不出来，就直接看值
            if info["state"] == "未知":
                for raw in text.splitlines():
                    val = raw.partition(":")[2].strip().lower()
                    if val in _STATE_WORDS:
                        info["state"] = _STATE_WORDS[val]
                        break
        except Exception as exc:  # noqa: BLE001
            info["state"] = f"读取失败：{exc}"

        info["last_result_text"] = _result_text(info["last_result"])[0]
        out.append(info)

    _tasks_cache, _tasks_at = out, time.time()
    return out


# ══════════════════════════════════════════════════════════════════
#  信号缓存
# ══════════════════════════════════════════════════════════════════

_signals_lock = threading.Lock()
_signals: dict | None = None


def load_signals() -> dict:
    global _signals
    with _signals_lock:
        if _signals is not None:
            return _signals
        if SIGNALS_CACHE.exists():
            try:
                _signals = json.loads(SIGNALS_CACHE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _signals = None
        if _signals is None:
            _signals = {"generated_at": None, "data_date": None, "total": 0, "groups": []}
        return _signals


def save_signals(payload: dict) -> None:
    global _signals
    with _signals_lock:
        _signals = payload
    SIGNALS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SIGNALS_CACHE.write_text(
        json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8"
    )


def enrich_groups(groups: list[dict]) -> list[dict]:
    """给信号里的代码补上中文名称和板块。"""
    names = get_name_map()
    out = []
    for g in groups:
        g2 = dict(g)
        g2["symbols"] = [
            {
                "symbol": s["symbol"] if isinstance(s, dict) else s,
                "name": names.get(s["symbol"] if isinstance(s, dict) else s, ""),
                "board": board_of(s["symbol"] if isinstance(s, dict) else s),
            }
            for s in g.get("symbols", [])
        ]
        out.append(g2)
    return out


# ══════════════════════════════════════════════════════════════════
#  任务（后台运行 + 日志捕获）
# ══════════════════════════════════════════════════════════════════

MAX_LOG_LINES = 2000


class Job:
    def __init__(self, job_id: str, kind: str):
        self.id = job_id
        self.kind = kind
        self.status = "running"
        self.started = now_str()
        self.ended: str | None = None
        self.exit: int | None = None
        self.error: str | None = None
        self.progress = {"done": 0, "total": 0, "text": "启动中…"}
        self.log: list[str] = []
        self._lock = threading.Lock()

    def add_log(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > MAX_LOG_LINES:
                del self.log[: len(self.log) - MAX_LOG_LINES]

    def snapshot(self, log_lines: int = 400) -> dict:
        with self._lock:
            tail = self.log[-log_lines:]
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "exit": self.exit,
            "error": self.error,
            "progress": dict(self.progress),
            "log": tail,
        }


class _JobLogHandler(logging.Handler):
    """把 logging 记录转发到某个 Job 的日志列表。"""

    def __init__(self, job: Job):
        super().__init__(level=logging.INFO)
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.add_log(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def _target_loggers() -> list[logging.Logger]:
    """需要挂 handler 的 logger：sequoia_x 全命名空间 + __main__。

    sequoia_x 的 logger 都设了 propagate=False，所以不能只挂 root。
    """
    names = {"__main__", "sequoia_x"}
    names |= {n for n in logging.Logger.manager.loggerDict if n.startswith("sequoia_x")}
    return [logging.getLogger(n) for n in sorted(names)]


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._counter = 0
        self._running: str | None = None

    def list(self) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))
            return [self._jobs[i].snapshot(log_lines=0) for i in ids[:50]]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def is_busy(self) -> str | None:
        with self._lock:
            return self._running

    def start(self, kind: str) -> Job:
        with self._lock:
            self._counter += 1
            job_id = f"j{self._counter}-{int(time.time())}"
            job = Job(job_id, kind)
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._running = job_id
        threading.Thread(target=self._run, args=(job, kind), daemon=True).start()
        return job

    def _run(self, job: Job, kind: str) -> None:
        handler = _JobLogHandler(job)
        loggers = _target_loggers()
        for lg in loggers:
            lg.addHandler(handler)
        job.add_log(f"===== 任务开始 {job.started}  类型={kind} =====")

        def on_strategy(index: int, total: int, name: str) -> None:
            job.progress = {"done": index, "total": total, "text": f"执行策略 {index}/{total}：{name}"}

        try:
            if kind == "signals":
                self._job_signals(job, on_strategy)
            elif kind == "daily":
                self._job_daily(job, on_strategy)
            elif kind == "backfill":
                self._job_backfill(job)
            else:
                raise ValueError(f"未知任务类型：{kind}")
            job.status = "done"
            job.exit = 0
            job.progress["text"] = "已完成"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.exit = 1
            job.error = f"{type(exc).__name__}: {exc}"
            job.add_log("!!! 任务失败：" + job.error)
            job.add_log(traceback.format_exc())
        finally:
            job.ended = now_str()
            for lg in loggers:
                try:
                    lg.removeHandler(handler)
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._running = None

    # ── 三种任务 ──

    def _job_signals(self, job: Job, on_strategy) -> None:
        engine = get_engine()
        settings = get_settings()
        job.progress = {"done": 0, "total": 7, "text": "开始计算信号…"}
        results = run_strategies(engine, settings, push=False, on_strategy=on_strategy)
        coverage = get_coverage(force=True)
        meta = {m["key"]: m for m in strategy_meta()}
        groups = [
            {
                "key": r["key"],
                "name": r["name"],
                "label": meta.get(r["key"], {}).get("label", r["key"]),
                "desc": meta.get(r["key"], {}).get("desc", ""),
                "error": r["error"],
                "symbols": r["symbols"],
            }
            for r in results
        ]
        payload = {
            "generated_at": now_str(),
            "data_date": coverage.get("max_date"),
            "total": sum(len(g["symbols"]) for g in groups),
            "groups": groups,
        }
        save_signals(payload)
        job.add_log(f"信号计算完成，共 {payload['total']} 个信号")

    def _job_daily(self, job: Job, on_strategy) -> None:
        engine = get_engine()
        settings = get_settings()
        job.progress = {"done": 0, "total": 7, "text": "增量同步最新交易日数据…"}
        added = engine.sync_today_bulk()
        job.add_log(f"增量同步完成，新增 {added} 行")
        job.progress = {"done": 0, "total": 7, "text": "执行 7 个策略并推送飞书…"}
        run_strategies(engine, settings, push=True, on_strategy=on_strategy)

    def _job_backfill(self, job: Job) -> None:
        engine = get_engine()
        job.progress = {"done": 0, "total": 0, "text": "获取全市场代码表…"}
        symbols = engine.get_all_symbols()
        job.add_log(f"全市场 {len(symbols)} 只，开始回填…")
        job.progress = {"done": 0, "total": len(symbols), "text": f"回填 {len(symbols)} 只…"}
        engine.backfill(symbols)
        get_coverage(force=True)


JOBS = JobManager()


# ══════════════════════════════════════════════════════════════════
#  HTTP 处理
# ══════════════════════════════════════════════════════════════════

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SequoiaX-WebUI/1.0"
    protocol_version = "HTTP/1.1"

    # ── 基础设施 ──

    def log_message(self, fmt, *args):  # 静音默认的逐请求日志
        return

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": message}, code)

    # ── 路由 ──

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            q = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")

            if path == "/api/overview":
                return self._api_overview()
            if path == "/api/coverage":
                return self._api_coverage(q)
            if path == "/api/strategies":
                return self._json({"ok": True, "strategies": strategy_meta()})
            if path == "/api/signals":
                return self._api_signals()
            if path == "/api/search":
                return self._api_search(q)
            if path.startswith("/api/stock/"):
                return self._api_stock(path[len("/api/stock/"):], q)
            if path == "/api/jobs":
                return self._json({"ok": True, "jobs": JOBS.list()})
            if path.startswith("/api/jobs/"):
                return self._api_job(path[len("/api/jobs/"):])
            if path == "/api/logs":
                return self._api_logfiles()
            if path.startswith("/api/logs/"):
                return self._api_logfile(path[len("/api/logs/"):], q)
            if path == "/api/tasks":
                return self._json({"ok": True, "tasks": list_tasks()})
            if path == "/api/suspended-check":
                return self._api_suspended_check()

            return self._error(f"未知接口：{path}", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("GET 处理失败")
            try:
                self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # noqa: BLE001
                pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                body = {}

            if parsed.path == "/api/jobs":
                return self._api_start_job(body)

            return self._error(f"未知接口：{parsed.path}", 404)
        except Exception as exc:  # noqa: BLE001
            log.exception("POST 处理失败")
            try:
                self._error(f"{type(exc).__name__}: {exc}", 500)
            except Exception:  # noqa: BLE001
                pass

    # ── 静态文件 ──

    def _static(self, rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            return self._error("非法路径", 403)
        if not target.is_file():
            if rel in ("", "index.html"):
                return self._send(
                    200,
                    "<h1>Sequoia-X 工作台</h1><p>前端文件还没就绪："
                    f"<code>{STATIC_DIR}</code> 下缺少 index.html。</p>".encode("utf-8"),
                    "text/html; charset=utf-8",
                )
            return self._error(f"文件不存在：{rel}", 404)
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    # ── 各接口实现 ──

    def _api_overview(self) -> None:
        cov = get_coverage()
        settings = get_settings()
        sig = load_signals()
        self._json({
            "ok": True,
            "generated_at": now_str(),
            "db": db_stats(),
            "sources": [
                {"name": k or "未知", "count": v}
                for k, v in sorted(cov["sources"].items(), key=lambda x: -x[1])
            ],
            "last_dates": [
                {"date": k, "count": v}
                for k, v in sorted(cov["last_dates"].items())
            ][-15:],
            "boards": [
                {"name": k, "count": v}
                for k, v in sorted(cov["boards"].items(), key=lambda x: -x[1])
            ],
            "config": {
                "data_source": settings.data_source,
                "auto_source_order": getattr(settings, "auto_source_order", ""),
                "workers": settings.ak_workers,
                "delay": settings.ak_delay,
                "start_date": settings.start_date,
                "rebase_stale_days": getattr(settings, "rebase_stale_days", None),
            },
            "tasks": list_tasks(),
            "signals": {
                "generated_at": sig.get("generated_at"),
                "data_date": sig.get("data_date"),
                "total": sig.get("total", 0),
            },
        })

    def _api_suspended_check(self) -> None:
        """核查所有「数据落后」的股票到底是真停牌，还是我们漏了数据。"""
        cov = get_coverage()
        stale = [i["symbol"] for i in cov["items"] if i["stale_days"] > 0]
        if not stale:
            return self._json({"ok": True, "checked": 0, "suspended": 0,
                               "missing": 0, "rows": []})
        rows = check_suspended(stale)
        suspended = sum(1 for r in rows if r.get("suspended") is True)
        missing = sum(1 for r in rows if r.get("suspended") is False)
        self._json({
            "ok": True,
            "checked": len(rows),
            "suspended": suspended,
            "missing": missing,
            "unknown": len(rows) - suspended - missing,
            "rows": rows,
        })

    def _api_coverage(self, q: dict) -> None:
        cov = get_coverage()
        items = cov["items"]

        flt = (q.get("filter") or ["all"])[0]
        keyword = (q.get("q") or [""])[0].strip().lower()
        limit = max(1, min(500, int((q.get("limit") or ["100"])[0])))
        offset = max(0, int((q.get("offset") or ["0"])[0]))

        if flt == "stale":
            items = [i for i in items if i["stale_days"] > 0]
        elif flt == "suspended":
            items = [i for i in items if i["stale_days"] > 0]

        if keyword:
            items = [
                i for i in items
                if keyword in i["symbol"].lower() or keyword in (i["name"] or "").lower()
            ]

        total = len(items)
        page = items[offset: offset + limit]
        self._json({"ok": True, "total": total, "limit": limit, "offset": offset, "rows": page})

    def _api_signals(self) -> None:
        sig = load_signals()
        self._json({
            "ok": True,
            "generated_at": sig.get("generated_at"),
            "data_date": sig.get("data_date"),
            "total": sig.get("total", 0),
            "groups": enrich_groups(sig.get("groups", [])),
        })

    def _api_search(self, q: dict) -> None:
        keyword = (q.get("q") or [""])[0].strip()
        if not keyword:
            return self._json({"ok": True, "results": []})
        names = get_name_map()
        low = keyword.lower()
        hits = []
        for symbol, name in names.items():
            if symbol.startswith(keyword) or (name and low in name.lower()):
                hits.append({"symbol": symbol, "name": name})
                if len(hits) >= 20:
                    break
        self._json({"ok": True, "results": hits})

    def _api_stock(self, symbol: str, q: dict) -> None:
        symbol = symbol.strip()
        if not re.fullmatch(r"\d{6}", symbol):
            return self._error(f"股票代码格式不对：{symbol}")
        days = max(20, min(1200, int((q.get("days") or ["250"])[0])))

        import sqlite3

        settings = get_settings()
        db = REPO / settings.db_path
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume, turnover FROM stock_daily "
                "WHERE symbol = ? ORDER BY date DESC LIMIT ?",
                (symbol, days),
            ).fetchall()
            src_row = conn.execute(
                "SELECT source FROM symbol_source WHERE symbol = ?", (symbol,)
            ).fetchone()
        finally:
            conn.close()

        if not rows:
            return self._error(f"库里没有这只股票的数据：{symbol}")

        rows.reverse()
        bars = [
            {"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "turnover": r[6]}
            for r in rows
        ]

        closes = [b["close"] for b in bars if b["close"] is not None]
        vols = [b["volume"] for b in bars if b["volume"] is not None]
        stats = {"close": closes[-1] if closes else None, "chg_1d": None,
                 "chg_20d": None, "vol_ratio": None}
        if len(closes) >= 2 and closes[-2]:
            stats["chg_1d"] = round((closes[-1] / closes[-2] - 1) * 100, 2)
        if len(closes) >= 21 and closes[-21]:
            stats["chg_20d"] = round((closes[-1] / closes[-21] - 1) * 100, 2)
        if len(vols) >= 6:
            base = sum(vols[-6:-1]) / 5
            if base:
                stats["vol_ratio"] = round(vols[-1] / base, 2)

        sig = load_signals()
        hit = [g["key"] for g in sig.get("groups", [])
               if any((s.get("symbol") if isinstance(s, dict) else s) == symbol
                      for s in g.get("symbols", []))]

        names = get_name_map()
        self._json({
            "ok": True,
            "symbol": symbol,
            "name": names.get(symbol, ""),
            "source": src_row[0] if src_row else "",
            "board": board_of(symbol),
            "last_date": bars[-1]["date"],
            "days": len(bars),
            "stats": stats,
            "signals": hit,
            "bars": bars,
        })

    def _api_job(self, job_id: str) -> None:
        job = JOBS.get(job_id.strip())
        if job is None:
            return self._error(f"任务不存在：{job_id}", 404)
        self._json({"ok": True, "job": job.snapshot()})

    def _api_start_job(self, body: dict) -> None:
        kind = str(body.get("kind") or "").strip()
        if kind not in ("daily", "backfill", "signals"):
            return self._error(f"未知任务类型：{kind}")
        busy = JOBS.is_busy()
        if busy:
            return self._error("已有任务在运行，请等它结束（同时只允许一个任务，避免争抢数据库）", 409)
        job = JOBS.start(kind)
        log.info(f"启动任务 {job.id} 类型={kind}")
        self._json({"ok": True, "job": job.snapshot(log_lines=0)})

    def _api_logfiles(self) -> None:
        files = []
        if LOGS_DIR.is_dir():
            for p in sorted(LOGS_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[:80]:
                st = p.stat()
                files.append({
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                })
        self._json({"ok": True, "files": files})

    def _api_logfile(self, name: str, q: dict) -> None:
        name = Path(name).name  # 防目录穿越
        target = LOGS_DIR / name
        if not target.is_file():
            return self._error(f"日志文件不存在：{name}", 404)
        tail = max(1, min(5000, int((q.get("tail") or ["500"])[0])))
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return self._error(f"读取失败：{exc}", 500)
        lines = text.splitlines()[-tail:]
        self._json({"ok": True, "name": name, "lines": lines})


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    # 预热：引擎 + 名称表（首次请求就不用等）
    try:
        get_engine()
        get_name_map()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"预热失败（不影响启动）：{exc}")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log.info("=" * 62)
    log.info(f"  Sequoia-X Web 工作台已启动：http://{HOST}:{PORT}")
    log.info(f"  仓库目录：{REPO}")
    log.info(f"  数据源：{get_settings().data_source}    仅本机可访问")
    log.info("  按 Ctrl+C 停止")
    log.info("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，正在停止…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
