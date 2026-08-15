"""统一数据模型。所有数据源（Bitquery / Dune / RPC / 合成）都归一化到这里。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class FlowKind(str, Enum):
    """非 swap 的代币流入/流出。成本基准归因的关键分类。"""
    AIRDROP = "airdrop"        # 无对价领取 → 成本 = 领取时市价，且从「交易能力」中剔除
    BRIDGE_IN = "bridge_in"    # 跨链转入 → 成本 = 转入时市价
    CEX_IN = "cex_in"          # CEX 提币 → 成本 = 转入时市价，实体打 has_offchain_leg
    INTERNAL = "internal"      # 同实体内部转账 → PnL 中性，净额抵消
    LP_NEUTRAL = "lp_neutral"  # add/removeLiquidity、stake/unstake → PnL 中性
    UNKNOWN = "unknown"        # 无法归因 → 计入 coverage 分母，拉低置信度


@dataclass(slots=True)
class Trade:
    """一笔 DEX swap，已归一化为 base/quote 视角。

    base_amount 必须是 **ERC20 Transfer 事件里的实际到账量**，不是 router 报价 ——
    BSC 上大量 memecoin 有 3~10% 的 fee-on-transfer 税。
    """
    ts: int
    block: int
    tx_hash: str
    log_index: int
    wallet: str            # 真实交易者（tx.from），不是 router
    token: str             # base（被交易的 memecoin）
    quote: str             # quote（WBNB / USDT / ...）
    side: Side
    base_amount: float     # 正数，实际收到/付出的 base 数量
    quote_amount: float    # 正数，付出/收到的 quote 数量
    quote_usd: float       # 该 quote 资产在 ts 时刻的 USD 单价
    gas_usd: float = 0.0
    venue: str = "pancake_v2"
    pool: str = ""
    success: bool = True   # 失败交易的 gas 也要计入 —— 狙击机器人失败率极高

    @property
    def usd(self) -> float:
        """成交额（USD）。由 quote 侧推导 ⇒ 天然内含滑点，不要再单独扣滑点。"""
        return self.quote_amount * self.quote_usd

    @property
    def exec_price(self) -> float:
        """执行价（USD / base）。base_amount 为 0 时返回 0。"""
        return self.usd / self.base_amount if self.base_amount else 0.0


@dataclass(slots=True)
class Flow:
    """非 swap 的代币流入/流出。"""
    ts: int
    block: int
    tx_hash: str
    wallet: str
    token: str
    amount: float          # 正 = 流入，负 = 流出
    kind: FlowKind = FlowKind.UNKNOWN
    mark_usd: float = 0.0  # 该时刻 token 的市价（USD），用于记成本基准
    counterparty: str = ""


@dataclass(slots=True)
class TokenMeta:
    """代币元数据。用于构造「比赛」和可跟单性过滤。"""
    address: str
    symbol: str = ""
    launch_block: int = 0      # 建池 / TokenCreate 的区块，block_delta 的 t=0
    launch_ts: int = 0
    graduated_block: Optional[int] = None  # Four.meme 毕业到 Pancake 的区块
    venue: str = "pancake_v2"
    peak_liquidity_usd: float = 0.0
    is_memerush: bool = False


@dataclass(slots=True)
class PositionResult:
    """(实体, 代币) 维度的 PnL 结算结果。"""
    entity: str
    token: str
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    invested_usd: float = 0.0      # 累计买入成本（含费），收益率的分母
    gas_usd: float = 0.0
    qty_open: float = 0.0
    cost_open: float = 0.0
    n_trades: int = 0
    n_buys: int = 0
    n_sells: int = 0
    first_block: int = 0
    last_block: int = 0
    first_ts: int = 0
    last_ts: int = 0
    block_delta: int = 10**9       # 距 launch_block 的区块数 → bundler/sniper 判据
    hold_seconds: float = 0.0
    inflow_usd_total: float = 0.0  # 所有流入（买入 + transfer_in）的 USD
    inflow_usd_priced: float = 0.0 # 其中有明确链上买入对价的部分
    has_offchain_leg: bool = False
    airdrop_pnl: float = 0.0       # 空投带来的盈亏，单独隔离，不算交易能力
    flips: int = 0                 # 连续等量买卖对的次数 → flip_ratio

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl - self.gas_usd

    @property
    def ret(self) -> float:
        """净收益率。分母用累计投入成本。"""
        return self.net_pnl / self.invested_usd if self.invested_usd > 1e-9 else 0.0

    @property
    def cost_coverage(self) -> float:
        """成本基准覆盖率。< 0.8 → 该仓位 PnL 标 low_confidence。"""
        if self.inflow_usd_total <= 1e-9:
            return 1.0
        return self.inflow_usd_priced / self.inflow_usd_total


@dataclass(slots=True)
class Entity:
    """一个「选手」。可能是单地址，也可能是 sybil 聚类后的地址簇。"""
    entity_id: str
    addresses: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)  # bundler / sniper / mev / wash / bot ...
    cluster_reason: str = ""


@dataclass(slots=True)
class RaceEntry:
    """一场比赛（代币 × 时间窗）中的一个参赛记录。"""
    entity: str
    ret: float          # 名次依据：窗口内净收益率
    net_pnl: float
    invested_usd: float


@dataclass(slots=True)
class Race:
    race_id: str        # f"{token}@{window_idx}"
    token: str
    start_block: int
    end_block: int
    entries: list[RaceEntry] = field(default_factory=list)


@dataclass(slots=True)
class Rating:
    """Elo-MMR 评分状态。"""
    mu: float = 1500.0
    sig: float = 350.0
    # 历史表现（p_k, beta_k），用于 Elo-MMR(ρ) 的鲁棒 logistic 更新
    history: list[tuple[float, float]] = field(default_factory=list)
    n_races: int = 0

    @property
    def display(self) -> float:
        """榜单展示值：TrueSkill 式保守分位 μ−3σ。
        样本少 → σ 大 → 展示值低，天然压制「4 笔交易 100% 胜率」。"""
        return self.mu - 3.0 * self.sig
