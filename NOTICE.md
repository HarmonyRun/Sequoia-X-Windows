# NOTICE — 来源、归属与改动说明

本文件说明本项目与上游项目的关系、各自的权利归属，以及本分支具体改了哪些文件。

---

## 1. 来源

| | |
|---|---|
| **上游项目** | [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) |
| **上游作者** | [sngyai](https://github.com/sngyai) |
| **本仓库** | 上游项目的二次开发分支（fork），**非官方版本** |
| **分叉起点** | 上游 `master` 分支的提交 `444c0db`（2026-05-09） |

本仓库是通过 GitHub 的 fork 功能创建的，与上游保持 fork 关系；
上游的后续提交可以通过 GitHub 的 "Sync fork" 或本地 `git remote add upstream` 合并进来。

---

## 2. 归属：哪些是原作者的，哪些是后加的

### 完全来自上游、本分支未改动

| 范围 | 说明 |
|---|---|
| `sequoia_x/strategy/` 下的 7 个策略 | `turtle_trade` / `ma_volume` / `high_tight_flag` / `limit_up_shakeout` / `uptrend_limit_down` / `rps_breakout` / `private_placement`。**选股算法本身完全没有改动。** |
| `sequoia_x/core/logger.py` | 未改动 |
| `sequoia_x/strategy/base.py` | 未改动 |
| 整体架构设计 | 数据层 → 策略层 → 推送层的分层、向量化计算、增量更新思路 |
| 飞书卡片推送的原始实现 | `feishu.py` 的基础实现（本分支只做了增量扩展，见下） |

### 来自上游、本分支做了修改

| 文件 | 改了什么 |
|---|---|
| `main.py` | 抽出策略流程到 `pipeline.py`；数据源说明 |
| `sequoia_x/core/config.py` | 新增多数据源 / 自动换源 / 大模型等配置项；`feishu_webhook_url` 从必填改可选（安装包要求「装完就能启动」） |
| `sequoia_x/data/engine.py` | **改动最大**：baostock 单源 → akshare 三源 + 自动换源 + `symbol_source` 表防同股跨源拼接 |
| `sequoia_x/notify/feishu.py` | 新增 `send_markdown()`（推大模型分析）与 `get_stock_names()`；空 Webhook 时静默跳过 |
| `tests/test_config.py` | 原来断言「缺 Webhook 抛异常」，按新的可选语义改写 |
| `README.md` | 加入二次开发声明、本分支改动说明、归属与许可证说明 |

### 本分支新增

| 路径 | 说明 |
|---|---|
| `sequoia_x/analyze/llm.py` | 大模型分析（OpenAI 兼容接口） |
| `sequoia_x/pipeline.py` | 策略列表与日常流程，命令行与 Web 工作台共用 |
| `webui/` | Web 工作台（`server.py` / `envstore.py` / `static/`），仅用 Python 标准库 |
| `build/` | 安装包构建脚本 |
| `installer/` | C# 安装器 / 启动器 / 卸载器源码 |
| `docs/使用说明.md` | 完整使用说明与踩坑记录 |
| `tests/test_llm.py` | 大模型模块的单元测试 |

---

## 3. 数据源变更说明（重要）

上游使用 [baostock](http://baostock.com)。本分支改为 akshare（东方财富 / 腾讯 / 新浪），原因是：

> 在作者的机器上，baostock 返回 `登录失败 code=10001011 msg=黑名单用户`，
> 即**该出口 IP 被 baostock 拉黑**，且持续 4 小时以上未解封。

这是**特定网络环境**的问题，**不代表 baostock 有问题**。
上游项目使用 baostock 是合理的；如果你的网络环境可以正常访问 baostock，
完全可以改回 `DATA_SOURCE=baostock`（配置项仍然保留）。

**⚠️ 换源带来的一个必须知道的影响**：不同数据源的后复权基准不同。
实测同一只股票同一天，三个源的价格能差 40%（而成交量、成交额是一致的）。
因此本分支引入 `symbol_source` 表记录每只股票使用的数据源，
增量更新只认这个源，**绝不允许同一只股票跨源拼接**。

---

## 4. 许可证情况

- 上游 `README.md` 的「许可证 | License」一节声明为 **MIT**。
- 但上游仓库中**没有 `LICENSE` 文件**，`pyproject.toml` 中也没有 `license` 字段。
- 本分支的处理：
  - **保留**上游的 MIT 声明，并在 README 中明确标注来源与本分支的改动范围；
  - **未新增 `LICENSE` 文件**——因为本分支新增代码的授权方式应由本分支作者决定，
    且是否需要原作者进一步确认也应由作者判断；
  - 不在本分支中移除或改写上游的任何版权/署名信息。

**给使用者的建议**：如果你要基于本分支继续开发或再分发，
请保留对 `sngyai/Sequoia-X` 的署名，并自行确认上游许可证状态。

---

## 5. 免责声明

- 本项目为量化选股信号工具，**不构成任何投资建议**。
- 原作者不对本分支的任何问题负责。
- 数据来自公开接口，仅供个人研究学习使用，请遵守各数据源的服务条款。
