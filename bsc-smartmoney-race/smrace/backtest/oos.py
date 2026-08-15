"""时间外样本验证（out-of-sample）—— 真实数据上唯一有说服力的验证手段。

合成数据有 ground truth，真实链上没有。所以在真实数据上，「精确率/召回率」
这类指标根本无法计算。能做的只有一件事：

    用前一段时间选人 → 看这批人在**没参与选拔的后一段时间**里表现如何
                      → 与「同期落选者」做对照

这直接检验了整个框架最想回答的问题：**选出来的人，是不是只是过去运气好？**

关键设计：对照组不是「全体地址」，而是「同样通过了净化层、同样有足够样本、
只是没通过统计闸门」的那批人。拿选中组去和全体（含机器人和一次性地址）比，
会得到一个虚高到没意义的差距。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..config import Config
from ..ingest.loader import (Dataset, marks_from_trades, price_fn_from_trades,
                             tokens_from_trades)
from ..normalize.entity import cluster_entities
from ..pnl.engine import compute_positions
from ..purify.filters import detect_wash_scc, purify
from ..race.engine import build_races, rank_percentiles
from ..scoring.gates import race_zscore


def slice_dataset(ds: Dataset, lo_ts: int, hi_ts: int) -> Dataset:
    """按时间戳切片。tokens / marks / price_fn 全部**只用切片内的数据重算** ——
    直接沿用全量的会造成前视偏差（训练期就知道了未来的最终价格）。"""
    tr = [t for t in ds.trades if lo_ts <= t.ts < hi_ts]
    fl = [f for f in ds.flows if lo_ts <= f.ts < hi_ts]
    fu = [f for f in ds.funding if f.ts < hi_ts]   # 资金关系是历史事实，可沿用

    tokens = {}
    for tok, meta in tokens_from_trades(tr).items():
        base = ds.tokens.get(tok)
        # launch_block 是代币的固有属性，可以沿用全量的（它早于切片起点）
        tokens[tok] = base if base is not None else meta
    return Dataset(
        trades=tr, flows=fl, funding=fu, tokens=tokens,
        marks=marks_from_trades(tr), price_fn=price_fn_from_trades(tr),
        truth=ds.truth, sybil_truth=ds.sybil_truth, source=ds.source + ":slice",
    )


@dataclass
class OosResult:
    n_selected: int = 0
    n_control: int = 0
    sel_median_z: float = 0.0
    ctl_median_z: float = 0.0
    sel_median_ret: float = 0.0
    ctl_median_ret: float = 0.0
    p_perm_z: float = 1.0        # 置换检验：中位 z 差异的显著性
    p_perm_ret: float = 1.0
    survival_rate: float = 0.0   # 选中组里，测试期仍活跃且表现在中位以上的比例
    detail: dict[str, Any] = field(default_factory=dict)


def _perm_test(a: np.ndarray, b: np.ndarray, B: int = 5000, seed: int = 0) -> float:
    """两组中位数差的置换检验。样本小、分布厚尾，t 检验不可信，用置换最稳妥。"""
    if len(a) < 3 or len(b) < 3:
        return 1.0
    obs = float(np.median(a) - np.median(b))
    pool = np.concatenate([a, b])
    n = len(a)
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(B):
        rng.shuffle(pool)
        if float(np.median(pool[:n]) - np.median(pool[n:])) >= obs:
            cnt += 1
    return (cnt + 1) / (B + 1)


def _test_period_stats(ds: Dataset, cfg: Config) -> tuple[dict[str, float], dict[str, float]]:
    """测试期每个实体的 (名次 z, 净收益率)。测试期**不做**统计闸门 ——
    我们要的是原始表现，不是又一次筛选。"""
    a2e, _ = cluster_entities(ds.trades, ds.funding, cfg.sybil)
    pos = compute_positions(ds.trades, ds.flows, ds.tokens, ds.marks, a2e)

    ret: dict[str, float] = {}
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for (e, _tok), p in pos.items():
        agg[e][0] += p.net_pnl
        agg[e][1] += p.invested_usd
    for e, (pnl, inv) in agg.items():
        if inv > 0:
            ret[e] = pnl / inv

    races = build_races(ds.trades, ds.tokens, cfg.race, a2e)
    pcts = rank_percentiles(races, cfg.race.draw_margin_ret, cfg.race.min_participants)
    z = {e: race_zscore(v)[0] for e, v in pcts.items() if len(v) >= 5}
    return z, ret


def run_oos(ds: Dataset, cfg: Config, split_ratio: float = 0.67,
            verbose: bool = True) -> OosResult:
    from ..pipeline import run as run_pipeline

    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    ts = sorted(t.ts for t in ds.trades)
    if len(ts) < 1000:
        raise RuntimeError("样本太少，时间外样本验证没有意义")
    lo, hi = ts[0], ts[-1] + 1
    split = ts[int(len(ts) * split_ratio)]

    train = slice_dataset(ds, lo, split)
    test = slice_dataset(ds, split, hi)
    log(f"[OOS] 训练期 {len(train.trades):,} 笔 / 测试期 {len(test.trades):,} 笔"
        f"（切点 ts={split}）")

    # ---- 训练期：完整跑一遍管线选人
    res = run_pipeline(cfg, verbose=False, ds=train)
    lanes = res["_internal"]["lanes"]
    passed = set(res["_internal"]["passed"])
    selected = {e for e in passed if lanes.get(e, "main") == "main"}

    # ---- 对照组：同样过了净化、同样有样本，只是没过统计闸门
    a2e_tr, _ = cluster_entities(train.trades, train.funding, cfg.sybil)
    pos_tr = compute_positions(train.trades, train.flows, train.tokens,
                               train.marks, a2e_tr)
    from ..pnl.engine import entity_rollup
    stats_tr = entity_rollup(pos_tr)
    verdicts_tr = purify(stats_tr, cfg.purify,
                         detect_wash_scc(train.trades, a2e_tr,
                                         cfg.purify.wash_scc_occurrence))
    control = {e for e, v in verdicts_tr.items()
               if not v.dropped and v.lane == "main" and e not in selected}
    log(f"[OOS] 选中 {len(selected)} 人，对照 {len(control)} 人")

    # ---- 测试期：只看原始表现
    z_test, ret_test = _test_period_stats(test, cfg)

    sel_z = np.array([z_test[e] for e in selected if e in z_test])
    ctl_z = np.array([z_test[e] for e in control if e in z_test])
    sel_r = np.array([ret_test[e] for e in selected if e in ret_test])
    ctl_r = np.array([ret_test[e] for e in control if e in ret_test])

    r = OosResult(
        n_selected=len(selected), n_control=len(control),
        sel_median_z=float(np.median(sel_z)) if len(sel_z) else 0.0,
        ctl_median_z=float(np.median(ctl_z)) if len(ctl_z) else 0.0,
        sel_median_ret=float(np.median(sel_r)) if len(sel_r) else 0.0,
        ctl_median_ret=float(np.median(ctl_r)) if len(ctl_r) else 0.0,
        p_perm_z=_perm_test(sel_z, ctl_z, seed=cfg.seed),
        p_perm_ret=_perm_test(sel_r, ctl_r, seed=cfg.seed),
        survival_rate=(float((sel_z > float(np.median(ctl_z))).mean())
                       if len(sel_z) and len(ctl_z) else 0.0),
        detail={"split_ts": split, "n_sel_active": int(len(sel_z)),
                "n_ctl_active": int(len(ctl_z)),
                "sel_dropout": 1.0 - (len(sel_z) / max(1, len(selected)))},
    )

    if verbose:
        log(f"\n=== 时间外样本验证 ===")
        log(f"  选中组   n={r.n_selected:4d}（测试期仍活跃 {r.detail['n_sel_active']}）"
            f"  中位名次 z={r.sel_median_z:+.2f}  中位收益率={r.sel_median_ret:+.1%}")
        log(f"  对照组   n={r.n_control:4d}（测试期仍活跃 {r.detail['n_ctl_active']}）"
            f"  中位名次 z={r.ctl_median_z:+.2f}  中位收益率={r.ctl_median_ret:+.1%}")
        log(f"  置换检验 p(名次 z)={r.p_perm_z:.4f}   p(收益率)={r.p_perm_ret:.4f}")
        log(f"  选中组在测试期跑赢对照组中位的比例：{r.survival_rate:.0%}")
        log(f"  选中组测试期失活率：{r.detail['sel_dropout']:.0%}"
            f"（失活本身就是问题 —— 跟不了一个已经不交易的地址）")
        if r.p_perm_z > 0.05 and r.p_perm_ret > 0.05:
            log("  ⚠️ 两项检验都不显著：说明这批人的优势没有延续到样本外，"
                "很可能只是过去运气好。不要上跟单。")
    return r
