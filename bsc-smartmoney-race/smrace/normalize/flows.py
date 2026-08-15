"""转账流归类 —— 补上 PnL 引擎最后一块拼图。

不接转账流的后果（docs/03 §5.1 反复警告过）：
  · cost_coverage 恒为 1.0 → 「收币→卖出」的老鼠仓算成**无限 ROI**，直接占榜首
  · 空投盈亏无法从「交易能力」里剥离
  · 同实体内部转账被当成真实买卖，**同一笔盈利重复计两次**

两条最容易搞错的规则，都在这里：

1. **swap 的转账腿必须丢掉，不是标成中性。** 一笔 swap 在链上同时产生
   Swap 事件和两条 ERC20 Transfer。Transfer 那两条如果也进 PnL 引擎，
   同一笔买入会被记两次。判据：该 transfer 的 tx_hash 已经出现在 trades 里。

2. **内部转账只能在实体聚类之后才认得出来。** 摄入时无法知道两个地址
   是不是同一个人，必须等 a2e 出来再回标 —— 所以拆成两步。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from ..constants import (BRIDGE_ADDRESSES, CEX_HOT_WALLETS, ROUTER_ADDRESSES,
                         FOURMEME_EXCHANGE)
from ..models import Flow, FlowKind, Trade

ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"


def classify_flow(
    sender: str,
    receiver: str,
    *,
    dex_pools: set[str] | None = None,
    lp_tokens: set[str] | None = None,
    token: str = "",
) -> FlowKind | None:
    """返回 None 表示**丢弃**这条流（不是中性，是根本不该进 PnL 引擎）。

    顺序有讲究：先判「该丢的」，再判「该记成本的」。
    """
    s, r = sender.lower(), receiver.lower()
    pools = dex_pools or set()

    # ---- 该丢的：swap / 路由 / 池子腿。它们已经在 Trade 里被算过一次了。
    if s in pools or r in pools:
        return None
    if s in ROUTER_ADDRESSES or r in ROUTER_ADDRESSES:
        return None
    if s == FOURMEME_EXCHANGE or r == FOURMEME_EXCHANGE:
        return None   # Four.meme 曲线买卖，应该走 TokenPurchase/TokenSale 而不是这里

    # ---- LP / 质押：PnL 中性（addLiquidity 被误读成卖出会产生巨额虚假 realized）
    if lp_tokens and (token.lower() in lp_tokens or s in lp_tokens or r in lp_tokens):
        return FlowKind.LP_NEUTRAL

    # ---- 该按转入时市价记成本的
    if s in (ZERO,):
        return FlowKind.AIRDROP        # mint / 领取，无对价
    if s in BRIDGE_ADDRESSES:
        return FlowKind.BRIDGE_IN
    if s in CEX_HOT_WALLETS:
        return FlowKind.CEX_IN
    if r in (ZERO, DEAD):
        return None                    # 销毁，不产生 PnL

    return FlowKind.UNKNOWN            # 计入 coverage 分母，拉低置信度


def drop_swap_legs(flows: Iterable[Flow], trades: Iterable[Trade]) -> list[Flow]:
    """丢掉与 swap 同一笔交易的转账 —— 否则同一笔买入被记两次。

    这是接入转账流时**最容易漏、后果最严重**的一步：不做的话，
    每个活跃地址的成本基准都会翻倍，PnL 全线失真。
    """
    tx = {t.tx_hash for t in trades if t.tx_hash}
    return [f for f in flows if f.tx_hash not in tx]


def label_internal(flows: Sequence[Flow], addr_to_entity: Mapping[str, str]) -> int:
    """实体聚类之后回标内部转账。返回改标的条数。

    簇内转账必须净额抵消 —— 不做的话，一个操盘手在自己 20 个地址之间倒仓，
    每倒一次就凭空产生一次「盈利」。
    """
    n = 0
    for f in flows:
        if not f.counterparty:
            continue
        a = addr_to_entity.get(f.wallet.lower(), f.wallet.lower())
        b = addr_to_entity.get(f.counterparty.lower(), f.counterparty.lower())
        if a == b and f.kind is not FlowKind.INTERNAL:
            f.kind = FlowKind.INTERNAL
            n += 1
    return n


def pools_from_trades(trades: Iterable[Trade]) -> set[str]:
    """从成交里反推池子地址集合，供 classify_flow 丢弃 swap 腿。"""
    return {t.pool.lower() for t in trades if t.pool}


def flow_summary(flows: Sequence[Flow]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for f in flows:
        c[f.kind.value] += 1
    c["total"] = len(flows)
    return dict(c)
