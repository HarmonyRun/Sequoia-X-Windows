"""大模型分析：把「当天市场情况 + 各策略选出的股票」交给大模型点评，推送到飞书。

## 为什么用 OpenAI 兼容接口

`POST {base_url}/chat/completions` 是事实标准：DeepSeek、通义千问（兼容模式）、
智谱、Moonshot、OpenAI、本地 Ollama / vLLM 全都认。所以用户只要填
**base_url + model + api_key** 三样就能接自己的模型，不用为每家写适配。

## 三条设计原则

1. **没配就安静跳过**：安装包分发的场景下多数用户不会配，不能因为缺配置就报错刷屏。
2. **失败不影响选股**：分析只是附加价值，任何异常都吞掉并记日志——选股结果该推还是要推。
3. **给模型真实数据**：把当天的涨跌家数、涨跌停家数、总成交额一起喂进去，
   否则它只能对着几个股票代码空谈。
"""

from __future__ import annotations

import json

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)

# 每个策略最多列多少只股票给模型。全市场策略一次能选出 100+ 只，
# 全塞进去既贵又没用——模型看的是「方向」不是「清单」。
MAX_PER_STRATEGY = 25

# 大模型调用超时（秒）。推理模型思考久，给宽松点。
DEFAULT_TIMEOUT = 180


class LLMError(RuntimeError):
    """大模型调用失败。消息可以直接给用户看。"""


def endpoint_of(base_url: str) -> str:
    """把用户填的 base_url 补成完整的 chat/completions 地址。

    容忍这些写法（都实测过是常见填法）：
        https://api.deepseek.com          -> .../chat/completions
        https://api.deepseek.com/v1       -> .../v1/chat/completions
        http://localhost:11434/v1         -> .../v1/chat/completions
        https://xx/v1/chat/completions    -> 原样使用
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("没有配置大模型接口地址")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def is_configured(settings: Settings) -> bool:
    """配置齐了没有。注意 api_key 允许为空——本地 Ollama 不需要。"""
    return bool(
        getattr(settings, "llm_enabled", False)
        and (settings.llm_base_url or "").strip()
        and (settings.llm_model or "").strip()
    )


def chat(settings: Settings, messages: list[dict], timeout: int = DEFAULT_TIMEOUT) -> str:
    """调一次 OpenAI 兼容的 chat/completions，返回助手回复的正文。"""
    url = endpoint_of(settings.llm_base_url)
    api_key = (settings.llm_api_key or "").strip()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        # 本地 Ollama 之类不需要鉴权，给个占位符更省事
        headers["Authorization"] = "Bearer not-needed"

    payload = {
        "model": (settings.llm_model or "").strip(),
        "messages": messages,
        "temperature": 0.4,
        "stream": False,
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"请求大模型失败：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"大模型返回 HTTP {resp.status_code}：{resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError(f"大模型返回的不是 JSON：{resp.text[:200]}") from exc

    # 有的服务出错时也返回 200，错误藏在 body 里
    if isinstance(data, dict) and data.get("error"):
        raise LLMError(f"大模型报错：{str(data['error'])[:300]}")

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"大模型返回结构异常：{str(data)[:300]}") from exc

    if not content or not str(content).strip():
        raise LLMError("大模型返回了空内容")
    return str(content).strip()


# ══════════════════════════════════════════════════════════════════
#  提示词
# ══════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一位严谨的 A 股量化分析师。用户会给你某个交易日的全市场概况，
以及一套量化选股系统当天各个策略选出的股票。

请你完成三件事：
1. 用两三句话概括当天的市场状态（结合涨跌家数、涨跌停家数、成交额）；
2. 分析各策略选出的股票有什么共性、反映了什么资金偏好或题材方向；
3. 给出值得关注的方向，以及需要警惕的风险。

要求：
- 全程中文，总长度控制在 500 字以内，直接给结论，不要复述用户给你的数据。
- 可以用 **加粗** 和「- 」开头的列表，但**不要用 # 标题、不要用表格、不要用代码块**
  （飞书卡片不支持这些语法，会显示成乱码）。
- 客观陈述信号本身，不要给出「买入」「卖出」这类投资建议；结尾用一句话提示
  这只是量化信号的技术性解读，不构成投资建议。
- 如果某个策略当天没有选出股票，简单提一句即可，不要强行解读。"""


def _fmt_symbols(symbols: list[str], names: dict[str, str]) -> str:
    shown = symbols[:MAX_PER_STRATEGY]
    parts = []
    for code in shown:
        name = names.get(code, "")
        parts.append(f"{code} {name}".strip())
    text = "、".join(parts)
    if len(symbols) > MAX_PER_STRATEGY:
        text += f" …（共 {len(symbols)} 只，其余 {len(symbols) - MAX_PER_STRATEGY} 只略）"
    return text


def build_user_prompt(breadth: dict, results: list[dict], labels: dict[str, str]) -> str:
    """把市场和信号拼成给模型的用户消息。"""
    lines = []

    if breadth:
        lines.append(f"交易日：{breadth.get('date')}")
        lines.append(
            f"全市场：{breadth.get('total', 0)} 只，"
            f"上涨 {breadth.get('up', 0)} 家、下跌 {breadth.get('down', 0)} 家、"
            f"平盘 {breadth.get('flat', 0)} 家"
        )
        lines.append(
            f"涨停（≥9.8%）{breadth.get('limit_up', 0)} 家、"
            f"跌停（≤-9.8%）{breadth.get('limit_down', 0)} 家，"
            f"平均涨跌幅 {breadth.get('avg_pct', 0)}%，"
            f"总成交额 {breadth.get('turnover_yi', 0)} 亿元"
        )
    else:
        lines.append("（当天市场统计不可用）")

    lines.append("")
    lines.append("各策略选股结果：")
    for entry in results:
        label = labels.get(entry.get("key", ""), entry.get("key", "?"))
        symbols = entry.get("symbols") or []
        if not symbols:
            lines.append(f"- {label}：无信号")
            continue
        names = entry.get("_names") or {}
        lines.append(f"- {label}（{len(symbols)} 只）：{_fmt_symbols(symbols, names)}")
        if entry.get("error"):
            lines[-1] += f"（该策略执行异常：{entry['error']}）"

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  对外入口
# ══════════════════════════════════════════════════════════════════

def analyze_daily(
    engine: DataEngine,
    settings: Settings,
    results: list[dict],
    labels: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str | None:
    """对当天的选股结果做一次大模型分析，返回分析正文。

    Args:
        results: ``run_strategies()`` 的返回值（每项含 key / symbols / error）。
        labels: ``{策略key: 中文名}``，不传就用 pipeline 里的默认映射。

    Returns:
        分析正文；未启用或失败时返回 ``None``（失败会记日志，不抛异常）。
    """
    if not getattr(settings, "llm_enabled", False):
        logger.info("未启用大模型分析，跳过")
        return None
    if not is_configured(settings):
        logger.warning("启用了大模型分析，但接口地址或模型名为空，跳过")
        return None

    if labels is None:
        from sequoia_x.pipeline import STRATEGY_LABELS

        labels = STRATEGY_LABELS

    # 先把名称查好塞进 results 的副本里，提示词里就能带上中文名
    enriched = []
    try:
        from sequoia_x.notify.feishu import FeishuNotifier

        notifier = FeishuNotifier(settings)
        all_symbols: list[str] = []
        for entry in results:
            all_symbols.extend(entry.get("symbols") or [])
        names = notifier.get_stock_names(all_symbols[:400]) if all_symbols else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"查询股票名称失败（分析里只显示代码）：{exc}")
        names = {}

    for entry in results:
        item = dict(entry)
        item["_names"] = names
        enriched.append(item)

    breadth: dict = {}
    try:
        coverage_date = None
        with_engine = engine
        # 用库里最新的交易日，避免分析一个根本没有数据的日期
        import sqlite3

        conn = sqlite3.connect(str(engine.db_path))
        try:
            row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            coverage_date = row[0] if row else None
        finally:
            conn.close()
        if coverage_date:
            breadth = with_engine.market_breadth(coverage_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"统计市场概况失败（分析将不带市场数据）：{exc}")

    user_prompt = build_user_prompt(breadth, enriched, labels)
    logger.info(f"开始大模型分析（模型 {settings.llm_model}，提示词 {len(user_prompt)} 字）")

    try:
        text = chat(
            settings,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            timeout=timeout,
        )
    except LLMError as exc:
        logger.error(f"大模型分析失败：{exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"大模型分析出现未预期错误：{exc}")
        return None

    logger.info(f"大模型分析完成，{len(text)} 字")
    return text


def to_lark_md(text: str) -> str:
    """把模型输出的 markdown 收拾成飞书卡片能渲染的样子。

    飞书的 lark_md 不支持 # 标题，会原样显示成「# 标题」很难看；
    表格和代码块也不支持。这里把标题降级成加粗。
    """
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                out.append(f"**{title}**")
            continue
        if stripped.startswith("```"):
            continue  # 去掉代码块围栏，内容保留
        out.append(line.rstrip())
    return "\n".join(out).strip()
