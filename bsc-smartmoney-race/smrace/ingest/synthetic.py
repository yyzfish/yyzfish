"""合成数据源：带 ground truth 的模拟宇宙，用于端到端自测与闸门有效性验证。

这不是玩具。它的作用是回答一个必须先回答的问题：
**「我的管线能不能把 8 个真有技能的人，从 800 个零 alpha 赌徒里分出来？」**
如果在已知答案的合成数据上都分不出来，在链上更不可能。

内置的选手类型（每类都对应一种真实污染模式）：
    skilled  真有技能：选中优质代币的概率更高，且出场更靠近局部高点
    gambler  零 alpha：随机进出，其中运气最好的几个必然「看起来很神」
    sniper   狙击：建池后 1~3 块内进场，高胜率，但跟单不可复制
    bundler  同块建仓：与建池同块，绝对不可复制
    mev      三明治：同块反向 swap，持仓 < 1 块
    wash     刷量：与关联地址循环对倒
    sybil    女巫：一个操盘手 12 个分身，运气最好的那个会污染榜首
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..constants import BLOCK_TIME_SEC, WBNB
from ..models import Flow, FlowKind, Side, TokenMeta, Trade

TICK_BLOCKS = 400            # 一个价格 tick ≈ 400 块 ≈ 3 分钟
TICKS_PER_TOKEN = 240        # 代币观察生命周期 ≈ 12 小时
GENESIS_BLOCK = 60_000_000
GENESIS_TS = 1_760_000_000


def _addr(prefix: str, i: int) -> str:
    return f"0x{prefix}{i:034x}"


@dataclass
class Universe:
    trades: list[Trade] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    funding: list[Flow] = field(default_factory=list)
    tokens: dict[str, TokenMeta] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    prices: dict[str, np.ndarray] = field(default_factory=dict)
    truth: dict[str, str] = field(default_factory=dict)       # addr -> 真实类型
    sybil_truth: dict[str, str] = field(default_factory=dict) # addr -> 真实簇 id


def _price_path(rng: np.random.Generator) -> np.ndarray:
    """memecoin 价格路径：极端右偏。80% 归零，15% 温和上涨，5% 暴涨。"""
    roll = rng.random()
    if roll < 0.05:
        peak, drift_up = rng.uniform(20, 120), True
    elif roll < 0.20:
        peak, drift_up = rng.uniform(2, 6), True
    else:
        peak, drift_up = rng.uniform(1.05, 1.8), False

    peak_tick = int(rng.integers(20, TICKS_PER_TOKEN // 2))
    up = np.linspace(0.0, np.log(peak), peak_tick + 1)
    end = np.log(rng.uniform(0.01, 0.35) if not drift_up else rng.uniform(0.05, 0.6))
    down = np.linspace(np.log(peak), end, TICKS_PER_TOKEN - peak_tick)
    path = np.concatenate([up, down[1:]])
    noise = rng.normal(0, 0.08, size=len(path)).cumsum() * 0.35
    p0 = 10 ** rng.uniform(-8, -5)
    return p0 * np.exp(path + noise)


def generate(
    n_tokens: int = 300,
    n_skilled: int = 8,
    n_gambler: int = 800,
    n_sniper: int = 30,
    n_bundler: int = 10,
    n_mev: int = 10,
    n_wash: int = 15,
    n_sybil_clusters: int = 5,
    sybil_size: int = 12,
    seed: int = 42,
) -> Universe:
    rng = np.random.default_rng(seed)
    u = Universe()

    # ---------------- 代币
    for i in range(n_tokens):
        tok = _addr("7", i)
        launch = GENESIS_BLOCK + i * TICKS_PER_TOKEN * TICK_BLOCKS // 3
        u.tokens[tok] = TokenMeta(
            address=tok, symbol=f"MEME{i}", launch_block=launch,
            launch_ts=GENESIS_TS + int((launch - GENESIS_BLOCK) * BLOCK_TIME_SEC),
            venue="pancake_v2" if i % 3 else "fourmeme",
            peak_liquidity_usd=float(10 ** rng.uniform(3.5, 6.2)),
            is_memerush=bool(i % 7 == 0),
        )
        u.prices[tok] = _price_path(rng)
        u.marks[tok] = float(u.prices[tok][-1])

    toks = list(u.tokens)
    quality = {t: float(u.prices[t].max() / u.prices[t][0]) for t in toks}
    q_rank = {t: r for r, t in enumerate(sorted(toks, key=lambda x: -quality[x]))}

    # ---------------- 选手花名册
    roster: list[tuple[str, str]] = []
    for i in range(n_skilled):
        roster.append((_addr("a", i), "skilled"))
    for i in range(n_gambler):
        roster.append((_addr("b", i), "gambler"))
    for i in range(n_sniper):
        roster.append((_addr("c", i), "sniper"))
    for i in range(n_bundler):
        roster.append((_addr("d", i), "bundler"))
    for i in range(n_mev):
        roster.append((_addr("e", i), "mev"))
    for i in range(n_wash):
        roster.append((_addr("f", i), "wash"))
    for c in range(n_sybil_clusters):
        for k in range(sybil_size):
            a = _addr("9", c * 100 + k)
            roster.append((a, "sybil"))
            u.sybil_truth[a] = f"sybil_cluster_{c}"
    for a, kind in roster:
        u.truth[a] = kind

    log = u.trades.append
    li = [0]

    def emit(wallet, tok, tick, side, usd, *, gas=0.06, ok=True, block_off=0,
             jitter=None, venue="pancake_v2"):
        meta = u.tokens[tok]
        px = float(u.prices[tok][min(tick, TICKS_PER_TOKEN - 1)])
        # tick 内加抖动：否则同 tick 的所有人落在同一区块，会给「区块共现」聚类
        # 制造大量假阳性（真实链上不会这么整齐）。
        # 机器人类选手显式传 jitter=0，因为它们的关键特征就是区块级精确。
        j = int(rng.integers(0, TICK_BLOCKS)) if jitter is None else int(jitter)
        blk = meta.launch_block + tick * TICK_BLOCKS + block_off + j
        li[0] += 1
        log(Trade(
            ts=GENESIS_TS + int((blk - GENESIS_BLOCK) * BLOCK_TIME_SEC),
            block=blk, tx_hash=f"0x{li[0]:064x}", log_index=li[0] % 400,
            wallet=wallet, token=tok, quote=WBNB, side=side,
            base_amount=max(usd / px, 1e-12), quote_amount=usd / 600.0,
            quote_usd=600.0, gas_usd=gas, venue=venue, success=ok,
            pool=f"0xpool{tok[-8:]}",
        ))

    def pick_tokens(n: int, skill: float) -> list[str]:
        """skill ∈ [0,1]：越高越偏向真正走出来的代币（这就是「技能」的定义）。"""
        w = np.array([1.0 / (1.0 + q_rank[t]) ** (1.2 * skill) if skill > 0 else 1.0 for t in toks])
        w = w / w.sum()
        return list(rng.choice(toks, size=min(n, len(toks)), replace=False, p=w))

    def trade_arc(wallet, tok, *, skill: float, entry_lo=2, entry_hi=120,
                  usd=None, block_off=0, gas=0.06):
        """一次完整的建仓-离场。skill 影响出场是否靠近局部高点。"""
        path = u.prices[tok]
        entry = int(rng.integers(entry_lo, min(entry_hi, TICKS_PER_TOKEN - 5)))
        usd = usd if usd is not None else float(10 ** rng.uniform(2.0, 3.6))
        emit(wallet, tok, entry, Side.BUY, usd, block_off=block_off, gas=gas)
        seg = path[entry:]
        if len(seg) < 3:
            return
        if rng.random() < skill:
            peak_off = int(np.argmax(seg[: max(3, len(seg) // 2)]))
            exit_tick = entry + max(1, peak_off)
        else:
            exit_tick = entry + int(rng.integers(1, len(seg)))
        exit_tick = min(exit_tick, TICKS_PER_TOKEN - 1)
        ratio = float(path[exit_tick] / path[entry])
        # 分 1~2 笔卖出，卖出额 = 建仓额 × 涨跌幅（近似全部平仓）
        n_out = 1 if rng.random() < 0.65 else 2
        for k in range(n_out):
            emit(wallet, tok, min(exit_tick + k, TICKS_PER_TOKEN - 1), Side.SELL,
                 usd * ratio / n_out, block_off=block_off, gas=gas)

    # ---------------- 各类选手行为
    for a, kind in roster:
        if kind == "skilled":
            for t in pick_tokens(int(rng.integers(30, 55)), skill=0.85):
                trade_arc(a, t, skill=0.72, usd=float(10 ** rng.uniform(2.8, 4.0)))
        elif kind == "gambler":
            for t in pick_tokens(int(rng.integers(20, 45)), skill=0.0):
                trade_arc(a, t, skill=0.05)
        elif kind == "sybil":
            # 每个分身只碰少量代币 —— 单看运气好的那个像高手，合并后原形毕露
            for t in pick_tokens(int(rng.integers(18, 30)), skill=0.12):
                trade_arc(a, t, skill=0.12, usd=float(10 ** rng.uniform(2.0, 2.8)))
        elif kind == "sniper":
            for t in pick_tokens(int(rng.integers(40, 70)), skill=0.3):
                # block_delta ∈ [1,3]，且极短持仓（55% 在 1 分钟内退出）
                emit(a, t, 0, Side.BUY, float(10 ** rng.uniform(2.5, 3.5)),
                     block_off=int(rng.integers(1, 4)), jitter=0, gas=0.4)
                emit(a, t, int(rng.integers(1, 6)), Side.SELL,
                     float(10 ** rng.uniform(2.7, 3.9)), jitter=0, gas=0.4)
                if rng.random() < 0.35:   # 狙击失败率高，失败交易也烧 gas
                    emit(a, t, 0, Side.BUY, 0.0, block_off=2, jitter=0, gas=0.4, ok=False)
        elif kind == "bundler":
            for t in pick_tokens(int(rng.integers(25, 45)), skill=0.5):
                emit(a, t, 0, Side.BUY, float(10 ** rng.uniform(3.0, 4.0)),
                     block_off=0, jitter=0, gas=0.5)
                emit(a, t, int(rng.integers(3, 25)), Side.SELL,
                     float(10 ** rng.uniform(3.4, 4.6)), jitter=0, gas=0.5)
        elif kind == "mev":
            for t in pick_tokens(int(rng.integers(60, 110)), skill=0.0):
                tick = int(rng.integers(5, TICKS_PER_TOKEN - 5))
                v = float(10 ** rng.uniform(3.5, 4.5))
                emit(a, t, tick, Side.BUY, v, block_off=0, jitter=0, gas=0.3)   # 同块反向
                emit(a, t, tick, Side.SELL, v * 1.004, block_off=0, jitter=0, gas=0.3)
        elif kind == "wash":
            partner = _addr("f", (int(a[-2:], 16) + 1) % max(1, n_wash))
            for t in pick_tokens(int(rng.integers(30, 60)), skill=0.0):
                v = float(10 ** rng.uniform(3.0, 4.2))
                for cyc in range(6):   # A 卖 → B 买 → B 卖 → A 买，净头寸≈0
                    tk = int(rng.integers(5, TICKS_PER_TOKEN - 5))
                    emit(a, t, tk, Side.SELL, v, jitter=0, gas=0.05)
                    emit(partner, t, tk, Side.BUY, v, jitter=0, gas=0.05)
                    emit(partner, t, tk + 1, Side.SELL, v, jitter=0, gas=0.05)
                    emit(a, t, tk + 1, Side.BUY, v, jitter=0, gas=0.05)

    # ---------------- 女巫的共同资金源（供 entity 聚类识别）
    for c in range(n_sybil_clusters):
        funder = _addr("8", c)
        amt = float(rng.uniform(0.4, 0.6))
        for k in range(sybil_size):
            child = _addr("9", c * 100 + k)
            blk = GENESIS_BLOCK + c * 1000 + k
            ts = GENESIS_TS + int((blk - GENESIS_BLOCK) * BLOCK_TIME_SEC) + k * 30
            u.funding.append(Flow(ts=ts, block=blk, tx_hash=f"0xfund{c}{k}",
                                  wallet=funder, token=WBNB, amount=-amt,
                                  kind=FlowKind.UNKNOWN, mark_usd=600.0, counterparty=child))

    # ---------------- 少量空投（制造成本基准缺失，检验 coverage 闸门）
    for i in range(60):
        a = _addr("b", int(rng.integers(0, n_gambler)))
        t = toks[int(rng.integers(0, n_tokens))]
        meta = u.tokens[t]
        u.flows.append(Flow(
            ts=meta.launch_ts + 600, block=meta.launch_block + 100,
            tx_hash=f"0xair{i:04x}", wallet=a, token=t,
            amount=float(rng.uniform(1e5, 1e7)), kind=FlowKind.AIRDROP,
            mark_usd=float(u.prices[t][3]),
        ))

    u.trades.sort(key=lambda x: (x.block, x.log_index))
    return u


class SyntheticSource:
    name = "synthetic"

    def __init__(self, **kw) -> None:
        self.universe = generate(**kw)

    def fetch_tokens(self, start_block: int = 0, end_block: int = 10**12):
        return {k: v for k, v in self.universe.tokens.items()
                if start_block <= v.launch_block < end_block}

    def fetch_trades(self, tokens=None, start_block: int = 0, end_block: int = 10**12):
        s = set(tokens) if tokens else None
        return [t for t in self.universe.trades
                if start_block <= t.block < end_block and (s is None or t.token in s)]

    def fetch_flows(self, tokens=None, start_block: int = 0, end_block: int = 10**12):
        return list(self.universe.flows)
