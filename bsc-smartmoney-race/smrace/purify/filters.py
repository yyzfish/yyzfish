"""净化层：先剔除污染，再评分。顺序不可颠倒。

核心洞察：任何按胜率排序的榜单，榜首必然是狙击/三明治机器人（87%+ 胜率），
而它们的 alpha 来自发射前信息优势和同块执行特权，跟单者 100% 无法复制。

设计取舍：sniper **不一刀切删除，而是单独分赛道**。它们是真实的赚钱者，
应该作为「信号源」（它们买了什么）而非「跟单对象」。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Mapping

from ..config import PurifyConfig
from ..constants import BLOCK_TIME_SEC
from ..models import Side, Trade


@dataclass
class Verdict:
    entity: str
    labels: set[str] = field(default_factory=set)
    dropped: bool = False
    lane: str = "main"      # main | sniper | excluded
    reasons: list[str] = field(default_factory=list)

    def mark(self, label: str, reason: str, drop: bool = True, lane: str | None = None) -> None:
        self.labels.add(label)
        self.reasons.append(reason)
        if lane:
            self.lane = lane
        if drop:
            self.dropped, self.lane = True, "excluded"


# ------------------------------------------------------------------ wash trading
def detect_wash_scc(
    trades: Iterable[Trade],
    addr_to_entity: Mapping[str, str] | None = None,
    occurrence_threshold: int = 100,
) -> dict[str, int]:
    """Victor & Weintraud (WWW 2021) 的 SCC 循环检测，简化为按代币构图。

    原文流程：构建代币转移有向多重图 → 求所有强连通分量 → 每轮把边权重减 1 →
    重复直到无边 → 统计每个地址出现在 SCC 中的次数 → ≥100 判定为 wash trader。

    这里用 Tarjan 求 SCC，边 = 同一代币上「A 卖出后 B 买入」的时序邻接。
    真实系统应改用 ERC20 Transfer 图（更完整），本实现给出可跑通的等价骨架。
    """
    a2e = addr_to_entity or {}
    by_token: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.success:
            by_token[t.token].append(t)

    occ: dict[str, int] = defaultdict(int)
    mirror: dict[tuple[str, str], int] = defaultdict(int)
    mirror_tokens: dict[tuple[str, str], set[str]] = defaultdict(set)

    for tok, ts in by_token.items():
        ts.sort(key=lambda x: (x.block, x.log_index))
        edges: dict[str, set[str]] = defaultdict(set)
        last_seller: str | None = None
        for i, t in enumerate(ts):
            e = a2e.get(t.wallet, t.wallet)
            if t.side is Side.SELL:
                last_seller = e
            elif last_seller and last_seller != e:
                edges[last_seller].add(e)
            # ---- 成交量匹配（Victor & Weintraud 算法 2 的实用化）：
            # 同块（或相邻块）出现金额几乎相同的反向成交 ⇒ 净头寸变化≈0，
            # 即「没有承担市场风险、也没有改变持仓」——CFTC 对 wash trade 的定义。
            for j in range(i + 1, min(i + 6, len(ts))):
                o = ts[j]
                if o.block - t.block > 1 or o.side is t.side:
                    continue
                oe = a2e.get(o.wallet, o.wallet)
                if oe == e:
                    continue
                m = max(t.usd, o.usd)
                if m > 0 and abs(t.usd - o.usd) / m <= 0.01:
                    key = (e, oe) if e < oe else (oe, e)
                    mirror[key] += 1
                    mirror_tokens[key].add(tok)
        for comp in _tarjan_scc(edges):
            if len(comp) > 1:
                for node in comp:
                    occ[node] += len(comp)

    # 两级确认：SCC 只是候选生成器（单用它误报很高 —— 正常交易者反复买卖
    # 同一批热门币也会成环）。必须叠加「镜像成交」证据才判定。
    confirmed: dict[str, int] = {}
    for (a, b), cnt in mirror.items():
        if cnt >= 10 and len(mirror_tokens[(a, b)]) >= 3:
            for node in (a, b):
                confirmed[node] = max(confirmed.get(node, 0), occ.get(node, 0) + cnt)
    return {k: v for k, v in confirmed.items()
            if v >= occurrence_threshold or occ.get(k, 0) >= occurrence_threshold}


def _tarjan_scc(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """迭代版 Tarjan，避免深图爆栈。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    nodes = set(graph) | {v for vs in graph.values() for v in vs}

    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recurse = False
            succs = sorted(graph.get(node, ()))
            for i in range(pi, len(succs)):
                w = succs[i]
                if w not in index:
                    work[-1] = (node, i + 1)
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if recurse:
                continue
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                out.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return out


# ------------------------------------------------------------------ 主净化流程
def purify(
    entity_stats: Mapping[str, dict],
    cfg: PurifyConfig,
    wash_entities: Mapping[str, int] | None = None,
) -> dict[str, Verdict]:
    """8 步硬性剔除顺序（docs/02 第 3.5 节）。

    1. 实体聚类（在 normalize.entity 中已完成）
    2. 剔除 bundler（与建池同块建仓）
    3. 剔除 MEV（中位持仓 < 1 块）
    4. 剔除 bump bot（flip_ratio > 50）
    5. 剔除 wash trader（SCC）
    6. 剔除 transfer_in 占比 > 30%（成本基准不可信）
    7. 降权 sniper（1~3 块内建仓）→ 单独分赛道，不删
    8. 剔除样本量不足
    """
    wash = wash_entities or {}
    out: dict[str, Verdict] = {}

    for e, s in entity_stats.items():
        v = Verdict(entity=e)

        bd = s.get("min_block_delta", 10**9)
        if bd <= cfg.bundler_block_delta:
            v.mark("bundler", f"与建池同块建仓 (block_delta={bd})")

        holds = [h for h in s.get("hold_seconds", []) if h >= 0]
        med_hold = median(holds) if holds else 0.0
        if holds and med_hold < cfg.mev_hold_blocks_max * BLOCK_TIME_SEC:
            v.mark("mev", f"中位持仓 {med_hold:.2f}s < {cfg.mev_hold_blocks_max} 块")

        n_tok = max(1, s.get("n_tokens", 1))
        flip_ratio = s.get("flips", 0) / n_tok
        if flip_ratio > cfg.flip_ratio_max:
            v.mark("bump_bot", f"flip_ratio={flip_ratio:.1f} > {cfg.flip_ratio_max}")

        if e in wash:
            v.mark("wash", f"SCC 出现 {wash[e]} 次 ≥ {cfg.wash_scc_occurrence}")

        tir = s.get("transfer_in_ratio", 0.0)
        if tir > cfg.transfer_in_usd_ratio_max:
            v.mark("cost_basis_unreliable",
                   f"transfer_in 占流入 {tir:.0%} > {cfg.transfer_in_usd_ratio_max:.0%}")

        if s.get("n_trades", 0) < cfg.min_trades:
            v.mark("insufficient_sample",
                   f"仅 {s.get('n_trades', 0)} 笔 < {cfg.min_trades}")

        # 第 7 步：sniper 不删，分赛道
        if not v.dropped and cfg.bundler_block_delta < bd <= cfg.sniper_block_delta_max:
            v.mark("sniper",
                   f"block_delta={bd} ≤ {cfg.sniper_block_delta_max}，跟单不可复制",
                   drop=False, lane="sniper")

        out[e] = v
    return out


def summarize(verdicts: Mapping[str, Verdict]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for v in verdicts.values():
        c[f"lane:{v.lane}"] += 1
        for lb in v.labels:
            c[f"label:{lb}"] += 1
    c["total"] = len(verdicts)
    return dict(c)
