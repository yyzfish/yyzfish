# 本地接手提示词

> **2026-08-15 更新：真实数据已跑通。** 本文档下述任务的完成情况见文末
> 「真实数据首跑记录」，未完成项也在那里。原文保留作背景。

> 把下面「提示词正文」整段贴给本地的 Claude Code（或任意能读写文件、能联网、能跑 shell 的 agent）。
> 后半部分「附录」是给你自己看的，不用贴。

---

## 提示词正文（从这里开始复制）

你接手一个已经写好的项目：**BSC 链上「聪明钱」赛马评分框架**。代码在当前目录，
约 5,500 行 Python + 一条 Dune SQL，40 个测试全绿。

**前一个环境没有外网**（沙箱的出网白名单只放行包管理源，api.dune.com /
streaming.bitquery.io 一律 403），所以所有涉及真实数据的部分都**只写了、没跑过**。
你的核心价值就在这里：**你有网，请把它们真正跑通。**

### 项目在做什么

把 BSC 上 meme/新币赛道的上万个地址当成赛马选手跑一个赛季，用统计上站得住脚的
方式选出真有技能的少数人，回测「跟单能拿到多少」，最后只对这批人做秒级实时跟踪。

管线顺序（不可颠倒）：
```
ingest → 实体聚类 → PnL 结算 → 净化 → 基数指标
       → 赛马 Elo-MMR → 统计闸门 → 可跟单性回测 → 选手池 → 实时告警
```

先读这三份文档，它们解释了每个设计决策的**理由**，不要绕过它们直接改代码：
- `README.md` —— 全貌与模块地图
- `docs/02-评分模型细则.md` —— 所有公式与口径，以及为什么这么定
- `docs/03-接入真实数据源.md` —— 接线步骤与验收标准

### 先确认基线没坏

```bash
pip install -r requirements.txt
python tests/test_core.py         # 12 个：PnL 口径、Elo-MMR、统计闸门
python tests/test_adapters.py     # 26 个：字段映射、成本估算、转账流
python tests/test_sql_parity.py   # 2 个：SQL 版 vs Python 版判别力对照
python -m smrace.cli validate     # 合成数据端到端，应得 main 精确率 100% / 召回 75%
```

四个都过了再往下。任何一个不过，先修它，不要在坏基线上加东西。

### 任务一：把真实数据跑通（最高优先级）

需要环境变量：
```bash
export DUNE_API_KEY=...            # dune.com → Settings → API keys
export BITQUERY_TOKEN=...          # bitquery.io → 控制台 → API token
export SMRACE_BNB_PRICE_FLAT=600   # 先跑通用固定价；正式改用 SMRACE_BNB_PRICE_CSV
```

**第一件事永远是探针，不是直接跑管线：**

```bash
python -m smrace.cli probe --source bitquery --minutes 10
```

它会拉 10 分钟真数据并自动做三项合理性检查。**验收标准**：

| 检查 | 通过线 | 不通过时改哪里 |
|---|---|---|
| 有成交返回 | > 0 笔 | `smrace/ingest/bitquery.py::_map_trade()` 的字段名 |
| gas 中位数 | $0.005–2 | `BitquerySource.gas_price_unit`（wei/gwei/bnb） |
| 最活跃地址占比 | < 30% | **wallet 取错了，八成取成了 Router** |
| venue 列表 | 含 v2/v3/infinity | Infinity 没出现 = 数据源没收录 |

三项全绿之后走 Dune：

```bash
python -m smrace.cli bootstrap --days 7 --max-tokens 800
```

它会：校验凭证（0 credit）→ 建 Query → 估账单 → **停下来等确认**。
Free 档没有 CRUD API，自动建 Query 会返回 402/403，这不是 bug ——
它会把 SQL 写到 `out/dune_queries.sql`，手工贴进 Dune 存成 Query 再把 id 传回来。

确认账单可接受后加 `--yes` 才真跑。**Dune Free 档 2,500 credits/月 ≈ 31 万行导出**，
超了要补 credit，而 Free 档的超额单价（$5.00/100）比 Analyst（$1.875/100）贵 2.7 倍
—— 卡在「Free 档超额跑」是最贵的选择。

### 任务二：验证那条 Dune SQL

`dune/leaderboard.sql` **从未在真实 Trino 上执行过**。请：

1. 先把 `params` 里的 `lookback_days` 改成 3、`max_tokens` 改成 200 再跑，确认语法
2. 修掉所有语法错误（重点怀疑：`normal_cdf` 的可用性、`VALUES` 里裸十六进制地址的
   字面量写法、`params` 交叉连接的展开方式、窗口函数里 `MIN(...) OVER (ORDER BY ... DESC)`
   的累积最小值语义）
3. 跑通后把实际结果和 `tests/test_sql_parity.py` 的预期对照，把真实数字回填进
   SQL 头部的「判别力实测」段

### 任务三：填两张白名单

`smrace/constants.py` 里这两个集合目前是**空的**，直接影响正确性：

```python
BRIDGE_ADDRESSES = { ... }   # Stargate / LayerZero OFT / Celer cBridge / Binance Bridge
CEX_HOT_WALLETS  = { ... }   # Binance / OKX / Bybit 热钱包
MEV_BUILDERS     = { ... }   # 48Club Puissant / bloXroute / BlockRazor
```

不填也能跑，但这两类转入会落进 `UNKNOWN`，分不清来源。

### 任务四：真实数据上的验证（没有 ground truth 时唯一有说服力的手段）

```bash
python -m smrace.cli oos --source dune --days 7 --split 0.67
```

用前 67% 时间选人，看这批人在没参与选拔的后 33% 表现如何，与「同期落选者」对照。

**判读**：两项置换检验 p 都 > 0.05 ⇒ 优势没延续到样本外，很可能只是过去运气好，
**不要上跟单**。选中组失活率高 ⇒ 就算显著也没用，跟不了一个已经不交易的地址。

合成数据上会很显著（技能是内置的）；**真实数据上要预期弱得多**，这是正常的。

---

## ⚠️ 已知陷阱（都是实测踩出来的，别重蹈覆辙）

这些坑的共同点是：**不会报错，只会静默算错**。管线照样跑完、照样出榜单，只是错的。

1. **wallet 必须取 `tx.From`，绝不能取 `Buy.Buyer` / `Sell.Seller`。**
   V3 / Infinity 上那是 Router，取错会把成千上万个用户归成一个地址，框架彻底失效。
   `tests/test_adapters.py::test_bitquery_wallet_is_tx_from_not_router` 锁着这条。

2. **并列名次必须与「并列组组首」比较，不能与前一名比较。**
   memecoin 一场比赛里收益率是连续分布的（大家都亏 55~65%），用相邻间距判并列
   会导致**全场并列第一**，名次信息归零、Elo 退化成噪音。实测中这个 bug 曾把
   赌徒的中位 z 从 −0.19 抬到 +4.27，完全掩盖真实信号。

3. **DSR / Sharpe 默认不作为硬闸门**（`gate.dsr_as_gate=False`）。
   memecoin 单笔收益偏度 >5，DSR 分母塌陷，实测会把 **8/8 真高手全部误杀**。
   主闸门用名次分位 z —— 它的零假设方差解析已知（1/12），与收益分布形状无关。
   **不要"顺手"把 DSR 打开**，除非你换了流动性好得多的标的池。

4. **E[max SR] 公式里的方差必须是零假设方差，不能用横截面方差。**
   memecoin 人群的横截面 Sharpe 均值能低到 −0.63，横截面方差反映的是
   「真实技能离散度 + 系统性亏损」，代进去会得到荒谬的高基准线（实测 1.557），
   把所有人一刀切掉。现在用置换检验标定。

5. **实体 id 必须用完整根地址。** 曾经用 `root[:12]` 截断，导致 5 个不同的
   sybil 簇撞成同一个 id，表现为「所有女巫被合并成一个巨型实体」——
   而聚类算法本身是对的，是命名撞了。

6. **转账流必须丢掉与 swap 同一笔 tx 的 transfer。** 一笔 swap 同时产生 Swap 事件
   和两条 ERC20 Transfer，都进 PnL 引擎会让同一笔买入记两次，
   每个活跃地址的成本基准直接翻倍。

7. **内部转账只能在实体聚类之后回标。** 摄入时无法知道两个地址是不是同一个人。
   不回标的话，操盘手在自己多个地址间倒仓，每倒一次就凭空产生一次「盈利」。

8. **sniper 不要一刀切删除，要单独分赛道。** 它们是真赚钱的（实证 87% 胜率），
   但 alpha 来自发射前信息优势 + 同块执行特权，跟单者 100% 复制不了。
   当信号源用（它们买了什么），不当跟单对象。

9. **Four.meme 曲线阶段的交易不在 `dex.trades` 里**，毕业事件是从 Four.meme 合约
   （`0x5c95…`）发出的，不是 Pancake Factory。只靠 Factory 事件会把 launch_block
   记成「毕业时刻」，导致所有 `block_delta`（bundler/sniper 判定）失真。

10. **BSC 出块 0.45s（Fermi 硬分叉后）≈ 192,000 块/天。** 实时链路一律用
    WSS / gRPC 推送，轮询必丢块。

---

## 未验证项（必须用真数据确认，不要当成事实）

- [ ] **V3 / Infinity 的 topic0**（`smrace/constants.py::TOPIC0`）：ABI 来自官方仓库，
      但哈希是本地 keccak 算的，**没找到第三方文档印证**。用一笔真实交易的 log 核对。
- [ ] **Dune 的 `dex.trades on bnb` 是否收录 PancakeSwap Infinity**：
      跑 `SELECT DISTINCT project, version FROM dex.trades WHERE blockchain='bnb'`。
      没收录就必须自建索引补，否则新盘/高频那段的聪明钱整段缺失。
- [ ] **Dune Free 档 2,500 credits/月**：只在官方 FAQ 单处提到，pricing 页是 JS 渲染
      抓不到。登录后自己核。
- [ ] **`--bytes-per-row` 默认 420** 是拍的，用「实际扣的 credit ÷ 预估」校准一次。
- [ ] **Bitquery GasPrice 的单位**（wei/gwei/bnb），探针的 gas 检查会暴露。
- [ ] **Bitquery archive 数据集是否额外计费**、历史深度上限 —— 需与销售确认。
      常规查询只覆盖最近约 30 天。
- [ ] **Infinity Bin 池的 Swap 事件 ABI** —— 只拿到了 CL 池的。
- [ ] **`erc20_bnb.evt_Transfer` 表名** —— `erc20_bnb` / `bep20_bnb` 两种命名都出现过。

---

## 不要做的事

- **不要改 `smrace/scoring/gates.py` 的闸门顺序或把 DSR 设为硬闸门**，除非你读完了
  `docs/02 §5.4` 并有真实数据支撑。
- **不要为了「让榜单上有人」去放松阈值。** 通过闸门的人很可能是 0 个，那是正确的 ——
  受监管的共同基金行业费后真有技能者只有 0.6%，匿名高摩擦的 BSC memecoin 只会更低。
  榜单的价值取决于假阳性率，不是覆盖率。
- **不要把 Top-N 截断做成静默的。** 任何丢弃都要 log 出丢了多少，
  否则会让人误以为覆盖了全部。
- **不要把凭证写进仓库任何文件**，包括配置示例和缓存。

## 改完怎么验证没破坏东西

```bash
python tests/test_core.py && python tests/test_adapters.py && \
python tests/test_sql_parity.py && python -m smrace.cli validate
```

`validate` 的基线是 **main 赛道精确率 100%、召回率 75%（6/8）、800 个赌徒 0 个混入**。
精确率掉下来就是回归，尤其注意「有赌徒混进 main 名单」——那说明闸门破了。

## 提示词正文结束

---

# 附录（给你自己看的，不用贴）

## 交接时的真实状态

| 模块 | 状态 |
|---|---|
| 合成数据端到端 | ✅ 跑通，main 精确率 100% / 召回 75% |
| PnL 引擎、净化、Elo-MMR、统计闸门 | ✅ 完成 + 单测 |
| 可跟单性回测、时间外样本验证 | ✅ 完成 |
| Bitquery / Dune 适配器 | ⚠️ **写完但从未联网跑过** |
| 转账流 / 资金流 | ⚠️ 同上，且两张地址白名单是空的 |
| Dune SQL 版 | ⚠️ **从未在真实 Trino 上执行** |
| 实时层 `stream()` | ❌ 未实现（要 `pip install websockets` 或 grpcio） |
| Four.meme 曲线阶段解码 | ❌ 未实现 |

## 两个凭证

本次会话中在对话里贴过 Dune 和 Bitquery 的 key，**都请去后台轮换**。
前一个环境无法联网，那两串 key 一次都没被实际使用过，但既然进了记录就该换掉。

## 建议的推进顺序

1. 基线四项测试全绿
2. `probe` 三项检查全绿（这一步最容易卡，也最重要）
3. `bootstrap` 小范围跑通（3 天 / Top200），确认账单口径
4. `oos` 出结果 —— 这才是「这套东西到底有没有用」的答案
5. 有余力再补：实时层、Four.meme 解码、Envio 适配器

---

# 真实数据首跑记录（2026-08-15）

## 四个任务的结果

**任务一 · probe / bootstrap ✅**
- `probe --source bitquery`：10 分钟拉 41,029 笔、10,654 地址，三项检查全绿
  （gas 中位 $0.031、最活跃地址 2.5%、venue 含 v2/v3/infinity/fourmeme）。
- 本 Bitquery token 是低档套餐：无 archive（`combined` 会 403，适配器已自动降级
  `realtime` 并告警）、限流 ~10 req/min（用 `BITQUERY_RPM=8` 压住）。
- 本 Dune key **有 CRUD 权限**，bootstrap 全自动建 Query 可用，不需要手工贴 SQL。
- `bootstrap --days 3 --max-tokens 200 --min-usd 500 --yes` 真跑成功：
  111,917 行 ≈ 916 credits（免费额度 37%）。**7 天/Top800 要 28,334 credits，
  免费档跑不动**；省钱的有效杠杆是 `--min-usd`（头部代币行数被零售小单主导，
  砍 `--max-tokens` 几乎不省）。
- 计费校准（`--bytes-per-row`）还没做：API 看不到扣费明细，需要登录
  dune.com 后台对一次「实际扣除 ÷ 预估 906」。

**任务二 · Dune SQL ✅**
- `dune/leaderboard.sql` 在真实 Trino 上**零语法修复一次通过**（3 天/Top200，
  medium 引擎 4s）。normal_cdf、裸 0x 字面量、params 交叉连接、窗口累积最小值
  全部按预期工作。实测数字已回填 SQL 头部。
- 真实结果：141 个 z>0 地址，5 人过 SQL 版闸门；Top4 中 3 个地址画像高度相似
  （64 场/~590 笔/胜率 0.6），疑似同一人分身 —— 地址级缺陷的活例证。

**任务三 · 白名单 ✅**
- 桥 40 / CEX 热钱包 32 / MEV builder 48，全部来自官方部署文件或两份独立
  BscScan 标签库交叉验证，出处写在 `constants.py` 注释里。
- Bitget 无法核验到 BSC 标签地址，宁缺毋滥未收录。

**任务四 · oos ✅（机制通，结论=不要跟单）**
- 3 天窗口（1786547052~1786806252，缓存在 out/cache/）：训练期 0 人过闸门 ⇒
  无可跟单对象；对照组 210 人测试期中位名次 z=-0.03（纯噪音）、失活率 81%。
- 完整管线结果：424/10,447 实体过净化，置换噪音基准 z=3.52 >
  观测最大 z=3.33，**0/260 过闸门**。π̂₀=0.864（估计 ~35 人有技能但个体
  不可分辨）。这与文档预期一致：3 天窗口 + $500 单笔下限太薄，
  想出结论至少要 7 天 + 更低 min_usd —— 那需要付费档或自建索引。

## 未验证项清单的销账

- [x] V3 / Infinity CL 的 topic0 —— 与链上真实日志逐字节一致（constants.py 已记）
- [x] Dune 收录 Infinity —— `infinity_cl` + `infinity_lb` 都在 `dex.trades`，
      **过滤 venue 用这两个 version 字符串**
- [x] Bitquery GasPrice 单位 = **BNB**（实测 5e-11 = 0.05 gwei；默认已改）
- [x] `erc20_bnb.evt_Transfer` 表名正确（非 bep20_bnb）
- [x] normal_cdf 在 DuneSQL 可用
- [ ] Dune Free 档 2,500 credits/月 —— 未在后台核对（API 看不到账单页）
- [ ] `--bytes-per-row=420` 校准 —— 同上，要后台实际扣费数
- [ ] Bitquery archive 计费/深度 —— 本 token 无 archive，问题变成「要不要买」
- [ ] Infinity Bin 池 Swap ABI —— 仍缺

## 本次修的接线 bug（都写了测试或有 loud 告警）

1. Dune Query 参数化：原实现内联时间戳但 execute 传参 → 400；且换窗口复用
   query_id 会**静默跑旧窗口**。现在建 Query 声明 `{{start_ts}}/{{end_ts}}`。
2. `quote_list` 必须渲染成裸 0x 字面量：`dex.trades` 地址列是 varbinary，
   带引号的 varchar 在 IN 里直接类型错误。
3. Bitquery `DEXTrades` 混有 NFT 市场成交（实测 seaport）：一侧是 WBNB 的
   NFT 单会被记成 memecoin 买入。已按 `NFT_MARKETPLACE_PROTOCOLS` 前缀过滤。
4. `permutation_noise_floor` 对 0 场次实体除零崩溃（真实数据才会出现）。
   已剔除并加测试 `test_noise_floor_tolerates_zero_race_entities`。

## 剩余未实现（与交接时相同）

- `DuneSource.fetch_flows` / `fetch_tokens`（转账流、建池事件）—— loader 会
  响亮降级：cost_coverage 恒 1.0、launch_block 用首笔成交近似
- 实时层 `stream()`、Four.meme 曲线阶段解码、Envio 适配器
