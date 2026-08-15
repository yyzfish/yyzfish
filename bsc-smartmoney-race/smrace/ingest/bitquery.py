"""Bitquery 适配器：BSC DEXTrades → 归一化 Trade。

覆盖 PancakeSwap V2 / V3 / Infinity + Four.meme，并支持 mempool 流（Scale 档起）。

⚠️ **字段映射是本文件唯一需要跟着官方文档校对的地方**，全部集中在
`_map_trade()` 一个函数里。GraphQL schema 变了只改那里。
上线前请先跑 `python -m smrace.cli probe --source bitquery`，它会拉一小段真实数据
并打印归一化前后的对照，用来确认字段名和单位。

三个已知坑（见 docs/01）：
  1. 常规查询只覆盖**最近约 30 天**。回看更久必须带 `dataset: combined`（本文件默认带上），
     而 archive 数据集很可能对应额外的历史包加购 —— 请与销售确认后再上量。
  2. `Buy.Buyer` / `Sell.Seller` 在 V3 / Infinity 上**可能是 Router 而不是真人**。
     本适配器一律取 `Transaction.From` 做 wallet，并把 Buyer/Seller 存进 pool 字段备查。
  3. mempool 流要 Scale 档；Kafka 只有 Enterprise 有，自助档最快是 CoreCast gRPC。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Mapping

from ..constants import NFT_MARKETPLACE_PROTOCOLS, QUOTE_TOKENS, STABLE_QUOTES, WBNB
from ..models import Flow, Side, TokenMeta, Trade
from ..normalize.flows import classify_flow
from .http import ApiError, HttpClient
from .prices import PriceOracle, default_oracle

ENDPOINT = "https://streaming.bitquery.io/graphql"

# ---------------------------------------------------------------- GraphQL
DEX_TRADES_QUERY = """
query($since: DateTime, $till: DateTime, $limit: Int!, $afterBlock: String) {
  EVM(network: bsc, dataset: __DATASET__) {
    DEXTrades(
      limit: {count: $limit}
      orderBy: {ascending: Block_Number}
      where: {
        Block: {Time: {since: $since, till: $till}, Number: {gt: $afterBlock}}
      }
    ) {
      Block { Number Time }
      Transaction { Hash From Gas GasPrice }
      Log { Index }
      Trade {
        Dex { ProtocolName ProtocolVersion SmartContract }
        Buy  { Amount AmountInUSD Buyer  Currency { SmartContract Symbol Decimals } }
        Sell { Amount AmountInUSD Seller Currency { SmartContract Symbol Decimals } }
      }
    }
  }
}
"""

# 新建池 / Four.meme TokenCreate，用于拿 launch_block（block_delta 的 t=0）
POOL_CREATED_QUERY = """
query($since: DateTime, $till: DateTime, $limit: Int!) {
  EVM(network: bsc, dataset: __DATASET__) {
    Events(
      limit: {count: $limit}
      orderBy: {ascending: Block_Number}
      where: {
        Block: {Time: {since: $since, till: $till}}
        Log: {Signature: {Name: {in: ["PairCreated", "PoolCreated", "TokenCreate"]}}}
      }
    ) {
      Block { Number Time }
      Log { Signature { Name } SmartContract }
      Arguments { Name Value { ... on EVM_ABI_Address_Value_Arg { address } } }
    }
  }
}
"""


# 代币转账：补 PnL 的成本基准。没有它，「收币→卖出」的老鼠仓 = 无限 ROI。
TRANSFERS_QUERY = """
query($since: DateTime, $till: DateTime, $limit: Int!, $afterBlock: String) {
  EVM(network: bsc, dataset: __DATASET__) {
    Transfers(
      limit: {count: $limit}
      orderBy: {ascending: Block_Number}
      where: {
        Block: {Time: {since: $since, till: $till}, Number: {gt: $afterBlock}}
        Transfer: {Currency: {Fungible: true}}
      }
    ) {
      Block { Number Time }
      Transaction { Hash }
      Transfer {
        Amount
        Sender
        Receiver
        Currency { SmartContract Symbol Native }
      }
    }
  }
}
"""

# 原生币转账：反 Sybil 的规则 A/B/E（共同资金源、gas 加注方、利润归集）全靠它。
# 没有它，一个操盘手的 20 个分身很可能不会被合并 —— 榜单最隐蔽的污染源。
NATIVE_TRANSFERS_QUERY = """
query($since: DateTime, $till: DateTime, $limit: Int!, $afterBlock: String,
      $minAmount: String) {
  EVM(network: bsc, dataset: __DATASET__) {
    Transfers(
      limit: {count: $limit}
      orderBy: {ascending: Block_Number}
      where: {
        Block: {Time: {since: $since, till: $till}, Number: {gt: $afterBlock}}
        Transfer: {Currency: {Native: true}, Amount: {ge: $minAmount}}
      }
    ) {
      Block { Number Time }
      Transaction { Hash }
      Transfer { Amount Sender Receiver }
    }
  }
}
"""


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> int:
    s = (s or "").replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return 0


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _addr(x: Any) -> str:
    return str(x or "").lower()


@dataclass
class BitquerySource:
    name: str = "bitquery"
    endpoint: str = ENDPOINT
    token: str = ""
    rpm: int = 200                # Scale 档 240 req/min，留余量
    page_size: int = 5000         # 单页条数；太大容易超时
    oracle: PriceOracle = field(default_factory=default_oracle)
    # 2026-08-15 实测：EAP 的 GasPrice 以 BNB 计（例 0.000000000050000000 = 0.05 gwei）
    gas_price_unit: str = "bnb"   # ⚠️ 见 _gas_usd()：wei | gwei | bnb
    # combined = realtime + archive；archive 需要套餐额外开通。
    # 403 access restricted 时自动降级 realtime（约覆盖最近 8 小时~30 天，见告警）。
    dataset: str = ""
    verbose: bool = True
    _client: HttpClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.token = self.token or os.getenv("BITQUERY_TOKEN", "")
        self.dataset = self.dataset or os.getenv("BITQUERY_DATASET", "combined")
        # 低档套餐（Developer 档 ~10 req/min）跑不动默认的 Scale 档参数，
        # 用环境变量压低，避免整轮 429 打满重试后失败。
        self.rpm = int(os.getenv("BITQUERY_RPM", self.rpm))
        self.page_size = int(os.getenv("BITQUERY_PAGE_SIZE", self.page_size))
        if not self.token:
            raise ApiError("缺少 BITQUERY_TOKEN 环境变量")
        self._client = HttpClient(
            base_headers={"Authorization": f"Bearer {self.token}",
                          "Content-Type": "application/json"},
            rpm=self.rpm,
            on_retry=(lambda i, m: print(f"  [bitquery] 重试 {i}: {m}")) if self.verbose else None,
        )

    # ------------------------------------------------------------ 底层
    def query(self, gql: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        try:
            r = self._client.post_json(
                self.endpoint,
                {"query": gql.replace("__DATASET__", self.dataset), "variables": variables})
        except ApiError as e:
            # 套餐没开 archive 时 combined 会 403。降级 realtime 能继续跑，
            # 但历史深度受限 —— 必须喊出来，否则长回看窗口会静默缺数据。
            if (self.dataset != "realtime" and e.status == 403
                    and "access restricted" in e.body):
                print(f"  [bitquery] ⚠️ 套餐不含 archive，dataset {self.dataset}→realtime 降级。"
                      "历史覆盖受限，长回看窗口（>数天）可能静默缺数据！")
                self.dataset = "realtime"
                r = self._client.post_json(
                    self.endpoint,
                    {"query": gql.replace("__DATASET__", self.dataset), "variables": variables})
            else:
                raise
        if "errors" in r and r["errors"]:
            raise ApiError(f"GraphQL 错误: {r['errors']}")
        return r.get("data") or {}

    # ------------------------------------------------------------ 归一化
    def _gas_usd(self, tx: dict[str, Any], ts: int) -> float:
        """gasUsed × gasPrice × P_BNB(t)。

        ⚠️ **GasPrice 的单位是最容易出错的一处。** Bitquery 在不同端点上返回过
        wei / gwei / 原生币三种口径。本类用 gas_price_unit 显式声明，并在
        `probe` 里做合理性检查：BSC 上单笔 gas 典型 $0.05–0.3，
        算出来差一个数量级就是单位错了。
        """
        gas = _f(tx.get("Gas"))
        gp = _f(tx.get("GasPrice"))
        if gas <= 0 or gp <= 0:
            return 0.0
        if self.gas_price_unit == "wei":
            native = gas * gp / 1e18
        elif self.gas_price_unit == "gwei":
            native = gas * gp / 1e9
        else:  # 已经是 BNB
            native = gas * gp
        return native * self.oracle.bnb(ts)

    def _map_trade(self, row: dict[str, Any]) -> Trade | None:
        """★ 全部字段映射集中在这里。schema 变了只改这个函数。"""
        blk = row.get("Block") or {}
        tx = row.get("Transaction") or {}
        trade = row.get("Trade") or {}
        buy = trade.get("Buy") or {}
        sell = trade.get("Sell") or {}

        # DEXTrades 里混着 NFT 市场成交（实测有 seaport）。一侧是 WBNB 的 NFT
        # 购买会通过下面的 quote 判断被当成 memecoin 买入，必须先丢。
        proto = str((trade.get("Dex") or {}).get("ProtocolName") or "").lower()
        if proto.startswith(NFT_MARKETPLACE_PROTOCOLS):
            return None

        ts = _parse_ts(blk.get("Time", ""))
        block = int(_f(blk.get("Number")))
        buy_cur = _addr((buy.get("Currency") or {}).get("SmartContract"))
        sell_cur = _addr((sell.get("Currency") or {}).get("SmartContract"))
        if not buy_cur or not sell_cur:
            return None

        q_buy, q_sell = buy_cur in QUOTE_TOKENS, sell_cur in QUOTE_TOKENS
        if q_buy == q_sell:
            # 两边都是报价资产（套利腿）或两边都是 memecoin（币币直换）——
            # 前者不是选币行为，后者拿不到可靠的 USD 计价，一律跳过。
            return None

        if q_sell:   # 付出报价资产 ⇒ 买入 base
            side, base_cur = Side.BUY, buy_cur
            base_amt, quote_amt, quote_cur = _f(buy.get("Amount")), _f(sell.get("Amount")), sell_cur
            usd_hint = _f(sell.get("AmountInUSD"))
            counterparty = _addr(buy.get("Buyer"))
        else:        # 收到报价资产 ⇒ 卖出 base
            side, base_cur = Side.SELL, sell_cur
            base_amt, quote_amt, quote_cur = _f(sell.get("Amount")), _f(buy.get("Amount")), buy_cur
            usd_hint = _f(buy.get("AmountInUSD"))
            counterparty = _addr(sell.get("Seller"))

        if base_amt <= 0 or quote_amt <= 0:
            return None

        # quote 的 USD 单价：稳定币恒 1；否则优先用 Bitquery 给的 AmountInUSD 反推，
        # 拿不到再退回自己的 oracle。反推更准，因为它和这笔成交同源。
        if quote_cur in STABLE_QUOTES:
            quote_usd = 1.0
        elif usd_hint > 0:
            quote_usd = usd_hint / quote_amt
        else:
            quote_usd = self.oracle.price(quote_cur, ts)
        if quote_usd <= 0:
            return None

        dex = trade.get("Dex") or {}
        venue = f"{dex.get('ProtocolName', '')}{dex.get('ProtocolVersion', '')}".strip() or "unknown"

        return Trade(
            ts=ts, block=block,
            tx_hash=str(tx.get("Hash", "")),
            log_index=int(_f((row.get("Log") or {}).get("Index"))),
            # ★ 归因：一律用 tx.From，不用 Buyer/Seller（V3/Infinity 上那是 Router）
            wallet=_addr(tx.get("From")),
            token=base_cur, quote=quote_cur, side=side,
            base_amount=base_amt, quote_amount=quote_amt, quote_usd=quote_usd,
            gas_usd=self._gas_usd(tx, ts),
            venue=venue,
            pool=_addr(dex.get("SmartContract")) or counterparty,
            success=True,   # DEXTrades 只返回成功的成交；失败交易需另走 RPC 补
        )

    # ------------------------------------------------------------ 对外接口
    def iter_trades(self, start_ts: int, end_ts: int,
                    max_pages: int = 10_000) -> Iterator[Trade]:
        """按区块升序分页拉取。用 afterBlock 游标而不是 offset ——
        offset 分页在数据持续写入时会漏行/重行。"""
        after = "0"
        pages = 0
        seen_last = -1
        while pages < max_pages:
            data = self.query(DEX_TRADES_QUERY, {
                "since": _iso(start_ts), "till": _iso(end_ts),
                "limit": self.page_size, "afterBlock": after,
            })
            rows = (((data.get("EVM") or {}).get("DEXTrades")) or [])
            if not rows:
                return
            for row in rows:
                t = self._map_trade(row)
                if t is not None:
                    yield t
            last = int(_f(((rows[-1].get("Block")) or {}).get("Number")))
            if last <= seen_last:
                return   # 游标没前进，防死循环
            seen_last, after = last, str(last)
            pages += 1
            if len(rows) < self.page_size:
                return
        if self.verbose:
            print(f"  [bitquery] ⚠️ 达到 max_pages={max_pages} 上限，数据可能被截断")

    def fetch_trades(self, tokens: Iterable[str] | None = None,
                     start_ts: int = 0, end_ts: int = 0) -> list[Trade]:
        s = {t.lower() for t in tokens} if tokens else None
        return [t for t in self.iter_trades(start_ts, end_ts)
                if s is None or t.token in s]

    def fetch_tokens(self, start_ts: int, end_ts: int,
                     limit: int = 20_000) -> dict[str, TokenMeta]:
        """从 PairCreated / PoolCreated / TokenCreate 拿 launch_block。

        ⚠️ Four.meme 的毕业事件是从 **Four.meme 合约**发出的，不是从 Pancake
        Factory 发出的 —— 用 Factory 的 PoolCreated 归因会把 launch_block
        记成毕业时刻，导致所有 block_delta 失真。这里按事件的发出合约区分。
        """
        data = self.query(POOL_CREATED_QUERY, {
            "since": _iso(start_ts), "till": _iso(end_ts), "limit": limit})
        out: dict[str, TokenMeta] = {}
        for ev in ((data.get("EVM") or {}).get("Events") or []):
            blk = ev.get("Block") or {}
            args = {a.get("Name"): ((a.get("Value") or {}).get("address"))
                    for a in (ev.get("Arguments") or [])}
            emitter = _addr((ev.get("Log") or {}).get("SmartContract"))
            for key in ("token0", "token1", "currency0", "currency1", "token"):
                addr = _addr(args.get(key))
                if not addr or addr in QUOTE_TOKENS:
                    continue
                if addr in out:
                    continue
                out[addr] = TokenMeta(
                    address=addr,
                    launch_block=int(_f(blk.get("Number"))),
                    launch_ts=_parse_ts(blk.get("Time", "")),
                    venue="fourmeme" if emitter.startswith("0x5c95") else "pancake",
                    is_memerush=addr.startswith("0x4444"),
                )
        return out

    # ------------------------------------------------------------ 转账流
    def _map_flow(self, row: dict[str, Any], dex_pools: set[str],
                  marks: Mapping[str, float] | None = None) -> Flow | None:
        """★ 转账的字段映射。返回 None 表示这条该丢（swap 腿 / 销毁 / 路由）。"""
        blk = row.get("Block") or {}
        tr = row.get("Transfer") or {}
        cur = tr.get("Currency") or {}
        token = _addr(cur.get("SmartContract"))
        sender, receiver = _addr(tr.get("Sender")), _addr(tr.get("Receiver"))
        amount = _f(tr.get("Amount"))
        if not token or not receiver or amount <= 0:
            return None

        kind = classify_flow(sender, receiver, dex_pools=dex_pools, token=token)
        if kind is None:
            return None

        ts = _parse_ts(blk.get("Time", ""))
        # 成本基准 = **转入时市价**（对标 Nansen），不是零成本。
        # 拿不到市价时给 0 —— 它会拉低 cost_coverage，从而把该实体挡在榜外，
        # 这比凭空给一个价更安全。
        mark = float((marks or {}).get(token, 0.0))
        return Flow(
            ts=ts, block=int(_f(blk.get("Number"))),
            tx_hash=str((row.get("Transaction") or {}).get("Hash", "")),
            wallet=receiver, token=token, amount=amount,
            kind=kind, mark_usd=mark, counterparty=sender,
        )

    def fetch_flows(self, tokens: Iterable[str] | None = None,
                    start_ts: int = 0, end_ts: int = 0,
                    dex_pools: set[str] | None = None,
                    marks: Mapping[str, float] | None = None,
                    max_pages: int = 10_000) -> list[Flow]:
        """代币转账流。**调用方必须再跑一遍 flows.drop_swap_legs()** ——
        这里只能按地址判断，判不出「这条 transfer 属于某笔 swap」。"""
        s = {t.lower() for t in tokens} if tokens else None
        pools = dex_pools or set()
        out: list[Flow] = []
        after, pages, seen = "0", 0, -1
        while pages < max_pages:
            data = self.query(TRANSFERS_QUERY, {
                "since": _iso(start_ts), "till": _iso(end_ts),
                "limit": self.page_size, "afterBlock": after,
            })
            rows = ((data.get("EVM") or {}).get("Transfers")) or []
            if not rows:
                break
            for row in rows:
                f = self._map_flow(row, pools, marks)
                if f is not None and (s is None or f.token in s):
                    out.append(f)
            last = int(_f(((rows[-1].get("Block")) or {}).get("Number")))
            if last <= seen:
                break
            seen, after, pages = last, str(last), pages + 1
            if len(rows) < self.page_size:
                break
        return out

    def fetch_funding(self, start_ts: int, end_ts: int,
                      min_bnb: float = 0.01, max_pages: int = 10_000) -> list[Flow]:
        """原生币（BNB）转账，供反 Sybil 用。

        输出成对的两条：转出方记负、转入方记正 —— entity.cluster_entities()
        的规则 A（共同资金源播撒）和 E（利润归集）分别用这两个方向。

        min_bnb 用来滤掉粉尘：BSC 上有大量 0.0001 BNB 的骚扰转账，
        全收进来会让「共同资金源」规则被噪音淹没。
        """
        out: list[Flow] = []
        after, pages, seen = "0", 0, -1
        while pages < max_pages:
            data = self.query(NATIVE_TRANSFERS_QUERY, {
                "since": _iso(start_ts), "till": _iso(end_ts),
                "limit": self.page_size, "afterBlock": after,
                "minAmount": str(min_bnb),
            })
            rows = ((data.get("EVM") or {}).get("Transfers")) or []
            if not rows:
                break
            for row in rows:
                blk, tr = row.get("Block") or {}, row.get("Transfer") or {}
                s_, r_ = _addr(tr.get("Sender")), _addr(tr.get("Receiver"))
                amt = _f(tr.get("Amount"))
                if not s_ or not r_ or amt <= 0:
                    continue
                ts = _parse_ts(blk.get("Time", ""))
                blkno = int(_f(blk.get("Number")))
                txh = str((row.get("Transaction") or {}).get("Hash", ""))
                px = self.oracle.bnb(ts)
                out.append(Flow(ts=ts, block=blkno, tx_hash=txh, wallet=s_,
                                token=WBNB, amount=-amt, mark_usd=px, counterparty=r_))
                out.append(Flow(ts=ts, block=blkno, tx_hash=txh, wallet=r_,
                                token=WBNB, amount=amt, mark_usd=px, counterparty=s_))
            last = int(_f(((rows[-1].get("Block")) or {}).get("Number")))
            if last <= seen:
                break
            seen, after, pages = last, str(last), pages + 1
            if len(rows) < self.page_size:
                break
        return out

    # ------------------------------------------------------------ 实时
    def stream(self, on_trade: Callable[[Trade], None], mempool: bool = False) -> None:
        """实时订阅。BSC 出块 0.45s ≈ 192,000 块/天 —— **不要轮询**。

        自助档最快通道是 CoreCast gRPC（~300ms），其次 WSS（~1s）；
        Kafka（~400ms）只有 Enterprise 有。mempool 需要 Scale 档起。
        """
        raise NotImplementedError(
            "待实现：pip install websockets，连 wss://streaming.bitquery.io/graphql "
            f"（subscription 版 DEXTrades{'，加 mempool: true' if mempool else ''}），"
            "复用本类的 _map_trade() 做归一化后回调 on_trade。"
            "更低延迟走 CoreCast gRPC（pip install grpcio + 官方 protobuf）。"
        )
