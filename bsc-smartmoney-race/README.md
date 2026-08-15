# BSC 聪明钱赛马框架

在 BNB Chain 的 meme / 新币赛道上，把**上万个地址当成赛马选手**跑一个赛季，
用统计上站得住脚的方式选出真有技能的少数人，再回测「你跟单能拿到多少」，
最后只对这批人做秒级实时跟踪。

两层落地：

- **离线层（天/周级）负责选人** —— 重、慢、统计严谨，产出选手池
- **实时层（秒级）负责跟人** —— 轻、快，只监听选手池里的地址

---

## 快速开始

```bash
pip install -r requirements.txt

python -m smrace.cli validate   # 端到端跑一遍，并用 ground truth 检验闸门有效性
python -m smrace.cli run        # 只跑管线，产出 out/leaderboard.md 与 out/report.json
python -m smrace.cli oos        # 时间外样本验证（真实数据上的首选验证手段）
python -m smrace.cli sweep      # 阈值敏感性扫描
python tests/test_core.py       # 12 个核心单测
python tests/test_adapters.py   # 15 个适配器字段映射测试（不需要 API key）
```

默认走内置合成数据源（带 ground truth 的模拟宇宙）。切到链上数据：

```bash
export BITQUERY_TOKEN=...  # 或 DUNE_API_KEY
export SMRACE_BNB_PRICE_FLAT=600            # gas 计价，正式用 SMRACE_BNB_PRICE_CSV

# Dune 一条龙（推荐）：校验凭证 → 建 Query → 估账单 → 停下来等你确认
python -m smrace.cli bootstrap --days 7 --max-tokens 800

# 或者分步来：
python -m smrace.cli probe --source bitquery --minutes 10   # ① 核对字段与单位
python -m smrace.cli sql --days 7 --max-tokens 800          # ② 打印要存成 Query 的 SQL
python -m smrace.cli estimate --estimate-query-id 123 --days 7   # ③ 跑前先算账单
python -m smrace.cli run  --source dune --days 7 --query-id 456  # ④ 正式跑
python -m smrace.cli oos  --source dune --days 7            # ⑤ 时间外样本验证
```

**免费额度**：Dune Free 档 2,500 credits/月 ≈ 31 万行导出，跑不了全量，
但用窄范围 SQL（只要窗口内新发、够开一场比赛、活跃度 Top-N 的代币）
跑通链路完全够。Bitquery 首月送 10K points，之后 $39/mo 起。
`estimate` 命令会在跑之前把各档的 credit 和美元算给你看。

**接线的完整步骤与验收标准见 `docs/03`。** 这一层的失败模式不是报错，
是静默算错 —— 字段名或单位错了，管线照样跑完、照样出榜单，只是榜单是错的。

**实测输出**

```
[0] 数据       trades=86,269  tokens=300  wallets=933
[1] 实体聚类   933 地址 → 878 实体（还原出 5 个 sybil 簇）
[2] PnL 结算   30,852 个 (实体,代币) 仓位
[3] 净化       存活 843/878  bundler 10/10 · mev 10/10 · wash 15/15 · sniper 30/30 分流
[4] 基数指标   843 个实体完成指标与 cluster bootstrap
[5] 赛马       300/300 场有效，人均 33.5 场
[6] 统计闸门   名次 z 噪音基准=3.74（观测最大 8.55）  通过 7/843  π̂₀=0.744
[7] 可跟单性   Top7 回测完成，跟单可行性系数中位数 0.56
[8] 实时告警   选手池 main=6 sniper=1，回放产出 132 条告警

main 赛道精确率 100%   召回率 75%   （800 个赌徒 0 个混进名单）
```

---

## 架构

```
                       ① 实体聚类（反 Sybil）
                              ↓
   数据源 ─────────────→ ② PnL 结算（加权平均成本 + 执行价 + gas/FoT）
   Bitquery / Dune              ↓
   Envio / RPC / 合成       ③ 净化（bundler·sniper·MEV·wash·transfer_in）
                              ↓
                          ④ 基数指标（PF / Sortino / cluster bootstrap）
                              ↓
                          ⑤ 赛马 Elo-MMR(ρ)  ← 一场比赛 = 代币 × 24h 窗口
                              ↓
                          ⑥ 统计闸门（置换基准线 + BHY-FDR + π̂₀ 分解）
                              ↓
                          ⑦ 可跟单性回测（T+3 块延迟重放）
                              ↓
                    ⑧ 选手池 ──→ ⑨ 实时告警（P0/P1/P2 + 共识窗口）
```

---

## 模块地图

| 路径 | 职责 | 关键点 |
|---|---|---|
| `constants.py` | BSC 地址、topic0、报价资产 | Infinity 是 singleton，不能按 pair 订阅 |
| `models.py` | 统一数据模型 | `base_amount` 必须是 Transfer 的实际到账量 |
| `config.py` | 全部阈值 | 调参与敏感性分析的唯一入口 |
| `ingest/bitquery.py` | Bitquery GraphQL 适配器 | 字段映射集中在 `_map_trade()` 一个函数 |
| `ingest/dune.py` | Dune Query API 适配器 | SQL 列数直接决定 credit 消耗 |
| `ingest/loader.py` | 统一装载 + 缓存 | 真实/合成数据在这层抹平，之后代码路径完全一致 |
| `ingest/prices.py` | 报价资产喂价 | 只给 quote 侧喂价；base 侧一律用执行价 |
| `ingest/cache.py` | 本地 JSONL 缓存 | 调参阶段反复重拉的成本很难看 |
| `ingest/adapters.py` | 数据源工厂 + RPC 骨架 | QuickNode 的 "Archive" 不是真 archive |
| `ingest/synthetic.py` | 带 ground truth 的模拟宇宙 | 7 类选手，每类对应一种真实污染模式 |
| `decode/swaps.py` | V2 / V3 / Infinity / Four.meme 解码 | 归因必须回退 `tx.from`，Router 不是真人 |
| `normalize/entity.py` | 实体聚类（反 Sybil） | 区块共现必须与代币重合联合判定 |
| `pnl/engine.py` | PnL 引擎 | 转入按市价记成本，不是零成本 |
| `purify/filters.py` | 8 步净化 | sniper **分赛道不删除** |
| `features/metrics.py` | 基数指标 | bootstrap 按**代币**分块，不是按时间 |
| `race/engine.py` | 赛制与名次 | 并列必须与组首比较（否则全场并列第一） |
| `scoring/elo_mmr.py` | Elo-MMR(ρ) | log-cosh 鲁棒平均 + 激励相容 |
| `scoring/gates.py` | 统计闸门 | DSR 默认**只诊断不拦截**，理由见 docs/02 §5.4 |
| `backtest/copyable.py` | 可跟单性回测 | 展示「你能赚多少」而非「他赚了多少」 |
| `backtest/oos.py` | 时间外样本验证 | 真实数据上唯一有说服力的验证手段 |
| `realtime/watcher.py` | 实时告警 | 只跟踪选手池，不做评分 |

---

## 五个最容易踩的坑

1. **BSC 出块 0.45s（Fermi 后）≈ 192,000 块/天** —— 轮询必丢块，实时链路一律用 WSS / gRPC。
2. **BSC 上没有现成的钱包 PnL API**（Moralis 不支持 BSC），PnL 引擎必须自建。
3. **Pancake V3 ≠ Uniswap V3，Infinity 是 singleton** —— 抄 Uniswap 解析器会漏掉全部 V3 和 Infinity 交易。
4. **Four.meme 曲线阶段的交易不在任何标准 DEX 索引里** —— 而这恰恰是 alpha 最集中的一段。
5. **按胜率排序的榜单，榜首必然是狙击 / 三明治机器人**（87%+ 胜率），且对跟单者 100% 不可复制。

---

## 不想装环境？纯 SQL 版

`dune/leaderboard.sql` —— 一条 Dune 查询，网页里贴进去就出榜单，不用终端、不用 Python。

它保留了框架真正干活的那部分：**同一代币内的横截面名次 → 名次 z（零假设方差
解析已知 1/12）→ BHY 多重检验校正**。这不是妥协的选择：敏感性扫描显示，
真正的约束就是名次 z 的噪音基准线，不是 Elo 也不是 FDR 调参。

实测判别力（合成数据）：

| | 精确率 | 召回率 |
|---|---|---|
| Python 完整版 | 100% | 75%（6/8） |
| 纯 SQL 版 | 100% | 50%（4/8） |

**假阳性同样为零，代价是漏掉一半真高手。** 缺的是：实体聚类（反 Sybil）、
转账流、Elo-MMR、bootstrap CI、置换基准线、可跟单性回测。
第一刀筛选够用；要做跟单决策回 Python 版。

---

## 文档

- `HANDOFF.md` —— **迁到本地环境时把它整段贴给本地 agent**：现状、下一步、已知陷阱、未验证项
- `docs/01-架构与数据源选型.md` —— 数据源横向对比、成本区间、合约地址与事件签名、上线前验证清单
- `docs/02-评分模型细则.md` —— PnL 口径、反作弊规则、Elo-MMR 公式、统计闸门推导、可跟单性衰减模型
- `docs/03-接入真实数据源.md` —— 接线步骤、探针验收标准、三类静默算错的排查、上线检查清单

---

## 免责声明

本框架是研究与工程脚手架，不构成投资建议。链上跟单涉及智能合约风险、流动性风险与
极高的本金损失概率 —— 公开数据显示 pump.fun 类平台上仅约 5% 的钱包盈利超过 $1,000。
框架本身的设计前提就是「绝大多数候选是噪音」，请以同样的怀疑态度对待它的输出。
