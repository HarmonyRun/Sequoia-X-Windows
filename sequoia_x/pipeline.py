"""选股流水线：把「跑 7 个策略（可选推送飞书）」这段流程抽出来。

命令行（main.py）和 Web 工作台（webui/server.py）都调用这里，
**策略列表只在 STRATEGY_CLASSES 维护一处**，避免两处漂移。
"""

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

logger = get_logger(__name__)

# 新增策略只要加到这里，命令行和 Web 工作台都会自动带上
STRATEGY_CLASSES: list[type[BaseStrategy]] = [
    MaVolumeStrategy,
    TurtleTradeStrategy,
    HighTightFlagStrategy,
    LimitUpShakeoutStrategy,
    UptrendLimitDownStrategy,
    RpsBreakoutStrategy,
    PrivatePlacementStrategy,
]

# 中文短名，供 Web 工作台展示
STRATEGY_LABELS: dict[str, str] = {
    "ma_volume": "均线放量",
    "turtle": "海龟交易",
    "flag": "高旗形整理",
    "shakeout": "涨停洗盘",
    "limit_down": "上升趋势跌停",
    "rps": "RPS极强动量",
    "private_placement": "定增监控",
}


def build_strategies(engine: DataEngine, settings: Settings) -> list[BaseStrategy]:
    """按 STRATEGY_CLASSES 实例化全部策略。"""
    return [cls(engine=engine, settings=settings) for cls in STRATEGY_CLASSES]


def strategy_meta() -> list[dict]:
    """策略元信息（key / 类名 / 中文短名 / 说明），供 Web 工作台展示。"""
    out = []
    for cls in STRATEGY_CLASSES:
        doc = (cls.__doc__ or "").strip().splitlines()
        out.append(
            {
                "key": cls.webhook_key,
                "name": cls.__name__,
                "label": STRATEGY_LABELS.get(cls.webhook_key, cls.webhook_key),
                "desc": doc[0].strip() if doc else "",
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
