-- ============================================================================
-- BSC 聪明钱赛马 · 纯 Dune SQL 版
-- ----------------------------------------------------------------------------
-- 用途：不装任何东西、不用终端，在 dune.com 网页里直接出榜单。
--
-- 它是 Python 框架的**统计核心**，不是全部。能进 SQL 的：
--   ✓ 窄范围收敛（只要窗口内新发、够开一场比赛的币）
--   ✓ 净化：bundler / sniper 分流、MEV 短持仓、样本量下限
--   ✓ 赛马：同一代币内的横截面名次 → 名次分位
--   ✓ 统计闸门：名次 z（零假设 U(0,1)，方差解析已知 1/12）+ BHY 多重检验校正
-- 进不了 SQL 的（要用 Python 版）：
--   ✗ Elo-MMR(ρ) 评分与 μ−3σ 展示值
--   ✗ 实体聚类（反 Sybil）—— 本查询是**地址级**的，一个操盘手的多个分身
--     会各自占一行。这是本版最大的已知缺陷。
--   ✗ 转账流 / 成本基准覆盖率 —— 空投与老鼠仓拦不住
--   ✗ cluster bootstrap 置信区间、置换检验噪音基准线
--   ✗ 可跟单性回测（copy_factor）
--
-- ⚠️ PnL 口径也是简化的：这里用「卖出 USD − 买入 USD」的净现金流，
--    对**已完全离场**的仓位是对的，对仍在持仓的会低估（不算浮盈）。
--    Python 版用的是移动加权平均成本 + 未实现盈亏，两者对不上是正常的。
--
-- 📊 判别力实测（合成数据，800 赌徒 + 8 真高手 + 5 类机器人）：
--       完整 Python 版   精确率 100%   召回率 75%（6/8）
--       本 SQL 版         精确率 100%   召回率 50%（4/8）
--    即：**假阳性同样为零，但会漏掉一半真高手**。作为第一刀筛选够用，
--    要做跟单决策请回到 Python 版。复刻脚本见 tests/test_sql_parity.py。
--
-- ✅ 真实 Trino 实测（2026-08-15，lookback 3 天 / Top200，medium 引擎 4s）：
--    零语法修复一次通过。normal_cdf、裸 0x 字面量（DuneSQL 扩展）、params
--    交叉连接、MIN(...) OVER 累积最小值全部按预期工作。
--    结果：141 个 z>0 地址，5 人通过闸门（main 4 + sniper 1），
--    榜首 z=3.73 —— 贴着「纯运气上限 ≈3.7」的经验线，量级自洽。
--    佐证地址级缺陷：Top4 里 3 个地址同为 64 场 / ~590 笔 / 胜率 0.6，
--    画像高度相似，疑似同一操盘手分身（SQL 版无法合并，见下）。
-- ============================================================================

WITH params AS (
    SELECT
        7      AS lookback_days,   -- 回看天数
        7      AS buffer_days,     -- 判定「老币」的回看缓冲
        50     AS min_usd,         -- 单笔成交额下限（滤粉尘）
        5      AS min_traders,     -- 代币的参与地址数下限（开不起比赛的币没信息量）
        800    AS max_tokens,      -- 代币数封顶（有损！最后一段会报出丢了多少）
        10     AS min_races,       -- 选手至少参与过几个代币
        3      AS sniper_blocks,   -- block_delta ≤ 此值 → sniper 赛道
        2      AS mev_hold_blocks, -- 持仓 < 此值 → 三明治 MEV，剔除
        0.10   AS fdr_target,      -- BHY 目标 FDR
        8      AS wash_min_trades, -- 对倒判据：同一代币成交 ≥ 此值
        0.02   AS wash_max_abs_ret -- 且净收益率绝对值 < 此值 → 判为刷量
),

-- 报价资产：base 侧的价格一律由成交额推导，不用外部喂价
quotes AS (
    SELECT * FROM (VALUES
        (0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c),  -- WBNB
        (0x55d398326f99059ff775485246999027b3197955),  -- USDT
        (0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d),  -- USDC
        (0xe9e7cea3dedca5984780bafc599bd69add087d56),  -- BUSD
        (0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d),  -- USD1
        (0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82)   -- CAKE
    ) AS t(addr)
),

win AS (
    SELECT t.block_number, t.block_time, t.tx_from AS wallet,
           t.token_bought_address AS bought, t.token_sold_address AS sold,
           t.amount_usd
    FROM dex.trades t, params p
    WHERE t.blockchain = 'bnb'
      AND t.block_time >= now() - (p.lookback_days * INTERVAL '1' DAY)
      AND t.amount_usd BETWEEN p.min_usd AND 5000000
),

-- 窗口开始前就存在的币 = 老币，不是本框架的标的
older AS (
    SELECT DISTINCT token FROM (
        SELECT token_bought_address AS token FROM dex.trades, params p
         WHERE blockchain = 'bnb'
           AND block_time <  now() - (p.lookback_days * INTERVAL '1' DAY)
           AND block_time >= now() - ((p.lookback_days + p.buffer_days) * INTERVAL '1' DAY)
        UNION ALL
        SELECT token_sold_address FROM dex.trades, params p
         WHERE blockchain = 'bnb'
           AND block_time <  now() - (p.lookback_days * INTERVAL '1' DAY)
           AND block_time >= now() - ((p.lookback_days + p.buffer_days) * INTERVAL '1' DAY)
    )
),

-- 把每笔成交归一化成 base/quote 视角。两边都是报价资产的套利腿直接丢。
legs AS (
    SELECT
        w.block_number, w.block_time, w.wallet, w.amount_usd,
        CASE WHEN w.sold IN (SELECT addr FROM quotes) THEN w.bought ELSE w.sold END AS token,
        CASE WHEN w.sold IN (SELECT addr FROM quotes) THEN 'buy' ELSE 'sell' END AS side
    FROM win w
    WHERE (w.sold   IN (SELECT addr FROM quotes))
       != (w.bought IN (SELECT addr FROM quotes))
),

tok_stats AS (
    SELECT token,
           COUNT(*)                                   AS n_trades,
           COUNT(DISTINCT wallet)                     AS n_traders,
           MIN(block_number)                          AS launch_block,
           ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) AS rn
    FROM legs
    WHERE token NOT IN (SELECT token FROM older)
      AND token NOT IN (SELECT addr FROM quotes)
    GROUP BY token
),

keep AS (
    SELECT s.token, s.launch_block, s.n_traders
    FROM tok_stats s, params p
    WHERE s.n_traders >= p.min_traders AND s.rn <= p.max_tokens
),

-- ---------------------------------------------------------------- 仓位
-- ⚠️ 净现金流口径：完全离场的仓位准确，仍持仓的低估（不含浮盈）
pos AS (
    SELECT
        l.wallet, l.token,
        SUM(CASE WHEN l.side = 'buy'  THEN l.amount_usd ELSE 0 END) AS buy_usd,
        SUM(CASE WHEN l.side = 'sell' THEN l.amount_usd ELSE 0 END) AS sell_usd,
        COUNT(*)                        AS n_trades,
        MIN(l.block_number)             AS first_block,
        MAX(l.block_number)             AS last_block,
        k.launch_block
    FROM legs l
    JOIN keep k ON k.token = l.token
    GROUP BY l.wallet, l.token, k.launch_block
),

scored AS (
    SELECT
        wallet, token, buy_usd, sell_usd, n_trades,
        sell_usd - buy_usd                          AS pnl_usd,
        (sell_usd - buy_usd) / NULLIF(buy_usd, 0)   AS ret,
        first_block - launch_block                  AS block_delta,
        last_block  - first_block                   AS hold_blocks
    FROM pos
    WHERE buy_usd > 0
),

-- ---------------------------------------------------------------- 净化
-- bundler（同块建仓）和 MEV（持仓 < 2 块）直接剔除；
-- sniper（1~3 块内建仓）**不删，单独分赛道** —— 它们真赚钱，
-- 但 alpha 来自发射前信息优势与同块执行特权，跟单者复制不了。
-- 对倒（wash trading）的 SQL 可表达特征：**同一代币成交很多笔，净收益率却≈0**
-- —— 这就是 CFTC 的定义：没承担市场风险、也没改变持仓。
-- 完整版用 SCC 环路 + 镜像成交两级判定，SQL 里做不到，这条是最接近的代理。
-- ⚠️ 真实数据上它可能误伤「反复分批进出最后打平」的正常交易者，
--    觉得杀伤过大就调高 wash_min_trades 或调低 wash_max_abs_ret。
clean AS (
    SELECT s.*,
           CASE WHEN s.block_delta <= (SELECT sniper_blocks FROM params)
                THEN 'sniper' ELSE 'main' END AS lane
    FROM scored s, params p
    WHERE s.block_delta > 0                      -- 剔除 bundler
      AND s.hold_blocks >= p.mev_hold_blocks     -- 剔除三明治 MEV
      AND NOT (s.n_trades >= p.wash_min_trades   -- 剔除对倒刷量
               AND abs(s.ret) < p.wash_max_abs_ret)
),

-- ---------------------------------------------------------------- 赛马
-- 一场比赛 = 一个代币。名次是**同一代币内的横截面比较**，
-- 因此自动剔除了代币本身的 beta（牛市普涨/熊市普跌不再污染排名）。
races AS (
    SELECT
        c.*,
        RANK()  OVER (PARTITION BY c.token ORDER BY c.ret DESC) AS rk,
        COUNT(*) OVER (PARTITION BY c.token)                    AS n_players
    FROM clean c
),

pct AS (
    SELECT wallet, lane, token, ret, pnl_usd, buy_usd, n_trades,
           1.0 - (rk - 1.0) / NULLIF(n_players - 1.0, 0) AS percentile
    FROM races
    WHERE n_players >= 5           -- 参与者太少的比赛信息量太低
),

-- ---------------------------------------------------------------- 统计闸门
-- 零假设：无技能 ⇒ 名次分位 ~ U(0,1)，均值 0.5、方差 1/12。
-- **方差是解析已知的，与收益分布形状无关** —— 这正是「赛马」相对
-- 「PnL 榜单」在统计上的根本优势（Sharpe 在 memecoin 的极端右偏分布上失效）。
agg AS (
    SELECT
        wallet,
        MAX(lane)                       AS lane,   -- 有任一 sniper 仓位就归 sniper
        COUNT(*)                        AS n_races,
        AVG(percentile)                 AS avg_pct,
        SUM(pnl_usd)                    AS net_pnl,
        SUM(buy_usd)                    AS invested_usd,
        SUM(n_trades)                   AS n_trades,
        AVG(CASE WHEN ret > 0 THEN 1.0 ELSE 0.0 END) AS hit_rate,
        SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END)
          / NULLIF(-SUM(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END), 0) AS profit_factor
    FROM pct
    GROUP BY wallet
),

z AS (
    SELECT a.*,
           (a.avg_pct - 0.5) / sqrt((1.0 / 12.0) / a.n_races) AS race_z
    FROM agg a, params p
    WHERE a.n_races >= p.min_races
),

-- 双尾 p 值。Trino 自带 normal_cdf。
pv AS (
    SELECT z.*, 2.0 * (1.0 - normal_cdf(0.0, 1.0, abs(z.race_z))) AS p_raw
    FROM z
),

-- Benjamini-Hochberg-Yekutieli（控制 FDR，允许任意相关结构）。
-- **必须用 BHY 而不是 BH**：地址收益高度相关，大家买的是同一批代币。
--     p_adj(i) = min[ p_adj(i+1),  M·c(M)/i · p(i) ],  c(M) = Σ 1/j ≈ ln M + γ
ranked_p AS (
    SELECT pv.*,
           ROW_NUMBER() OVER (ORDER BY p_raw ASC) AS i,
           COUNT(*)     OVER ()                   AS m_total
    FROM pv
),
bhy AS (
    SELECT r.*,
           LEAST(1.0, MIN(
               r.m_total * (ln(CAST(r.m_total AS double)) + 0.5772156649)
               / r.i * r.p_raw
           ) OVER (ORDER BY r.i DESC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
           ) AS p_bhy
    FROM ranked_p r
)

SELECT
    ROW_NUMBER() OVER (ORDER BY b.race_z DESC)      AS "#",
    b.lane                                          AS "赛道",
    b.wallet                                        AS "地址",
    ROUND(b.race_z, 2)                              AS "名次 z",
    ROUND(b.avg_pct, 3)                             AS "平均名次分位",
    b.n_races                                       AS "参赛代币数",
    b.n_trades                                      AS "成交笔数",
    ROUND(b.hit_rate, 2)                            AS "胜率",
    ROUND(b.profit_factor, 2)                       AS "盈亏比",
    ROUND(b.net_pnl, 0)                             AS "净盈亏USD",
    ROUND(b.invested_usd, 0)                        AS "累计投入USD",
    CAST(b.p_bhy AS decimal(18,10))                 AS "BHY校正p",
    CASE WHEN b.p_bhy <= (SELECT fdr_target FROM params)
              AND b.profit_factor > 1.2
              AND b.net_pnl > 0
         THEN '✅ 通过' ELSE '—' END                 AS "闸门"
FROM bhy b
WHERE b.race_z > 0
ORDER BY b.race_z DESC
LIMIT 200

-- ============================================================================
-- 怎么读这张表
-- ----------------------------------------------------------------------------
-- · 只看「闸门 = ✅ 通过」且「赛道 = main」的行。**通过的人很可能是 0 个** ——
--   那是正常的、也是正确的。参照量级：受监管的共同基金行业里费后真有技能者
--   只有 0.6%，匿名高摩擦的 BSC memecoin 只会更低。
--
-- · 「名次 z」是核心。z > 3.7 大致是 800 个候选下的纯运气上限（Python 版用
--   置换检验精确标定这条线，SQL 版给不了，用 3.7 当经验阈值）。
--
-- · **sniper 赛道的人不要跟单**。他们是真赚钱的，但你复制不了同块执行特权。
--   把他们当信号源看：他们买了什么。
--
-- · 这张表是**地址级**的。一个操盘手用 20 个分身分仓，运气最好的那个会出现在
--   榜首，19 个亏损兄弟被忽略 —— 这是幸存者偏差，SQL 版没法修。
--   看到几个地址的行为高度相似（同样的代币、相近的进场区块），八成是同一个人。
--
-- · 榜单是**回看**选出的。上榜那一刻往往正是运气峰值。真要用，
--   必须做时间外样本验证（Python 版的 `smrace.cli oos`）。
-- ============================================================================
