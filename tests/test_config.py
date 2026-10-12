"""配置管理属性测试。"""

import os
import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st


# Feature: sequoia-x-v2, Property 1: 环境变量覆盖配置默认值
@given(db_path=st.text(min_size=1, max_size=100, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="/_.-")))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_env_overrides_default(db_path: str, monkeypatch) -> None:
    """属性 1：任意合法 db_path 通过环境变量设置后，Settings 实例应反映该值。"""
    import sequoia_x.core.config as cfg_module
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(cfg_module, "_settings", None)
    from sequoia_x.core.config import Settings
    s = Settings()
    assert s.db_path == db_path


# Feature: sequoia-x-v2, Property 2（已按分发需求调整）
#
# 原来这里断言「缺 feishu_webhook_url 就抛 ValidationError」。但安装包要分发给
# 别人、必须保证「装完就能启动」，所以这个字段改成可选（默认空串），
# Web 工作台的设置页负责提示用户去填。对应的不变量变成了下面两条：
#   2a. 没有任何 .env / 环境变量时，Settings 仍能实例化，webhook 为空
#   2b. 空 Webhook 参与解析时不能把推送整个丢掉（要回退到主通道）
def test_missing_webhook_does_not_block_startup() -> None:
    """属性 2a：没有配置任何 Webhook 时也必须能构造出 Settings（安装后开箱可用）。"""
    from sequoia_x.core.config import Settings

    env_backup = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        s = Settings(_env_file=None)
        assert s.feishu_webhook_url == ""
        # 数据源默认必须是 auto：baostock 需要登录且会拉黑 IP，
        # 分发给别人时不能作为默认值
        assert s.data_source == "auto"
        # 两者都没配时返回空串（其它模块据此跳过推送而不是崩）
        assert s.get_webhook_url("ma_volume") == ""
    finally:
        if env_backup is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = env_backup


def test_webhook_fallback_skips_empty_strategy_url() -> None:
    """属性 2b：策略专属 Webhook 是空串时要回退到主 Webhook，而不是返回空。"""
    from sequoia_x.core.config import Settings

    env_backup = {
        k: os.environ.pop(k, None)
        for k in ("FEISHU_WEBHOOK_URL", "STRATEGY_WEBHOOK_RPS")
    }
    try:
        os.environ["FEISHU_WEBHOOK_URL"] = "https://example.com/main"
        os.environ["STRATEGY_WEBHOOK_RPS"] = ""     # 显式留空
        s = Settings(_env_file=None)
        assert s.get_webhook_url("rps") == "https://example.com/main"
        # 配了专属的就用它
        os.environ["STRATEGY_WEBHOOK_RPS"] = "https://example.com/rps"
        s2 = Settings(_env_file=None)
        assert s2.get_webhook_url("rps") == "https://example.com/rps"
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
