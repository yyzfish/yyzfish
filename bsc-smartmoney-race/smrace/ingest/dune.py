"""Dune 适配器：dex.trades on bnb → 归一化 Trade。

用法有两种：
  A) 把 `TRADES_SQL` 存成一个 Dune Query，把 query_id 传进来（推荐，最省 credit）
  B) 传 sql= 自己的语句，本类只负责执行、轮询、翻页、归一化

⚠️ **credit 按「数据点 = 行 × 列」计费**，拉 90 天 BSC 明细极易爆量。
本文件的 SQL 已经把列压到最少；如果还是太贵，就在 Dune 侧先聚合到
(wallet, token, day) 再导出，不要把逐笔明细拖回本地。

⚠️ **不要把 Dune 当唯一数据源**：2026-08-06 发生过 BNB 相关表停更约一天的事故。
本框架的定位是把它放在 L3 校验层，和自建索引对账。

上线前先跑一次：
    SELECT DISTINCT project, version FROM dex.trades WHERE blockchain='bnb'
确认 PancakeSwap Infinity 是否已被收录。**没收录就必须自建索引补这一块**，
否则新盘/高频那部分的聪明钱会整段缺失。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from ..constants import QUOTE_TOKENS, STABLE_QUOTES
from ..models import Side, Trade
from .http import ApiError, HttpClient
from .prices import PriceOracle, default_oracle

API = "https://api.dune.com/api/v1"

# 列数直接决定 credit 消耗 —— 每一列都是必需的，不要随手加。
# gas 走可选 join：dex.trades 本身没有 gas，需要关联 bnb.transactions。
TRADES_SQL = """
SELECT
    t.block_number                          AS block_number,
    to_unixtime(t.block_time)               AS ts,
    t.tx_hash                               AS tx_hash,
    t.evt_index                             AS evt_index,
    t.tx_from                               AS wallet,
    t.token_bought_address                  AS bought_addr,
    t.token_sold_address                    AS sold_addr,
    t.token_bought_amount                   AS bought_amt,
    t.token_sold_amount                     AS sold_amt,
    t.amount_usd                            AS amount_usd,
    t.project                               AS project,
    t.version                               AS version
    {gas_select}
FROM dex.trades t
{gas_join}
WHERE t.blockchain = 'bnb'
  AND t.block_time >= from_unixtime(CAST({start_ts} AS bigint))
  AND t.block_time <  from_unixtime(CAST({end_ts}   AS bigint))
  AND t.amount_usd BETWEEN 10 AND 5000000
ORDER BY t.block_number, t.evt_index
"""

GAS_SELECT = """,
    x.gas_used                              AS gas_used,
    x.gas_price                             AS gas_price"""

GAS_JOIN = """
LEFT JOIN bnb.transactions x
       ON x.hash = t.tx_hash
      AND x.block_number = t.block_number"""

# ---------------------------------------------------------------- 窄范围版
# 全量 dex.trades 对免费额度是灾难性的。但这个框架只关心 meme / 新币，
# 三层收敛能把导出量砍掉一到两个数量级：
#
#   1. 只要**窗口内首次出现**的代币（老币不是本框架的标的）
#   2. 只要**参与地址数 ≥ min_traders** 的代币 —— 参与者不足的代币
#      连一场比赛都开不起来（race.min_participants），导出它们是纯浪费
#   3. 按活跃度取 Top-N 代币，硬性封顶
#
# ⚠️ 第 3 条是**有损**的。被砍掉多少条在 estimate 里会明确报出来，
#    不要让截断静默发生 —— 那会让人误以为覆盖了全部。
SCOPED_TRADES_SQL = """
WITH win AS (
    SELECT block_number, block_time, tx_hash, evt_index, tx_from,
           token_bought_address, token_sold_address,
           token_bought_amount, token_sold_amount, amount_usd, project, version
    FROM dex.trades
    WHERE blockchain = 'bnb'
      AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
      AND block_time <  from_unixtime(CAST({end_ts}   AS bigint))
      AND amount_usd BETWEEN {min_usd} AND 5000000
),
tok AS (   -- 把每笔成交拆成 (代币, 地址) 两行，便于统计
    SELECT token_bought_address AS token, tx_from AS w FROM win
    UNION ALL
    SELECT token_sold_address,   tx_from        FROM win
),
-- 窗口开始前 {buffer_days} 天内出现过的代币 = 老币，排除
older AS (
    SELECT DISTINCT token FROM (
        SELECT token_bought_address AS token FROM dex.trades
         WHERE blockchain='bnb'
           AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
                             - INTERVAL '{buffer_days}' DAY
           AND block_time <  from_unixtime(CAST({start_ts} AS bigint))
        UNION ALL
        SELECT token_sold_address FROM dex.trades
         WHERE blockchain='bnb'
           AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
                             - INTERVAL '{buffer_days}' DAY
           AND block_time <  from_unixtime(CAST({start_ts} AS bigint))
    )
),
ranked AS (
    SELECT token,
           COUNT(*)                                   AS n_trades,
           COUNT(DISTINCT w)                          AS n_traders,
           ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) AS rn
    FROM tok
    WHERE token NOT IN (SELECT token FROM older)
      AND token NOT IN ({quote_list})
    GROUP BY token
    HAVING COUNT(DISTINCT w) >= {min_traders}
),
keep AS (SELECT token FROM ranked WHERE rn <= {max_tokens})
SELECT
    t.block_number                          AS block_number,
    to_unixtime(t.block_time)               AS ts,
    t.tx_hash                               AS tx_hash,
    t.evt_index                             AS evt_index,
    t.tx_from                               AS wallet,
    t.token_bought_address                  AS bought_addr,
    t.token_sold_address                    AS sold_addr,
    t.token_bought_amount                   AS bought_amt,
    t.token_sold_amount                     AS sold_amt,
    t.amount_usd                            AS amount_usd,
    t.project                               AS project,
    t.version                               AS version
    {gas_select}
FROM win t
{gas_join}
WHERE t.token_bought_address IN (SELECT token FROM keep)
   OR t.token_sold_address   IN (SELECT token FROM keep)
ORDER BY t.block_number, t.evt_index
"""

# 代币转账流：补成本基准。**必须排除 swap 腿**，否则同一笔买入被记两次 ——
# 这里用「tx_hash 不在 dex.trades 里」来排除，比按地址判断干净得多。
FLOWS_SQL = """
WITH swap_tx AS (
    SELECT DISTINCT tx_hash FROM dex.trades
    WHERE blockchain = 'bnb'
      AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
      AND block_time <  from_unixtime(CAST({end_ts}   AS bigint))
)
SELECT
    evt_block_number                        AS block_number,
    to_unixtime(evt_block_time)             AS ts,
    evt_tx_hash                             AS tx_hash,
    contract_address                        AS token,
    "from"                                  AS sender,
    to                                      AS receiver,
    value                                   AS raw_amount
FROM erc20_bnb.evt_Transfer
WHERE evt_block_time >= from_unixtime(CAST({start_ts} AS bigint))
  AND evt_block_time <  from_unixtime(CAST({end_ts}   AS bigint))
  AND evt_tx_hash NOT IN (SELECT tx_hash FROM swap_tx)   -- ★ 排除 swap 腿
  AND "from" != 0x0000000000000000000000000000000000000000
ORDER BY evt_block_number
"""
# ⚠️ 表名以你的 Dune 环境为准（erc20_bnb / bep20_bnb 两种命名都出现过），
#    raw_amount 是未除以 decimals 的原始值，需要 join tokens.erc20 拿 decimals。
#    这个查询的数据量比成交明细大得多 —— **务必先 estimate，别直接跑。**

# 跑前先算账单：只返回几个聚合数，几乎不消耗导出额度。
ESTIMATE_SQL = """
WITH win AS (
    SELECT block_time, tx_from, token_bought_address, token_sold_address, amount_usd
    FROM dex.trades
    WHERE blockchain = 'bnb'
      AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
      AND block_time <  from_unixtime(CAST({end_ts}   AS bigint))
      AND amount_usd BETWEEN {min_usd} AND 5000000
),
tok AS (
    SELECT token_bought_address AS token, tx_from AS w FROM win
    UNION ALL
    SELECT token_sold_address,   tx_from        FROM win
),
older AS (
    SELECT DISTINCT token FROM (
        SELECT token_bought_address AS token FROM dex.trades
         WHERE blockchain='bnb'
           AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
                             - INTERVAL '{buffer_days}' DAY
           AND block_time <  from_unixtime(CAST({start_ts} AS bigint))
        UNION ALL
        SELECT token_sold_address FROM dex.trades
         WHERE blockchain='bnb'
           AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
                             - INTERVAL '{buffer_days}' DAY
           AND block_time <  from_unixtime(CAST({start_ts} AS bigint))
    )
),
ranked AS (
    SELECT token, COUNT(*) AS n_trades, COUNT(DISTINCT w) AS n_traders,
           ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) AS rn
    FROM tok
    WHERE token NOT IN (SELECT token FROM older)
      AND token NOT IN ({quote_list})
    GROUP BY token
    HAVING COUNT(DISTINCT w) >= {min_traders}
)
SELECT
    (SELECT COUNT(*) FROM win)                                   AS rows_full,
    (SELECT COUNT(*) FROM ranked)                                AS tokens_eligible,
    (SELECT COALESCE(SUM(n_trades),0) FROM ranked)               AS rows_eligible,
    (SELECT COALESCE(SUM(n_trades),0) FROM ranked WHERE rn <= {max_tokens})
                                                                 AS rows_scoped,
    (SELECT COUNT(*) FROM ranked WHERE rn > {max_tokens})        AS tokens_truncated,
    (SELECT COALESCE(SUM(n_trades),0) FROM ranked WHERE rn > {max_tokens})
                                                                 AS rows_truncated
"""

# 新建池 → launch_block（block_delta 的 t=0）。
# ⚠️ Four.meme 曲线阶段的交易不在 dex.trades 里，毕业事件也是从 Four.meme
# 合约发出的 —— 只靠这个查询会把 launch_block 记成「毕业时刻」，
# 使所有 bundler / sniper 判定失真。Four.meme 那段必须另走 Bitquery 或自建解码。
POOLS_SQL = """
SELECT
    MIN(block_number)                       AS launch_block,
    MIN(to_unixtime(block_time))            AS launch_ts,
    token                                   AS token
FROM (
    SELECT block_number, block_time, token_bought_address AS token
    FROM dex.trades WHERE blockchain='bnb'
      AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
      AND block_time <  from_unixtime(CAST({end_ts}   AS bigint))
    UNION ALL
    SELECT block_number, block_time, token_sold_address AS token
    FROM dex.trades WHERE blockchain='bnb'
      AND block_time >= from_unixtime(CAST({start_ts} AS bigint))
      AND block_time <  from_unixtime(CAST({end_ts}   AS bigint))
)
GROUP BY token
"""


# ---------------------------------------------------------------- 计费模型
# 数据来源：Dune 官方 API Billing / Pricing FAQs（见 docs/01）。
# ⚠️ Free 档的 2,500 credits/月 只在 FAQ 单处提到，官网 pricing 页是 JS 渲染
#    抓不到 —— 登录后请自己核一遍再依赖这个数字。
DUNE_TIERS = {
    #        每 MB 扣的 credit,  超额单价（美元/100 credits）, 月度赠送 credits
    "free":    {"credits_per_mb": 20, "usd_per_100": 5.000,  "monthly": 2500},
    "analyst": {"credits_per_mb": 10, "usd_per_100": 1.875,  "monthly": None},
    "plus":    {"credits_per_mb": 2,  "usd_per_100": 1.596,  "monthly": None},
}

# 一行 JSON（含键名）的经验字节数。第一次真跑完请用「实际扣的 credit / 预估」
# 校准这个值 —— 它是整个估算里唯一的经验参数。
BYTES_PER_ROW_DEFAULT = 420


def estimate_cost(n_rows: int, bytes_per_row: int = BYTES_PER_ROW_DEFAULT) -> dict:
    """把行数换算成各档的 credit 与美元。跑之前就知道账单，不用事后才发现超了。"""
    mb = n_rows * bytes_per_row / 1_048_576
    out: dict[str, Any] = {"n_rows": n_rows, "mb": mb, "bytes_per_row": bytes_per_row}
    for name, t in DUNE_TIERS.items():
        credits = mb * t["credits_per_mb"]
        row: dict[str, Any] = {
            "credits": credits,
            "usd_if_overage": credits / 100.0 * t["usd_per_100"],
        }
        if t["monthly"]:
            row["monthly_allowance"] = t["monthly"]
            row["fits_in_free_quota"] = credits <= t["monthly"]
            row["pct_of_quota"] = credits / t["monthly"]
        out[name] = row
    return out


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _addr(x: Any) -> str:
    return str(x or "").lower()


@dataclass
class DuneSource:
    name: str = "dune"
    api_key: str = ""
    query_id: int | None = None       # 方案 A：预存的 Query
    sql: str | None = None            # 方案 B：直接给语句（需要 Plus 档的 Query API）
    rpm: int = 60                     # Free 15–40 / Plus 70–200，按自己的档调
    page_size: int = 20_000
    poll_interval: float = 3.0
    poll_timeout: float = 1800.0
    performance: str = "medium"       # medium | large（large 更贵更快）
    with_gas: bool = True
    # ---- 窄范围参数（免费额度下能不能跑通，全看这几个）
    scoped: bool = True               # 只拉窗口内新发的币
    min_usd: float = 10.0             # 单笔成交额下限
    min_traders: int = 5              # 参与地址数下限 = race.min_participants
    max_tokens: int = 2000            # 代币数硬性封顶（有损，会在 estimate 里报出）
    buffer_days: int = 7              # 判定「老币」的回看缓冲
    oracle: PriceOracle = field(default_factory=default_oracle)
    verbose: bool = True
    _client: HttpClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.getenv("DUNE_API_KEY", "")
        if not self.api_key:
            raise ApiError("缺少 DUNE_API_KEY 环境变量")
        self._client = HttpClient(
            base_headers={"X-Dune-API-Key": self.api_key,
                          "Content-Type": "application/json"},
            rpm=self.rpm,
            on_retry=(lambda i, m: print(f"  [dune] 重试 {i}: {m}")) if self.verbose else None,
        )

    # ------------------------------------------------------------ 鉴权与建 Query
    def check_auth(self) -> bool:
        """零 credit 的凭证校验：故意查一个不存在的 execution。
        key 无效 → 401；key 有效 → 404/400。不会触发任何查询。"""
        assert self._client is not None
        try:
            self._client.get_json(f"{API}/execution/01ZZZZZZZZZZZZZZZZZZZZZZZZ/status")
            return True
        except ApiError as e:
            if e.status in (401, 403):
                return False
            return True   # 404/400 说明鉴权过了，只是 id 不存在

    def create_query(self, name: str, sql: str, is_private: bool = True) -> int:
        """用 CRUD API 建一个 Query，省掉手工往网页里贴 SQL。

        ⚠️ CRUD API 通常需要付费档。Free 档大概率返回 402/403 —— 调用方应当
        捕获并退回「手工贴 SQL」的路径，而不是直接失败。
        2026-08-15 实测：本 key 的 CRUD 直接可用（HTTP 200）。

        SQL 里含 {{start_ts}} / {{end_ts}} 时自动声明为 number 参数 ——
        execute() 传的 query_parameters 必须与 Query 声明的参数一致，
        多传会 400 (unknown parameters)，少传会用默认值静默跑错窗口。
        """
        assert self._client is not None
        body: dict[str, Any] = {"name": name, "query_sql": sql, "is_private": is_private}
        params = [{"key": k, "type": "number", "value": "0"}
                  for k in ("start_ts", "end_ts") if "{{" + k + "}}" in sql]
        if params:
            body["parameters"] = params
        r = self._client.post_json(f"{API}/query", body)
        qid = r.get("query_id") or r.get("id")
        if not qid:
            raise ApiError(f"建 Query 成功但没拿到 query_id：{r}")
        return int(qid)

    # ------------------------------------------------------------ 执行与轮询
    def execute(self, query_params: dict[str, Any] | None = None) -> str:
        assert self._client is not None
        if self.query_id is None:
            raise ApiError(
                "未提供 query_id。请把 dune.TRADES_SQL 存成一个 Dune Query 后把 id 传进来，"
                "或用 Plus 档的 Query API 动态创建（POST /api/v1/query）。"
            )
        body: dict[str, Any] = {"performance": self.performance}
        if query_params:
            body["query_parameters"] = query_params
        try:
            r = self._client.post_json(f"{API}/query/{self.query_id}/execute", body)
        except ApiError as e:
            # Query 没声明参数（例如手工存了内联时间戳的版本）时多传参会 400。
            # 去参重试能跑，但窗口以 Query 文本里写死的为准 —— 必须喊出来。
            if (query_params and e.status == 400 and "unknown parameters" in e.body):
                print(f"  [dune] ⚠️ Query {self.query_id} 未声明 start_ts/end_ts 参数，"
                      "已去参重试。实际窗口以 Query 文本内写死的时间戳为准，"
                      "与本次请求的窗口可能不一致！建议重存为参数化 Query。")
                r = self._client.post_json(f"{API}/query/{self.query_id}/execute",
                                           {"performance": self.performance})
            else:
                raise
        eid = r.get("execution_id")
        if not eid:
            raise ApiError(f"未拿到 execution_id: {r}")
        return str(eid)

    def wait(self, execution_id: str) -> dict[str, Any]:
        assert self._client is not None
        t0 = time.monotonic()
        while True:
            st = self._client.get_json(f"{API}/execution/{execution_id}/status")
            state = st.get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                return st
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                raise ApiError(f"查询失败 state={state}: {st.get('error')}")
            if time.monotonic() - t0 > self.poll_timeout:
                raise ApiError(f"查询超时（{self.poll_timeout}s），execution_id={execution_id}")
            if self.verbose:
                print(f"  [dune] {state} … {time.monotonic() - t0:.0f}s")
            time.sleep(self.poll_interval)

    def iter_rows(self, execution_id: str) -> Iterator[dict[str, Any]]:
        """按 offset 翻页。单次返回 >32GB 会被截断，必要时传
        allow_partial_results=true —— 但那意味着你的查询本来就该在 Dune 侧聚合。"""
        assert self._client is not None
        offset = 0
        while True:
            r = self._client.get_json(
                f"{API}/execution/{execution_id}/results",
                params={"limit": self.page_size, "offset": offset},
            )
            rows = ((r.get("result") or {}).get("rows")) or []
            if not rows:
                return
            yield from rows
            if len(rows) < self.page_size:
                return
            offset += len(rows)

    # ------------------------------------------------------------ 归一化
    def _map_row(self, row: dict[str, Any]) -> Trade | None:
        """★ 全部字段映射集中在这里。列名跟着你的 SQL 改。"""
        bought, sold = _addr(row.get("bought_addr")), _addr(row.get("sold_addr"))
        if not bought or not sold:
            return None
        q_b, q_s = bought in QUOTE_TOKENS, sold in QUOTE_TOKENS
        if q_b == q_s:
            return None   # 套利腿或币币直换，跳过（见 bitquery._map_trade 同处注释）

        ts = int(_f(row.get("ts")))
        amount_usd = _f(row.get("amount_usd"))

        if q_s:   # 付出报价资产 ⇒ 买入
            side, base, quote = Side.BUY, bought, sold
            base_amt, quote_amt = _f(row.get("bought_amt")), _f(row.get("sold_amt"))
        else:
            side, base, quote = Side.SELL, sold, bought
            base_amt, quote_amt = _f(row.get("sold_amt")), _f(row.get("bought_amt"))
        if base_amt <= 0 or quote_amt <= 0:
            return None

        # quote 的 USD 单价：稳定币恒 1；否则用 amount_usd 反推（与本笔成交同源，
        # 比外部 K 线准）；再不行退回 oracle。
        if quote in STABLE_QUOTES:
            quote_usd = 1.0
        elif amount_usd > 0:
            quote_usd = amount_usd / quote_amt
        else:
            quote_usd = self.oracle.price(quote, ts)
        if quote_usd <= 0:
            return None

        gas_usd = 0.0
        if row.get("gas_used") is not None:
            # bnb.transactions.gas_price 是 wei
            native = _f(row.get("gas_used")) * _f(row.get("gas_price")) / 1e18
            gas_usd = native * self.oracle.bnb(ts)

        return Trade(
            ts=ts, block=int(_f(row.get("block_number"))),
            tx_hash=str(row.get("tx_hash", "")),
            log_index=int(_f(row.get("evt_index"))),
            wallet=_addr(row.get("wallet")),   # tx_from，不是 taker/maker
            token=base, quote=quote, side=side,
            base_amount=base_amt, quote_amount=quote_amt, quote_usd=quote_usd,
            gas_usd=gas_usd,
            venue=f"{row.get('project', '')}{row.get('version', '') or ''}".strip() or "unknown",
            pool="",
            success=True,   # dex.trades 只含成功成交；失败交易的 gas 需另走 RPC 补
        )

    # ------------------------------------------------------------ 对外接口
    def fetch_trades(self, tokens: Iterable[str] | None = None,
                     start_ts: int = 0, end_ts: int = 0) -> list[Trade]:
        eid = self.execute({"start_ts": start_ts, "end_ts": end_ts})
        self.wait(eid)
        s = {t.lower() for t in tokens} if tokens else None
        out: list[Trade] = []
        for row in self.iter_rows(eid):
            t = self._map_row(row)
            if t is not None and (s is None or t.token in s):
                out.append(t)
        return out

    def _fmt(self, tpl: str, start_ts: int | None, end_ts: int | None) -> str:
        # start_ts/end_ts 传 None ⇒ 渲染成 Dune 参数 {{start_ts}}，Query 可以
        # 换窗口复用；传具体数字 ⇒ 内联（只适合一次性手工跑，换窗口必须重建，
        # 否则会静默用旧窗口）。
        return tpl.format(
            start_ts="{{start_ts}}" if start_ts is None else start_ts,
            end_ts="{{end_ts}}" if end_ts is None else end_ts,
            min_usd=self.min_usd, min_traders=self.min_traders,
            max_tokens=self.max_tokens, buffer_days=self.buffer_days,
            # dex.trades 的地址列是 varbinary —— 必须用 DuneSQL 裸 0x 字面量，
            # 加引号会变成 varchar，IN 比较直接类型错误（2026-08-15 实测）。
            quote_list=", ".join(sorted(QUOTE_TOKENS)),
            gas_select=GAS_SELECT if self.with_gas else "",
            gas_join=GAS_JOIN if self.with_gas else "",
        )

    def render_sql(self, start_ts: int, end_ts: int) -> str:
        """打印出来贴到 Dune 里存成 Query 用。

        scoped=True 时用窄范围版（只要窗口内新发、够开一场比赛、活跃度 Top-N
        的代币）——免费额度下唯一跑得动的写法。
        """
        return self._fmt(SCOPED_TRADES_SQL if self.scoped else TRADES_SQL,
                         start_ts, end_ts)

    def render_estimate_sql(self, start_ts: int, end_ts: int) -> str:
        """只返回几个聚合数，几乎不消耗导出额度 —— 先跑它再决定要不要跑正式查询。"""
        return self._fmt(ESTIMATE_SQL, start_ts, end_ts)

    def estimate(self, start_ts: int, end_ts: int, estimate_query_id: int) -> dict:
        """跑 ESTIMATE_SQL，返回行数口径 + 各档 credit/美元换算。

        它只返回 6 个聚合数，导出成本可以忽略 —— **正式查询之前一定先跑这个**。
        """
        saved, self.query_id = self.query_id, estimate_query_id
        try:
            eid = self.execute({"start_ts": start_ts, "end_ts": end_ts})
            self.wait(eid)
            rows = list(self.iter_rows(eid))
        finally:
            self.query_id = saved
        if not rows:
            raise ApiError("估算查询没有返回结果")
        r = rows[0]
        n_cols = 14 if self.with_gas else 12
        scoped_rows = int(_f(r.get("rows_scoped")))
        return {
            "rows_full": int(_f(r.get("rows_full"))),
            "rows_eligible": int(_f(r.get("rows_eligible"))),
            "rows_scoped": scoped_rows,
            "tokens_eligible": int(_f(r.get("tokens_eligible"))),
            "tokens_truncated": int(_f(r.get("tokens_truncated"))),
            "rows_truncated": int(_f(r.get("rows_truncated"))),
            "n_cols": n_cols,
            "cost_full": estimate_cost(int(_f(r.get("rows_full")))),
            "cost_scoped": estimate_cost(scoped_rows),
        }

    def fetch_flows(self, *_a, **_kw) -> list:
        raise NotImplementedError(
            "待实现：查 bep20_bnb.evt_Transfer 里非 DEX 的转账，映射到 models.Flow。"
            "不做这一步，cost_coverage 恒为 1.0，「收币→卖出」的老鼠仓会算成无限 ROI。"
        )
