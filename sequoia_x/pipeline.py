"""选股流水线：把「跑选股策略（可选推送飞书）」这段流程抽出来。

命令行（main.py）和 Web 工作台（webui/server.py）都调用这里，
**策略列表只在 STRATEGY_CLASSES 维护一处**，避免两处漂移。

用户可以在 Web 工作台的「设置」页勾选启用哪些策略（写入 .env 的
ENABLED_STRATEGIES），这个开关对命令行、工作台手动触发、计划任务**全局生效**。
"""

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.boll_breakout import BollBreakoutStrategy
from sequoia_x.strategy.gap_up import GapUpStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_bullish_align import MaBullishAlignStrategy
from sequoia_x.strategy.ma_golden_cross import MaGoldenCrossStrategy
from sequoia_x.strategy.ma_squeeze_breakout import MaSqueezeBreakoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.macd_golden_cross import MacdGoldenCrossStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.rsi_oversold_rebound import RsiOversoldReboundStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.volume_price_rise import VolumePriceRiseStrategy

logger = get_logger(__name__)

# 新增策略只要加到这里，命令行和 Web 工作台都会自动带上。
#
# 排序有讲究：**原项目自带的 7 个策略排在最前面**，保持原有行为与推送顺序；
# 新增的 8 个排在后面。这样即使将来有人用「取前 N 个」之类的逻辑也不会乱。
STRATEGY_CLASSES: list[type[BaseStrategy]] = [
    # ── 原项目自带（算法未改动）──
    MaVolumeStrategy,
    TurtleTradeStrategy,
    HighTightFlagStrategy,
    LimitUpShakeoutStrategy,
    UptrendLimitDownStrategy,
    RpsBreakoutStrategy,
    PrivatePlacementStrategy,
    # ── 本项目新增的经典策略 ──
    MaBullishAlignStrategy,
    MacdGoldenCrossStrategy,
    BollBreakoutStrategy,
    MaGoldenCrossStrategy,
    MaSqueezeBreakoutStrategy,
    VolumePriceRiseStrategy,
    RsiOversoldReboundStrategy,
    GapUpStrategy,
]

# 中文短名，供 Web 工作台展示
STRATEGY_LABELS: dict[str, str] = {
    # 原项目自带
    "ma_volume": "均线放量",
    "turtle": "海龟交易",
    "flag": "高旗形整理",
    "shakeout": "涨停洗盘",
    "limit_down": "上升趋势跌停",
    "rps": "RPS极强动量",
    "private_placement": "定增监控",
    # 本项目新增
    "ma_bullish": "均线多头排列",
    "macd_cross": "MACD金叉",
    "boll_breakout": "布林带突破",
    "ma_cross": "双均线金叉",
    "ma_squeeze": "均线粘合突破",
    "vol_price": "量价齐升",
    "rsi_rebound": "RSI超卖反弹",
    "gap_up": "向上跳空缺口",
}

# 策略分组：供 Web 工作台把「原项目自带」和「本项目新增」分开展示，
# 让用户一眼看出哪些是原有算法、哪些是后加的。
STRATEGY_GROUPS: dict[str, list[str]] = {
    "upstream": [
        "ma_volume", "turtle", "flag", "shakeout",
        "limit_down", "rps", "private_placement",
    ],
    "added": [
        "ma_bullish", "macd_cross", "boll_breakout", "ma_cross",
        "ma_squeeze", "vol_price", "rsi_rebound", "gap_up",
    ],
}


def enabled_keys(settings: Settings) -> set[str]:
    """从配置里解析出「要启用哪些策略」。

    空配置 = 全部启用。这一点是**刻意**的向后兼容：老用户的 .env 里没有
    ENABLED_STRATEGIES 这一项，行为必须和加这个功能之前完全一致。

    Returns:
        启用的 webhook_key 集合；配置为空时返回全部策略的 key。
    """
    raw = (getattr(settings, "enabled_strategies", "") or "").strip()
    if not raw:
        return {cls.webhook_key for cls in STRATEGY_CLASSES}
    return {part.strip() for part in raw.split(",") if part.strip()}


def build_strategies(engine: DataEngine, settings: Settings) -> list[BaseStrategy]:
    """按 STRATEGY_CLASSES 实例化策略，**只实例化用户启用的那些**。

    被禁用的策略连对象都不创建——省掉一次全市场遍历，也避免日志里出现
    「某策略选出 0 只」这种让人误以为它跑过的记录。
    """
    keys = enabled_keys(settings)
    chosen = [cls for cls in STRATEGY_CLASSES if cls.webhook_key in keys]
    skipped = [cls.webhook_key for cls in STRATEGY_CLASSES if cls.webhook_key not in keys]
    if skipped:
        logger.info(
            f"已启用 {len(chosen)}/{len(STRATEGY_CLASSES)} 个策略，"
            f"跳过：{', '.join(skipped)}"
        )
    return [cls(engine=engine, settings=settings) for cls in chosen]


def strategy_meta(settings: Settings | None = None) -> list[dict]:
    """策略元信息（key / 类名 / 中文短名 / 说明 / 是否启用），供 Web 工作台展示。

    Args:
        settings: 传入时会带上 enabled 字段；不传则 enabled 一律为 True，
            保持老调用点的行为不变。
    """
    keys = enabled_keys(settings) if settings is not None else None
    added = set(STRATEGY_GROUPS.get("added", []))
    out = []
    for cls in STRATEGY_CLASSES:
        doc = (cls.__doc__ or "").strip().splitlines()
        out.append(
            {
                "key": cls.webhook_key,
                "name": cls.__name__,
                "label": STRATEGY_LABELS.get(cls.webhook_key, cls.webhook_key),
                "desc": doc[0].strip() if doc else "",
                "enabled": True if keys is None else (cls.webhook_key in keys),
                # 前端据此分组显示「原项目自带 / 本项目新增」
                "group": "added" if cls.webhook_key in added else "upstream",
            }
        )
    return out


def run_strategies(
    engine: DataEngine,
    settings: Settings,
    push: bool = False,
    notifier: FeishuNotifier | None = None,
    on_strategy=None,
) -> list[dict]:
    """跑一遍全部策略。

    Args:
        push: 是否推送飞书。命令行日常模式传 True；Web 工作台的「只算信号」传 False，
            避免调试时把飞书群刷屏。
        notifier: 复用已有的 FeishuNotifier；为 None 且 push=True 时自动创建。
        on_strategy: 进度回调 ``(序号, 总数, 策略类名)``，Web 工作台用它汇报进度。

    Returns:
        ``[{"key", "name", "label", "symbols": [...], "error": None|str}]``
    """
    if push and notifier is None:
        notifier = FeishuNotifier(settings)

    strategies = build_strategies(engine, settings)
    total = len(strategies)
    results: list[dict] = []

    for index, strategy in enumerate(strategies, start=1):
        name = type(strategy).__name__
        if on_strategy is not None:
            on_strategy(index, total, name)

        entry = {
            "key": strategy.webhook_key,
            "name": name,
            "label": STRATEGY_LABELS.get(strategy.webhook_key, strategy.webhook_key),
            "symbols": [],
            "error": None,
        }

        logger.info(f"执行策略：{name}")
        try:
            selected = list(strategy.run())
        except Exception as exc:  # noqa: BLE001
            # 单个策略失败不应拖垮整条流水线：记下来继续跑下一个
            logger.exception(f"{name} 执行失败")
            entry["error"] = f"{type(exc).__name__}: {exc}"
            results.append(entry)
            continue

        # 注意：每个策略内部已经打过「选出 N 只股票」，这里不再重复打一遍
        entry["symbols"] = selected
        results.append(entry)

        if selected:
            if push:
                notifier.send(
                    symbols=selected,
                    strategy_name=name,
                    webhook_key=strategy.webhook_key,
                )
        elif push:
            logger.info(f"{name} 无选股结果，跳过推送")

    return results


def run_daily(
    engine: DataEngine,
    settings: Settings,
    push: bool = True,
    on_strategy=None,
    analyze: bool = True,
) -> dict:
    """日常流程：跑策略 →（可选）大模型分析 → 推送分析。

    命令行（main.py）和 Web 工作台的「运行日常选股」都走这里，
    保证两边行为完全一致——尤其是「分析结果推送」这一步不能只在一边有。

    Args:
        push: 是否推送选股卡片。
        analyze: 是否做大模型分析（还要看 ``settings.llm_enabled``）。

    Returns:
        ``{"results": [...], "analysis": str|None, "analysis_pushed": bool}``
    """
    results = run_strategies(engine, settings, push=push, on_strategy=on_strategy)

    out = {"results": results, "analysis": None, "analysis_pushed": False}
    if not analyze:
        return out

    # 大模型分析是附加价值：任何失败都只记日志，不能影响已经算出来的选股结果
    try:
        from sequoia_x.analyze.llm import analyze_daily, to_lark_md

        text = analyze_daily(engine, settings, results, labels=STRATEGY_LABELS)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"大模型分析异常：{exc}")
        return out

    if not text:
        return out
    out["analysis"] = text

    from sequoia_x.notify.feishu import FeishuNotifier

    notifier = FeishuNotifier(settings)
    date_text = ""
    try:
        import sqlite3

        conn = sqlite3.connect(str(engine.db_path))
        try:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            date_text = row[0] if row else ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass

    pushed = notifier.send_markdown(
        title="🤖 Sequoia-X 选股 AI 分析",
        subtitle=f"数据日期：{date_text}" if date_text else "",
        content=to_lark_md(text),
        webhook_key="default",
        template="indigo",
    )
    out["analysis_pushed"] = bool(pushed)
    return out
