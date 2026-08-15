"""实体聚类（反 sybil）。

为什么必须做：一个操盘手用 20 个地址分仓，运气最好的那个会出现在榜首，
它的 19 个亏损兄弟被榜单忽略 —— 这是「幸存者偏差 + 选择偏差」的组合，
是所有聪明钱榜单最隐蔽也最致命的污染源。

规则来自 Wormhole 空投反女巫实践：ownership clustering、source-of-funds
（diffusion / sequential diffusion funding）、behavioral clustering、tx spam。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from ..config import SybilConfig
from ..models import Flow, Side, Trade


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.reason: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # 路径压缩
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str, reason: str = "") -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.parent[rb] = ra
        if reason:
            self.reason[ra] = self.reason.get(ra, "") or reason

    def groups(self) -> dict[str, set[str]]:
        g: dict[str, set[str]] = defaultdict(set)
        for a in list(self.parent):
            g[self.find(a)].add(a)
        return dict(g)


def cluster_entities(
    trades: Iterable[Trade],
    funding_flows: Iterable[Flow] = (),
    cfg: SybilConfig | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (addr -> entity_id, entity_id -> 聚类理由)。

    实现四条规则：
      A 共同资金源：同一地址在窗口内向 ≥N 个地址转出近似数额的原生币
      C 同区块共现：建仓区块集合 Jaccard > 阈值 且共现次数 ≥ 阈值
      D 代币重合度：交易过的代币集合 Jaccard > 阈值 且首买区块差 ≤ 阈值
      E 利润归集：多个地址的资金最终汇入同一地址（最强证据）

    规则 B（首笔 BNB 流入来自同一 EOA）在 A 中被覆盖。
    真实系统还应叠加 Arkham 的实体标签作为外部先验。
    """
    cfg = cfg or SybilConfig()
    uf = UnionFind()
    trades = list(trades)
    funding_flows = list(funding_flows)

    for t in trades:
        uf.find(t.wallet)

    # ---- 规则 A / E：资金流向（一跳）
    out_edges: dict[str, list[Flow]] = defaultdict(list)
    in_edges: dict[str, list[Flow]] = defaultdict(list)
    for f in funding_flows:
        if f.amount < 0 and f.counterparty:
            out_edges[f.wallet].append(f)
            in_edges[f.counterparty].append(f)

    for src, fs in out_edges.items():
        fs.sort(key=lambda x: x.ts)
        i = 0
        while i < len(fs):
            j, base = i, abs(fs[i].amount)
            group = []
            while j < len(fs) and fs[j].ts - fs[i].ts <= cfg.funding_window_sec:
                amt = abs(fs[j].amount)
                if base > 0 and abs(amt - base) / base <= cfg.funding_amount_tol:
                    group.append(fs[j].counterparty)
                j += 1
            if len(set(group)) >= cfg.funding_fanout_min:
                first = group[0]
                for g in group[1:]:
                    uf.union(first, g, f"共同资金源 {src[:10]}… 播撒 {len(set(group))} 地址")
            i = j if j > i else i + 1

    if cfg.enable_profit_sink:
        for sink, fs in in_edges.items():
            senders = {f.wallet for f in fs}
            if len(senders) >= cfg.funding_fanout_min:
                s = sorted(senders)
                for other in s[1:]:
                    uf.union(s[0], other, f"利润归集至 {sink[:10]}…")

    # ---- 规则 C / D：行为共现
    blocks: dict[str, set[int]] = defaultdict(set)
    toks: dict[str, set[str]] = defaultdict(set)
    first_buy: dict[tuple[str, str], int] = {}
    for t in trades:
        if t.side is Side.BUY:
            blocks[t.wallet].add(t.block)
            toks[t.wallet].add(t.token)
            k = (t.wallet, t.token)
            if k not in first_buy or t.block < first_buy[k]:
                first_buy[k] = t.block

    # 用倒排索引把候选对压到「至少共享一个代币」的范围内，避免 O(N²)
    tok_index: dict[str, set[str]] = defaultdict(set)
    for w, ts in toks.items():
        for tk in ts:
            tok_index[tk].add(w)

    checked: set[tuple[str, str]] = set()
    for tk, ws in tok_index.items():
        if len(ws) > 400:   # 热门币参与者太多，共现无信息量
            continue
        ws_l = sorted(ws)
        for i in range(len(ws_l)):
            for j in range(i + 1, len(ws_l)):
                a, b = ws_l[i], ws_l[j]
                if (a, b) in checked:
                    continue
                checked.add((a, b))
                ja = _jaccard(toks[a], toks[b])
                if ja >= cfg.token_jaccard_min:
                    gaps = [
                        abs(first_buy[(a, t)] - first_buy[(b, t)])
                        for t in (toks[a] & toks[b])
                        if (a, t) in first_buy and (b, t) in first_buy
                    ]
                    if gaps and _median(gaps) <= cfg.token_first_buy_block_gap:
                        uf.union(a, b, f"代币重合 Jaccard={ja:.2f}，首买区块差中位 {_median(gaps):.0f}")
                        continue
                # 区块共现单独一条太容易误伤：热门时段本来就有大量地址同块建仓。
                # 要求「区块共现」与「代币重合」双条件同时成立才合并 ——
                # 过度合并会把两个互不相干的操盘手判成同一人，比漏合并更难排查。
                shared = blocks[a] & blocks[b]
                if len(shared) >= cfg.cooccur_min_count and ja >= cfg.cooccur_jaccard_min:
                    jb = _jaccard(blocks[a], blocks[b])
                    if jb >= cfg.cooccur_jaccard_min:
                        uf.union(a, b, f"建仓区块共现 {len(shared)} 次，Jaccard={jb:.2f}"
                                       f"，代币重合={ja:.2f}")

    groups = uf.groups()
    addr_to_entity: dict[str, str] = {}
    entity_reason: dict[str, str] = {}
    for root, members in groups.items():
        # ⚠️ entity_id 必须用**完整**根地址：截断前缀会让不同簇撞成同一个 id，
        # 表现为「所有女巫被合并成一个巨型实体」，而聚类算法本身其实是对的。
        eid = root if len(members) == 1 else f"cluster:{root}#{len(members)}"
        for m in members:
            addr_to_entity[m] = eid
        if len(members) > 1:
            entity_reason[eid] = uf.reason.get(root, "行为聚类")
    return addr_to_entity, entity_reason


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def _median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return float(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def entity_report(addr_to_entity: Mapping[str, str], reason: Mapping[str, str]) -> list[dict]:
    members: dict[str, list[str]] = defaultdict(list)
    for a, e in addr_to_entity.items():
        members[e].append(a)
    rows = [
        {"entity": e, "n_addresses": len(ms), "reason": reason.get(e, ""), "addresses": sorted(ms)}
        for e, ms in members.items() if len(ms) > 1
    ]
    rows.sort(key=lambda r: -r["n_addresses"])
    return rows
