"""端到端管线编排。

    ingest → 实体聚类 → PnL → 净化 → 基数指标 → 赛马(Elo-MMR)
           → 统计闸门 → 可跟单性回测 → 选手池 → 实时告警

顺序不可颠倒。尤其是「实体聚类必须在 PnL 之前」和「净化必须在评分之前」：
不先合并 sybil，20 个分身里最幸运的那个就会以「高手」身份进入评分；
不先剔除 MEV / bundler，任何按胜率排序的榜单榜首必然是它们。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .backtest.copyable import batch_copy_backtest
from .config import Config
from .features.metrics import compute_metrics
from .ingest.loader import load_dataset
from .normalize.entity import cluster_entities, entity_report
from .normalize.flows import flow_summary, label_internal
from .pnl.engine import compute_positions, entity_rollup
from .purify.filters import detect_wash_scc, purify, summarize
from .race.engine import build_races, rank_percentiles, run_season
from .realtime.watcher import Watcher, roster_from_leaderboard
from .scoring.elo_mmr import EloMMR
from .scoring.gates import apply_gates


def run(cfg: Config, verbose: bool = True, ds=None) -> dict[str, Any]:
    """ds 可传入预先装载好的 Dataset（时间外样本验证会复用同一份数据切片，
    避免对同一段历史重复计费）。"""
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    out: dict[str, Any] = {}

    # ---------------------------------------------------------- 0 数据
    ds = ds if ds is not None else load_dataset(cfg, verbose=verbose)
    trades, tokens = ds.trades, ds.tokens
    log(f"[0] 数据       source={ds.source}  trades={len(trades):,}  "
        f"tokens={len(tokens):,}  wallets={len({t.wallet for t in trades}):,}")

    # ---------------------------------------------------------- 1 实体聚类
    # 没有资金流时只有行为聚类规则生效，反 sybil 能力会明显下降 ——
    # 这是真实数据源接入初期最该优先补齐的一块。
    a2e, reason = cluster_entities(trades, ds.funding, cfg.sybil)
    clusters = entity_report(a2e, reason)
    # ★ 内部转账只有在聚类之后才认得出来。不回标的话，一个操盘手在自己
    #   多个地址之间倒仓，每倒一次就凭空产生一次「盈利」。
    n_internal = label_internal(ds.flows, a2e)
    log(f"[1] 实体聚类   {len(a2e):,} 地址 → {len(set(a2e.values())):,} 实体"
        f"（合并出 {len(clusters)} 个多地址簇，回标内部转账 {n_internal} 条）")

    # ---------------------------------------------------------- 2 PnL
    positions = compute_positions(trades, ds.flows, tokens, ds.marks, a2e)
    stats = entity_rollup(positions)
    cov = [p.cost_coverage for p in positions.values() if p.inflow_usd_total > 0]
    med_cov = sorted(cov)[len(cov) // 2] if cov else 1.0
    log(f"[2] PnL 结算   {len(positions):,} 个 (实体,代币) 仓位  "
        f"成本覆盖率中位 {med_cov:.0%}"
        + ("  ⚠️ 未接转账流，老鼠仓/空投的无限 ROI 不会被拦" if not ds.flows else "")
        + (f"  flows={flow_summary(ds.flows)}" if ds.flows else ""))

    # ---------------------------------------------------------- 3 净化
    wash = detect_wash_scc(trades, a2e, cfg.purify.wash_scc_occurrence)
    verdicts = purify(stats, cfg.purify, wash)
    summary = summarize(verdicts)
    survivors = [e for e, v in verdicts.items() if not v.dropped]
    lanes = {e: v.lane for e, v in verdicts.items()}
    log(f"[3] 净化       存活 {len(survivors):,}/{len(verdicts):,}  {summary}")

    # ---------------------------------------------------------- 4 基数指标
    rets_by_entity: dict[str, list[float]] = defaultdict(list)
    toks_by_entity: dict[str, list[str]] = defaultdict(list)
    for (e, tok), p in positions.items():
        if e in verdicts and not verdicts[e].dropped and p.invested_usd > 0:
            rets_by_entity[e].append(p.ret)
            toks_by_entity[e].append(tok)

    metrics = {
        e: compute_metrics(rets_by_entity[e], toks_by_entity[e],
                           B=cfg.gate.bootstrap_B, alpha=cfg.gate.bootstrap_alpha,
                           seed=cfg.seed)
        for e in survivors if len(rets_by_entity[e]) >= 3
    }
    log(f"[4] 基数指标   {len(metrics):,} 个实体完成指标与 cluster bootstrap")

    # ---------------------------------------------------------- 5 赛马
    keep = set(metrics)
    race_trades = [t for t in trades if a2e.get(t.wallet, t.wallet) in keep]
    races = build_races(race_trades, tokens, cfg.race, a2e)
    elo = EloMMR(cfg=cfg.elo)
    season = run_season(races, elo, cfg.race)
    lb_all = elo.leaderboard(min_races=3)
    log(f"[5] 赛马       {season['n_races_scored']}/{season['n_races_total']} 场有效，"
        f"人均 {season['avg_races_per_entity']:.1f} 场")

    # ---------------------------------------------------------- 6 统计闸门
    pcts = rank_percentiles(races, cfg.race.draw_margin_ret, cfg.race.min_participants)
    rows = [{
        "entity": e, "sharpe": m.sharpe, "t_stat": m.t_stat, "n": m.n,
        "skew": m.skew, "kurt": m.kurt, "mean_ret": m.mean_ret, "ci_low": m.ci_low,
        "profit_factor": m.profit_factor, "race_pct": pcts.get(e, []),
    } for e, m in metrics.items()]
    gates, diag = apply_gates(
        rows, cfg.gate.fdr_target, cfg.gate.dsr_min, cfg.gate.use_emax_sr_floor,
        noise_floor_B=cfg.gate.noise_floor_B, seed=cfg.seed,
        dsr_as_gate=cfg.gate.dsr_as_gate,
        min_profit_factor=cfg.gate.min_profit_factor, min_races=cfg.gate.min_races)
    passed = {g.entity for g in gates if g.passed}
    gate_by_entity = {g.entity: g for g in gates}
    log(f"[6] 统计闸门   名次 z 噪音基准={diag['race_z_noise_floor']:.2f}"
        f"（观测最大 {diag['race_z_max_observed']:.2f}）  "
        f"通过 {len(passed)}/{len(rows)}  π̂₀={diag['pi0']:.3f}  "
        f"估计真有技能者≈{diag['n_skilled']}")

    # ---------------------------------------------------------- 7 可跟单性
    lb = [r for r in lb_all if r["entity"] in passed]
    top = [r["entity"] for r in lb[:30]]
    tbe: dict[str, list] = defaultdict(list)
    for t in trades:
        e = a2e.get(t.wallet, t.wallet)
        if e in set(top):
            tbe[e].append(t)
    copy_res = batch_copy_backtest(top, tbe, ds.price_fn, cfg.copy, tokens)
    cf = {c.entity: c.copy_factor for c in copy_res}
    med_cf = sorted(cf.values())[len(cf) // 2] if cf else 0.0
    log(f"[7] 可跟单性   Top{len(top)} 回测完成，跟单可行性系数中位数 {med_cf:.2f}")

    # ---------------------------------------------------------- 8 选手池 + 实时
    roster = roster_from_leaderboard(lb, lanes, cf, a2e, top_n=50)
    alerts = Watcher(roster).replay(trades[-20000:])
    log(f"[8] 实时告警   选手池 main={len(roster.main)} sniper={len(roster.sniper)}，"
        f"回放产出 {len(alerts)} 条告警")

    out.update({
        "summary": {
            "n_trades": len(trades), "n_tokens": len(tokens),
            "n_addresses": len(a2e), "n_entities": len(set(a2e.values())),
            "n_clusters_merged": len(clusters),
            "purify": summary, "season": season, "gates": diag,
            "median_copy_factor": med_cf, "n_alerts": len(alerts),
        },
        "leaderboard": [
            {**r, "lane": lanes.get(r["entity"], "main"),
             "copy_factor": cf.get(r["entity"], 0.0),
             "sharpe": metrics[r["entity"]].sharpe if r["entity"] in metrics else 0.0,
             "profit_factor": metrics[r["entity"]].profit_factor if r["entity"] in metrics else 0.0,
             "hit_rate": metrics[r["entity"]].hit_rate if r["entity"] in metrics else 0.0,
             "n_trades": stats.get(r["entity"], {}).get("n_trades", 0),
             "net_pnl": stats.get(r["entity"], {}).get("net_pnl", 0.0),
             "race_z": gate_by_entity[r["entity"]].race_z if r["entity"] in gate_by_entity else 0.0,
             "p_bhy": gate_by_entity[r["entity"]].p_bhy if r["entity"] in gate_by_entity else 1.0}
            for r in lb[:50]
        ],
        "clusters": clusters[:20],
        "copy_backtest": [asdict(c) | {"copy_factor": c.copy_factor} for c in copy_res[:20]],
        "alerts": [asdict(a) for a in alerts[:50]],
        "_internal": {"truth": ds.truth, "sybil_truth": ds.sybil_truth,
                      "passed": sorted(passed), "lanes": lanes, "a2e": a2e,
                      "lb_all": lb_all},
    })
    return out


def validate(res: dict[str, Any], verbose: bool = True) -> dict[str, Any]:
    """用合成数据的 ground truth 检验管线：真有技能的人是否被留下、
    机器人和赌徒是否被挡住。这是上线前唯一能做的「已知答案」测试。"""
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    truth, a2e = res["_internal"]["truth"], res["_internal"]["a2e"]
    passed = set(res["_internal"]["passed"])
    lanes = res["_internal"]["lanes"]
    main_passed = {e for e in passed if lanes.get(e, "main") == "main"}

    if not truth:
        log("\n=== ground truth 验证 ===")
        log("  跳过：真实数据源没有 ground truth。真实数据上的替代验证手段：")
        log("   · 时间外样本（最有力）：用前 60 天选人，看这批人在后 30 天的实际表现")
        log("   · 置换检验：打乱名次重跑，确认榜首 z 确实落在噪音分布之外")
        log("   · 可跟单性回测：copy_factor 才是「能不能赚钱」的直接证据")
        log(f"  本次通过闸门 {len(passed)} 人（其中 main 赛道 {len(main_passed)} 人）")
        return {"by_kind": {}, "precision": None, "recall": None,
                "n_passed": len(passed), "n_main_passed": len(main_passed),
                "note": "no ground truth — 请改用 `smrace.cli oos` 做时间外样本验证"}

    def kind_of(entity: str) -> str:
        members = [a for a, e in a2e.items() if e == entity]
        kinds = {truth.get(m, "?") for m in members}
        return "sybil" if "sybil" in kinds else (sorted(kinds)[0] if kinds else "?")

    by_kind: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0, "passed_main": 0})
    all_entities = set(a2e.values())
    for e in all_entities:
        k = kind_of(e)
        by_kind[k]["total"] += 1
        if e in passed:
            by_kind[k]["passed"] += 1
        if e in main_passed:
            by_kind[k]["passed_main"] += 1

    n_skilled_passed = by_kind["skilled"]["passed_main"]
    n_skilled_total = by_kind["skilled"]["total"]
    # 精确率只在 main 赛道上算：sniper 被单独分赛道是**设计意图**，不是误判。
    precision = n_skilled_passed / len(main_passed) if main_passed else 0.0
    recall = n_skilled_passed / n_skilled_total if n_skilled_total else 0.0

    log("\n=== ground truth 验证 ===")
    log(f"  {'类型':<9} {'总数':>6} {'通过闸门':>8} {'其中 main 赛道':>14} {'通过率':>8}")
    for k in sorted(by_kind, key=lambda x: -by_kind[x]["total"]):
        v = by_kind[k]
        log(f"  {k:<9} {v['total']:>6d} {v['passed']:>8d} {v['passed_main']:>14d} "
            f"{v['passed']/max(1,v['total']):>8.1%}")
    log(f"  → main 赛道精确率 {precision:.1%}   召回率 {recall:.1%}   "
        f"（{n_skilled_passed}/{n_skilled_total} 个真高手被找到，"
        f"main 名单共 {len(main_passed)} 人 / 全部通过 {len(passed)} 人）")
    return {"by_kind": {k: dict(v) for k, v in by_kind.items()},
            "precision": precision, "recall": recall,
            "n_main_passed": len(main_passed), "n_passed": len(passed)}


def save(res: dict[str, Any], out_dir: str = "out") -> list[str]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    files = []
    payload = {k: v for k, v in res.items() if k != "_internal"}
    p = d / "report.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    files.append(str(p))

    lines = ["# 聪明钱赛马 · 榜单", "",
             "| # | 实体 | 赛道 | 展示分 μ−3σ | μ | σ | 场次 | Sharpe | 盈亏比 | 胜率 | 跟单系数 | 净盈亏 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    def short(e: str) -> str:
        # 地址中段省略：只截前缀会让 0xa00…001 和 0xa00…002 显示成同一个
        return e if len(e) <= 22 else f"{e[:10]}…{e[-6:]}"

    for i, r in enumerate(res.get("leaderboard", []), 1):
        lines.append(
            f"| {i} | `{short(r['entity'])}` | {r['lane']} | {r['display']:.1f} | "
            f"{r['mu']:.1f} | {r['sigma']:.1f} | {r['n_races']} | {r['sharpe']:.2f} | "
            f"{r['profit_factor']:.2f} | {r['hit_rate']:.0%} | {r['copy_factor']:.2f} | "
            f"${r['net_pnl']:,.0f} |")
    p2 = d / "leaderboard.md"
    p2.write_text("\n".join(lines))
    files.append(str(p2))
    return files
