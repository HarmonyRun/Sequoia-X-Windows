"""临时自测：验证「提示词 -> 模型响应解析 -> lark_md 转换 -> 飞书真实推送」整条链。

模型响应是伪造的（本机没有大模型 API Key），但除此之外全是真的：
真的读数据库算市场统计、真的查股票名称、真的往飞书发卡片。
"""
import sys
from unittest.mock import patch

sys.path.insert(0, r"C:\Users\mjx server\Documents\股票\Sequoia-X")

from sequoia_x.analyze import llm
from sequoia_x.core.config import get_settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier

FAKE = """## 市场概况
今天是普跌格局，**下跌 4031 家**远超上涨 1099 家，跌停 37 家，平均跌幅 -1.59%，
成交额 1.54 万亿属于缩量下跌。

- 涨停仍有 55 家，说明**局部题材还在活跃**
- 缩量普跌通常意味着恐慌盘不重，但要警惕补跌

## 策略信号解读
- **均线放量**选出 92 只，集中在中小市值与电力设备，属于逆势放量
- **海龟交易** 123 只覆盖面最广，说明多数个股仍在中期上升通道内
- 高旗形整理只有 1 只，**筛选条件在弱市里更严格**，这类信号通常更值得关注

## 关注方向
- 逆势放量的电力设备、公用事业
- 高旗形整理这类罕见的强信号

风险提示：普跌日的信号存在次日补跌风险。**本内容为量化信号的技术性解读，不构成投资建议。**"""

settings = get_settings()
engine = DataEngine(settings)

results = [
    {"key": "ma_volume", "symbols": ["600519", "000001"], "error": None},
    {"key": "turtle", "symbols": ["000002"], "error": None},
    {"key": "flag", "symbols": [], "error": None},
]

print("=== 1. 分析链路（模型响应伪造，其余全真）===")
with patch.object(llm.requests, "post") as mocked:
    mocked.return_value.status_code = 200
    mocked.return_value.json.return_value = {"choices": [{"message": {"content": FAKE}}]}
    probe = settings.model_copy(update={
        "llm_enabled": True, "llm_base_url": "https://fake.example/v1",
        "llm_model": "fake-model", "llm_api_key": "sk-test",
    })
    text = llm.analyze_daily(engine, probe, results)

    call = mocked.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    body = call.kwargs.get("json")
    user_msg = body["messages"][1]["content"]
    system_msg = body["messages"][0]["content"]
    headers = call.kwargs.get("headers") or {}

    print(f"  请求地址     : {url}")
    print(f"  鉴权头       : {headers.get('Authorization')}")
    print(f"  model        : {body['model']}")
    print(f"  temperature  : {body.get('temperature')}  stream={body.get('stream')}")
    print(f"  system 长度  : {len(system_msg)}")
    print(f"  用户提示词长度: {len(user_msg)}")
    print(f"  含真实市场数据: {'4031' in user_msg}")
    print(f"  含股票中文名  : {'贵州茅台' in user_msg}")
    print(f"  分析产出长度  : {len(text or '')}")
    print()
    print("  --- 实际发给模型的用户提示词 ---")
    for line in user_msg.splitlines():
        print("   ", line)

print()
print("=== 2. lark_md 转换（飞书卡片不支持 # 标题和代码块）===")
md = llm.to_lark_md(text)
hashes = [ln for ln in md.splitlines() if ln.strip().startswith("#")]
print(f"  残留 # 标题行: {len(hashes)}")
print(f"  首行: {md.splitlines()[0]}")
print(f"  含加粗: {'**' in md}")

print()
print("=== 3. 真的推送到飞书（验证卡片渲染）===")
notifier = FeishuNotifier(settings)
ok = notifier.send_markdown(
    title="🤖 Sequoia-X 选股 AI 分析（链路自测）",
    subtitle="数据日期：2026-10-12",
    content=md,
    template="indigo",
)
print(f"  推送结果: {'成功' if ok else '失败'}")

print()
print("=== 4. 没配 Webhook 时 send_markdown 应安静返回 False ===")
no_hook = settings.model_copy(update={"feishu_webhook_url": "", "strategy_webhooks": {}})
print(f"  结果: {FeishuNotifier(no_hook).send_markdown('t', 'c')}")
