"""可跟单性回测 —— 整个系统最有商业价值的部分。

不要展示领头者的 PnL。展示的应该是：
「如果你在他之后 N 个区块以市价跟进、在他卖出后 N 个区块卖出、按 $X 仓位、
  扣除 BSC gas、交易费和代币税，你会赚多少。」

这个数字与领头者自己 PnL 的比值 = **跟单可行性系数 (copy factor)**。
参照量级：已发表研究在 6,000 个 meme 项目上观测到 smart money 平均收益 14%，
跟单者在考虑现实摩擦后约 3%（≈79% 的 alpha 被摩擦吃掉）。该数字是特定平台、
特定样本下的观测，不要当常数外推，但它给出了正确的心理预期。

四层衰减，按影响从大到小：
  1. 进场延迟：memecoin 早期价格是超线性的，差 6 个区块可能就是 2~5 倍价差。
     这不是「滑点」，这是根本不同的成交价。
  2. 出场滞后：只能在看到领头者卖出上链后才卖，此时他的抛压已经打进价格。
     **结构性的：出场时你永远是领头者的对手方。**
  3. 自身冲击：跟单资金越大滑点越大；BSC 有公开 mempool，跟单流本身可被前跑。
  4. 选择偏差反噬：榜单是回看选出的，上榜那一刻往往正是其运气峰值。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from ..config import CopyConfig
from ..constants import BLOCK_TIME_SEC
from ..models import Side, TokenMeta, Trade

PriceFn = Callable[[str, int], float]  # (token, block) -> USD 价


@dataclass
class CopyResult:
    entity: str
    leader_pnl: float = 0.0
    leader_ret: float = 0.0
    copy_pnl: float = 0.0
    copy_ret: float = 0.0
    n_signals: int = 0
    n_copied: int = 0
    n_skipped_shallow: int = 0   # 池子太浅
    n_skipped_fast: int = 0      # 领头者持仓太短，跟不上
    gas_paid: float = 0.0
    fees_paid: float = 0.0

    @property
    def copy_factor(self) -> float:
        """跟单可行性系数 = 跟单收益率 / 领头者收益率。<0.3 基本不值得跟。"""
        if abs(self.leader_ret) < 1e-9:
            return 0.0
        return self.copy_ret / self.leader_ret


def simulate_copy(
    entity: str,
    trades: Sequence[Trade],
    price_fn: PriceFn,
    cfg: CopyConfig,
    tokens: Mapping[str, TokenMeta] | None = None,
    token_tax_bps: Mapping[str, float] | None = None,
) -> CopyResult:
    """对单个实体做「延迟跟单」重放。trades 应只含该实体的成交。"""
    tokens = tokens or {}
    taxes = token_tax_bps or {}
    r = CopyResult(entity=entity)

    by_tok: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.success:
            by_tok[t.token].append(t)

    for tok, ts in by_tok.items():
        ts.sort(key=lambda x: (x.block, x.log_index))
        meta = tokens.get(tok)
        tax = taxes.get(tok, cfg.token_tax_bps) / 10_000.0
        fee = cfg.taker_fee_bps / 10_000.0

        # ---- 领头者自身（作为对照）
        l_qty = l_cost = l_real = 0.0
        for t in ts:
            if t.side is Side.BUY:
                l_qty += t.base_amount
                l_cost += t.usd
            elif l_qty > 1e-18:
                q = min(t.base_amount, l_qty)
                avg = l_cost / l_qty
                l_real += t.usd * (q / t.base_amount if t.base_amount else 1.0) - avg * q
                l_cost -= avg * q
                l_qty -= q
        r.leader_pnl += l_real

        # ---- 跟单者
        buys = [t for t in ts if t.side is Side.BUY]
        sells = [t for t in ts if t.side is Side.SELL]
        if not buys:
            continue
        r.n_signals += 1

        first_buy, last_sell = buys[0], (sells[-1] if sells else None)
        hold_sec = ((last_sell.block - first_buy.block) * BLOCK_TIME_SEC) if last_sell else 1e9

        if meta and meta.peak_liquidity_usd < cfg.min_pool_liquidity_usd:
            r.n_skipped_shallow += 1
            continue
        if hold_sec < cfg.max_hold_filter_sec:
            r.n_skipped_fast += 1
            continue

        entry_block = first_buy.block + cfg.entry_lag_blocks
        entry_px = price_fn(tok, entry_block)
        if entry_px <= 0:
            continue

        notional = cfg.position_usd
        # 进场：付手续费，收到的代币还要被 FoT 税吃掉一部分
        qty = notional * (1 - fee) * (1 - tax) / entry_px
        r.fees_paid += notional * fee
        r.gas_paid += cfg.gas_usd_per_tx

        if last_sell is None:
            exit_px = price_fn(tok, ts[-1].block + cfg.exit_lag_blocks)
        else:
            exit_px = price_fn(tok, last_sell.block + cfg.exit_lag_blocks)
        exit_px = max(exit_px, 0.0)

        proceeds = qty * exit_px * (1 - fee) * (1 - tax)
        r.fees_paid += qty * exit_px * fee
        r.gas_paid += cfg.gas_usd_per_tx
        r.copy_pnl += proceeds - notional - 2 * cfg.gas_usd_per_tx
        r.n_copied += 1

    if leader_invested_total := sum(t.usd for t in trades if t.success and t.side is Side.BUY):
        r.leader_ret = r.leader_pnl / leader_invested_total
    if r.n_copied:
        r.copy_ret = r.copy_pnl / (r.n_copied * cfg.position_usd)
    return r


def batch_copy_backtest(
    entities: Iterable[str],
    trades_by_entity: Mapping[str, Sequence[Trade]],
    price_fn: PriceFn,
    cfg: CopyConfig,
    tokens: Mapping[str, TokenMeta] | None = None,
) -> list[CopyResult]:
    out = [
        simulate_copy(e, trades_by_entity.get(e, []), price_fn, cfg, tokens)
        for e in entities
    ]
    out.sort(key=lambda x: -x.copy_pnl)
    return out


def make_price_fn(prices: Mapping[str, Sequence[float]], tokens: Mapping[str, TokenMeta],
                  tick_blocks: int) -> PriceFn:
    """从 tick 价格序列构造 price_fn。生产环境应换成对池子 reserves/sqrtPriceX96
    的历史查询，或对 swap 明细做 last-trade 插值。"""
    def fn(token: str, block: int) -> float:
        path = prices.get(token)
        meta = tokens.get(token)
        if path is None or len(path) == 0 or meta is None:
            return 0.0
        idx = max(0, min(len(path) - 1, (block - meta.launch_block) // tick_blocks))
        return float(path[int(idx)])
    return fn
