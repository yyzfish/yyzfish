"""统一数据装载：把任意数据源变成管线能吃的 Dataset。

真实数据源和合成数据源的差别只在这一层被抹平，`pipeline.run()` 之后的所有
逻辑对两者完全一致 —— 这也意味着合成数据上验证过的行为，换真数据后不会
因为代码路径不同而失效。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..models import Flow, FlowKind, Side, TokenMeta, Trade
from ..normalize.flows import drop_swap_legs, pools_from_trades
from . import cache
from .adapters import build_source


@dataclass
class Dataset:
    trades: list[Trade] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    funding: list[Flow] = field(default_factory=list)  # 原生币转账，供反 sybil 用
    tokens: dict[str, TokenMeta] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    price_fn: Callable[[str, int], float] = lambda _t, _b: 0.0
    truth: dict[str, str] = field(default_factory=dict)        # 仅合成数据有
    sybil_truth: dict[str, str] = field(default_factory=dict)  # 仅合成数据有
    source: str = ""

    @property
    def has_truth(self) -> bool:
        return bool(self.truth)


def price_fn_from_trades(trades: list[Trade]) -> Callable[[str, int], float]:
    """用「该区块之前最后一笔成交的执行价」作为价格函数。

    这是生产环境的正确做法：memecoin 没有可靠的外部喂价，池子里最后一笔
    实际成交就是最好的价格估计，而且它天然含滑点（docs/02 §1.2）。
    比查 reserves / sqrtPriceX96 便宜得多，代价是流动性极差的币会有陈旧价格 ——
    可跟单性回测里的 min_pool_liquidity_usd 过滤正是为了压住这个问题。
    """
    import bisect
    from collections import defaultdict

    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for t in trades:
        if t.success and t.base_amount > 0:
            px = t.exec_price
            if px > 0:
                series[t.token].append((t.block, px))
    ordered = {k: sorted(v) for k, v in series.items()}
    blocks = {k: [b for b, _ in v] for k, v in ordered.items()}

    def fn(token: str, block: int) -> float:
        v = ordered.get(token)
        if not v:
            return 0.0
        i = bisect.bisect_right(blocks[token], block) - 1
        return v[i][1] if i >= 0 else v[0][1]
    return fn


def tokens_from_trades(trades: list[Trade]) -> dict[str, TokenMeta]:
    """兜底的代币元数据：用「首次出现的区块」当 launch_block。

    ⚠️ 这是**近似**。真正的 launch_block 应该来自 PairCreated / PoolCreated /
    TokenCreate 事件。用首笔成交近似会让 bundler（block_delta==0）判定偏松 ——
    因为你观测到的第一笔本身可能就是 bundler 的那笔。
    生产环境务必用数据源的 fetch_tokens() 拿真实建池区块。
    """
    out: dict[str, TokenMeta] = {}
    for t in trades:
        m = out.get(t.token)
        if m is None:
            out[t.token] = TokenMeta(address=t.token, launch_block=t.block, launch_ts=t.ts)
        elif t.block < m.launch_block:
            m.launch_block, m.launch_ts = t.block, t.ts
    return out


def marks_from_trades(trades: list[Trade]) -> dict[str, float]:
    """期末市价 = 每个代币最后一笔成交价。未平仓部分据此估未实现盈亏。"""
    last: dict[str, tuple[int, float]] = {}
    for t in trades:
        if not t.success:
            continue
        px = t.exec_price
        if px <= 0:
            continue
        cur = last.get(t.token)
        if cur is None or t.block >= cur[0]:
            last[t.token] = (t.block, px)
    return {k: v[1] for k, v in last.items()}


def load_dataset(cfg, verbose: bool = True) -> Dataset:
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    kind = cfg.data_source

    # ---------------------------------------------------------- 合成
    if kind == "synthetic":
        from .synthetic import TICK_BLOCKS, SyntheticSource
        from ..backtest.copyable import make_price_fn
        u = SyntheticSource(seed=cfg.seed).universe
        return Dataset(
            trades=u.trades, flows=u.flows, funding=u.funding,
            tokens=u.tokens, marks=u.marks,
            price_fn=make_price_fn(u.prices, u.tokens, TICK_BLOCKS),
            truth=u.truth, sybil_truth=u.sybil_truth, source="synthetic",
        )

    # ---------------------------------------------------------- 真实数据源
    end_ts = int(getattr(cfg, "end_ts", 0)) or int(time.time())
    start_ts = int(getattr(cfg, "start_ts", 0)) or (end_ts - cfg.lookback_days * 86400)

    tpath = cache.cache_path(cfg.out_dir, kind, "trades", start_ts, end_ts)
    if cache.exists(tpath):
        log(f"  [loader] 命中缓存 {tpath}")
        trades = list(cache.load_jsonl(tpath, Trade))
        for t in trades:                       # JSON 里 side 是字符串
            if isinstance(t.side, str):
                t.side = Side(t.side)
    else:
        src = build_source(kind)
        log(f"  [loader] 从 {kind} 拉取 {start_ts}~{end_ts}（{cfg.lookback_days} 天）…")
        trades = src.fetch_trades(None, start_ts, end_ts)
        n = cache.dump_jsonl(tpath, trades)
        log(f"  [loader] 已缓存 {n:,} 笔到 {tpath}")

    if not trades:
        raise RuntimeError(
            f"{kind} 没有返回任何成交。先跑 `python -m smrace.cli probe --source {kind}` "
            "检查凭证、字段映射与时间范围。"
        )

    # 代币元数据：优先用数据源的建池事件，拿不到就退回首笔成交近似
    tokens: dict[str, TokenMeta] = {}
    try:
        src = locals().get("src") or build_source(kind)
        tokens = src.fetch_tokens(start_ts, end_ts)
        log(f"  [loader] 建池事件覆盖 {len(tokens):,} 个代币")
    except (NotImplementedError, Exception) as e:  # noqa: BLE001
        log(f"  [loader] ⚠️ 拿不到建池事件（{type(e).__name__}），"
            f"退回「首笔成交」近似 launch_block —— bundler 判定会偏松")
    for tok, meta in tokens_from_trades(trades).items():
        if tok not in tokens:
            tokens[tok] = meta

    marks = marks_from_trades(trades)

    # ---- 转账流：补成本基准。缺了它「收币→卖出」的老鼠仓 = 无限 ROI
    flows: list[Flow] = []
    fpath = cache.cache_path(cfg.out_dir, kind, "flows", start_ts, end_ts)
    if cache.exists(fpath):
        flows = list(cache.load_jsonl(fpath, Flow))
        for f in flows:
            if isinstance(f.kind, str):
                f.kind = FlowKind(f.kind)
        log(f"  [loader] 转账流命中缓存 {len(flows):,} 条")
    else:
        try:
            src = locals().get("src") or build_source(kind)
            flows = src.fetch_flows(None, start_ts, end_ts,
                                    dex_pools=pools_from_trades(trades), marks=marks)
            # ★ 必须丢掉与 swap 同一笔交易的转账，否则同一笔买入被记两次
            before = len(flows)
            flows = drop_swap_legs(flows, trades)
            log(f"  [loader] 转账流 {before:,} 条 → 去掉 swap 腿后 {len(flows):,} 条")
            cache.dump_jsonl(fpath, flows)
        except NotImplementedError as e:
            log(f"  [loader] ⚠️ {kind} 未实现转账流：cost_coverage 将恒为 1.0，"
                "「收币→卖出」的老鼠仓会被算成无限 ROI，空投盈亏也无法隔离")
        except Exception as e:  # noqa: BLE001
            log(f"  [loader] ⚠️ 转账流拉取失败（{type(e).__name__}: {e}）—— 同上，结果会偏")

    # ---- 原生币资金流：反 Sybil 的规则 A/B/E 全靠它
    funding: list[Flow] = []
    gpath = cache.cache_path(cfg.out_dir, kind, "funding", start_ts, end_ts)
    if cache.exists(gpath):
        funding = list(cache.load_jsonl(gpath, Flow))
        for f in funding:
            if isinstance(f.kind, str):
                f.kind = FlowKind(f.kind)
        log(f"  [loader] 资金流命中缓存 {len(funding):,} 条")
    else:
        try:
            src = locals().get("src") or build_source(kind)
            funding = src.fetch_funding(start_ts, end_ts)
            log(f"  [loader] 资金流 {len(funding):,} 条")
            cache.dump_jsonl(gpath, funding)
        except (NotImplementedError, AttributeError):
            log("  [loader] ⚠️ 未接入原生币资金流：反 Sybil 只剩行为规则，"
                "一个操盘手的多个分身很可能不会被合并")
        except Exception as e:  # noqa: BLE001
            log(f"  [loader] ⚠️ 资金流拉取失败（{type(e).__name__}）—— 同上")

    return Dataset(
        trades=trades, flows=flows, funding=funding, tokens=tokens,
        marks=marks,
        price_fn=price_fn_from_trades(trades),
        source=kind,
    )
