"""PnL 引擎：加权平均成本法 + 执行价定价 + gas/FoT + 转入归因。

口径决策（见 docs/02 第 2 节）：
  1. 成本法 = 移动加权平均（对标 Nansen / Dune），路径无关、O(1) 状态。
  2. 定价 = swap 的**实际执行价**（由 quote 侧推导），天然内含滑点。
     用外部 K 线给 swap 定价会系统性低估低流动性 memecoin 的滑点。
  3. 转入 **不按零成本**，按转入时市价记成本 —— 否则「收币→卖出」的老鼠仓
     会显示为无限 ROI，这是各类榜单最常见的污染源。
  4. 排行榜一律用 Net PnL（扣 gas、扣失败交易 gas、扣 FoT 税）。
     Gross PnL 榜单会把高频机器人排到最前面。
  5. LP / 质押事件标记为 PnL 中性 —— 朴素引擎会把 addLiquidity 误读成
     「卖出 token A+B、买入 LP token」，产生巨额虚假 realized PnL。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from ..models import Flow, FlowKind, PositionResult, Side, TokenMeta, Trade


class _Book:
    """单个 (实体, 代币) 的移动加权平均成本账本。"""

    __slots__ = ("qty", "cost")

    def __init__(self) -> None:
        self.qty = 0.0   # 持仓数量
        self.cost = 0.0  # 成本池（USD）

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > 1e-18 else 0.0

    def buy(self, qty: float, usd: float) -> None:
        self.qty += qty
        self.cost += usd

    def sell(self, qty: float, usd: float) -> float:
        """返回本次卖出的已实现盈亏。卖超（成本基准缺失）时按 0 成本处理并由
        调用方通过 coverage 指标标记低置信度。"""
        if self.qty <= 1e-18:
            return usd  # 无成本记录 —— coverage 会反映这一点
        q = min(qty, self.qty)
        avg = self.avg
        realized = usd * (q / qty if qty > 1e-18 else 1.0) - avg * q
        self.cost -= avg * q
        self.qty -= q
        if self.qty < 1e-18:
            self.qty, self.cost = 0.0, 0.0
        return realized


def _is_flip(prev: Trade | None, cur: Trade, tol: float = 0.02) -> bool:
    """连续的等量反向买卖 → 一次 flip。flip_ratio 高 = bump bot（刷量）。"""
    if prev is None or prev.side == cur.side:
        return False
    a, b = prev.base_amount, cur.base_amount
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tol


def compute_positions(
    trades: Iterable[Trade],
    flows: Iterable[Flow] = (),
    tokens: Mapping[str, TokenMeta] | None = None,
    marks: Mapping[str, float] | None = None,
    addr_to_entity: Mapping[str, str] | None = None,
) -> dict[tuple[str, str], PositionResult]:
    """结算所有 (实体, 代币) 仓位。

    trades / flows 无需预先排序，内部按 (block, log_index) 排序。
    marks: token -> 期末市价（USD），用于未实现盈亏。缺失则未实现按 0 处理
           （保守：套牢仓位不计浮盈，避免高估）。
    addr_to_entity: sybil 聚类结果。缺失则地址即实体。
    """
    tokens = tokens or {}
    marks = marks or {}
    a2e = addr_to_entity or {}

    def ent(addr: str) -> str:
        return a2e.get(addr, addr)

    books: dict[tuple[str, str], _Book] = defaultdict(_Book)
    res: dict[tuple[str, str], PositionResult] = {}
    last_trade: dict[tuple[str, str], Trade] = {}

    def get(key: tuple[str, str]) -> PositionResult:
        if key not in res:
            res[key] = PositionResult(entity=key[0], token=key[1])
        return res[key]

    events: list[tuple[int, int, int, object]] = []
    for t in trades:
        events.append((t.block, t.log_index, 0, t))
    for f in flows:
        events.append((f.block, 0, 1, f))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    for _blk, _li, _kind, ev in events:
        if isinstance(ev, Trade):
            key = (ent(ev.wallet), ev.token)
            r, bk = get(key), books[(ent(ev.wallet), ev.token)]

            # 失败交易：只烧 gas，不动账本。狙击机器人失败率极高，
            # 只统计成功交易会严重高估其净收益。
            r.gas_usd += ev.gas_usd
            if not ev.success:
                continue

            usd = ev.usd
            if ev.side is Side.BUY:
                bk.buy(ev.base_amount, usd)
                r.invested_usd += usd
                r.inflow_usd_total += usd
                r.inflow_usd_priced += usd
                r.n_buys += 1
            else:
                r.realized_pnl += bk.sell(ev.base_amount, usd)
                r.n_sells += 1

            r.n_trades += 1
            if _is_flip(last_trade.get(key), ev):
                r.flips += 1
            last_trade[key] = ev

            if r.first_block == 0 or ev.block < r.first_block:
                r.first_block, r.first_ts = ev.block, ev.ts
            if ev.block > r.last_block:
                r.last_block, r.last_ts = ev.block, ev.ts

        else:  # Flow
            f: Flow = ev  # type: ignore[assignment]
            key = (ent(f.wallet), f.token)
            r, bk = get(key), books[(ent(f.wallet), f.token)]

            # 内部转账 / LP 与质押 → PnL 中性，直接跳过
            if f.kind in (FlowKind.INTERNAL, FlowKind.LP_NEUTRAL):
                continue

            usd = abs(f.amount) * f.mark_usd
            if f.amount > 0:
                # 转入按转入时市价记成本（对标 Nansen），不是零成本
                bk.buy(f.amount, usd)
                r.inflow_usd_total += usd
                # 关键：transfer_in 不计入 inflow_usd_priced —— 它拉低 coverage
                if f.kind is FlowKind.AIRDROP:
                    r.airdrop_pnl += 0.0  # 成本已入账；卖出时的盈亏在下方隔离
                if f.kind is FlowKind.CEX_IN:
                    r.has_offchain_leg = True
            else:
                # 转出视同卖出，PnL 归属转出方
                r.realized_pnl += bk.sell(-f.amount, usd)

    # 收尾：未实现盈亏、持仓时长、block_delta
    for key, r in res.items():
        bk = books[key]
        r.qty_open, r.cost_open = bk.qty, bk.cost
        mark = marks.get(r.token, 0.0)
        r.unrealized_pnl = (bk.qty * mark - bk.cost) if mark > 0 else 0.0
        r.hold_seconds = max(0.0, float(r.last_ts - r.first_ts))
        meta = tokens.get(r.token)
        if meta and meta.launch_block and r.first_block:
            r.block_delta = max(0, r.first_block - meta.launch_block)
    return res


def entity_rollup(positions: Mapping[tuple[str, str], PositionResult]) -> dict[str, dict]:
    """把仓位汇总到实体维度，产出评分层需要的原始字段。"""
    agg: dict[str, dict] = {}
    for (e, _tok), p in positions.items():
        a = agg.setdefault(e, {
            "entity": e, "net_pnl": 0.0, "invested_usd": 0.0, "gas_usd": 0.0,
            "n_trades": 0, "n_tokens": 0, "wins": 0, "returns": [], "tokens": [],
            "inflow_usd_total": 0.0, "inflow_usd_priced": 0.0,
            "flips": 0, "min_block_delta": 10**9, "hold_seconds": [],
            "has_offchain_leg": False, "airdrop_pnl": 0.0,
        })
        a["net_pnl"] += p.net_pnl
        a["invested_usd"] += p.invested_usd
        a["gas_usd"] += p.gas_usd
        a["n_trades"] += p.n_trades
        a["n_tokens"] += 1
        a["wins"] += 1 if p.net_pnl > 0 else 0
        a["returns"].append(p.ret)
        a["tokens"].append(p.token)
        a["inflow_usd_total"] += p.inflow_usd_total
        a["inflow_usd_priced"] += p.inflow_usd_priced
        a["flips"] += p.flips
        a["min_block_delta"] = min(a["min_block_delta"], p.block_delta)
        a["hold_seconds"].append(p.hold_seconds)
        a["has_offchain_leg"] |= p.has_offchain_leg
        a["airdrop_pnl"] += p.airdrop_pnl
    for a in agg.values():
        n = max(1, a["n_tokens"])
        a["hit_rate"] = a["wins"] / n
        a["transfer_in_ratio"] = (
            1.0 - a["inflow_usd_priced"] / a["inflow_usd_total"]
            if a["inflow_usd_total"] > 1e-9 else 0.0
        )
    return agg
