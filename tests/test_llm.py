"""大模型分析模块的单元测试（全部离线，不调真实 API）。"""

import pytest

from sequoia_x.analyze.llm import (
    LLMError,
    build_user_prompt,
    endpoint_of,
    is_configured,
    to_lark_md,
)
from sequoia_x.core.config import Settings


def _settings(**overrides) -> Settings:
    """构造一个不读 .env 的 Settings，避免受本机配置影响。"""
    return Settings(_env_file=None).model_copy(update=overrides)


# ── 接口地址归一化 ────────────────────────────────────────────────

@pytest.mark.parametrize("given,expected", [
    ("https://api.deepseek.com", "https://api.deepseek.com/chat/completions"),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
    # 已经填到完整路径的就别重复拼
    ("https://x/v1/chat/completions", "https://x/v1/chat/completions"),
])
def test_endpoint_of(given: str, expected: str) -> None:
    assert endpoint_of(given) == expected


def test_endpoint_of_rejects_empty() -> None:
    with pytest.raises(LLMError):
        endpoint_of("")


# ── 配置完整性判断 ────────────────────────────────────────────────

def test_is_configured_requires_switch_url_and_model() -> None:
    assert not is_configured(_settings())
    assert not is_configured(_settings(llm_enabled=True))
    assert not is_configured(_settings(llm_enabled=True, llm_base_url="https://x/v1"))
    assert not is_configured(
        _settings(llm_enabled=True, llm_base_url="https://x/v1", llm_model="  ")
    )
    assert is_configured(
        _settings(llm_enabled=True, llm_base_url="https://x/v1", llm_model="m")
    )


def test_api_key_may_be_empty_for_local_models() -> None:
    """本地 Ollama 不需要鉴权，所以 api_key 为空也必须算「配置好了」。"""
    s = _settings(
        llm_enabled=True, llm_base_url="http://localhost:11434/v1",
        llm_model="qwen2.5", llm_api_key="",
    )
    assert is_configured(s)


# ── 提示词 ────────────────────────────────────────────────────────

def test_price_prompt_carries_market_facts_and_names() -> None:
    breadth = {
        "date": "2026-10-12", "total": 5210, "up": 1099, "down": 4031, "flat": 80,
        "limit_up": 55, "limit_down": 37, "avg_pct": -1.59, "turnover_yi": 15368.4,
    }
    results = [
        {"key": "ma_volume", "symbols": ["600519", "000001"],
         "_names": {"600519": "贵州茅台"}, "error": None},
        {"key": "flag", "symbols": [], "error": None},
        {"key": "rps", "symbols": ["000002"], "_names": {}, "error": "boom"},
    ]
    text = build_user_prompt(breadth, results, {"ma_volume": "均线放量", "flag": "高旗形整理"})

    assert "2026-10-12" in text
    assert "5210" in text and "1099" in text and "4031" in text
    assert "55" in text and "15368.4" in text
    assert "贵州茅台" in text
    assert "均线放量" in text and "2 只" in text
    # 无信号的策略要明说，不能凭空消失
    assert "高旗形整理：无信号" in text
    # 策略执行异常要带出来
    assert "boom" in text


def test_prompt_truncates_long_lists() -> None:
    """海龟交易一次能选出 100+ 只，不能全塞进提示词。"""
    symbols = [f"{i:06d}" for i in range(200)]
    text = build_user_prompt({}, [{"key": "turtle", "symbols": symbols,
                                   "_names": {}, "error": None}], {})
    assert "共 200 只" in text
    assert text.count("略") >= 1
    # 只展开前 25 只
    assert "000024" in text and "000025" not in text


# ── lark_md 转换 ──────────────────────────────────────────────────

def test_to_lark_md_downgrades_headings_and_drops_fences() -> None:
    raw = "## 市场概况\n正文 **加粗**\n```\ncode line\n```\n### 小标题\n- 列表"
    md = to_lark_md(raw)

    assert "**市场概况**" in md
    assert "**小标题**" in md
    # 飞书 lark_md 不支持 # 标题语法，不能残留
    assert not any(line.strip().startswith("#") for line in md.splitlines())
    assert "```" not in md
    assert "正文 **加粗**" in md
    assert "code line" in md      # 围栏去掉，内容保留
    assert "- 列表" in md


def test_to_lark_md_handles_empty() -> None:
    assert to_lark_md("") == ""
    assert to_lark_md(None) == ""
