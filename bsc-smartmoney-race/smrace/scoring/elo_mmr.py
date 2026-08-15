"""Elo-MMR(ρ)：面向大规模多人竞赛的评分系统（Ebtekar & Liu, WWW 2021）。

为什么不用 Elo / Glicko-2 / TrueSkill：
  · Elo 是两两对战模型，本场景是「N 人对同一批标的下注」的 free-for-all。
  · Glicko-2 存在 **volatility farming** 漏洞：选手可以故意打出差表现抬高 σ，
    随后一次好表现获得超额加分。在有金钱激励的加密场景中这必然被利用。
  · Elo-MMR(ρ) 的 log-cosh 损失在原点像 L2、在尾部像 L1 —— 产生鲁棒平均，
    **自动降权极端表现**。一次 1000x 的运气爆发不会让评分冲到榜首，
    而按 PnL 求和的榜单会。且论文 Thm 5.5 证明其激励相容。

两阶段：
  Stage 1 表现估计：解 Q_i(p)=0，即「期望名次 == 实际名次」的表现水平。
                    不做两两比较，用相邻比较约束。
  Stage 2 评分更新：解 L'(s)=0，
        L(s) = L₂((s−p₀)/β₀) + Σ_k L_R((s−p_k)/β_k),  L_R(x)=2·ln cosh(πx/√12)

已知简化（相对论文）：σ 的更新用 1/σ'² = 1/σ_d² + 1/β²（论文的高斯近似），
扩散用 σ_d²=σ²+γ² 并同步按 κ 膨胀历史项尺度。这些在 docs/02 第 4 节标注。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from ..config import EloConfig
from ..models import Rating

SQRT3 = math.sqrt(3.0)
SQRT12 = math.sqrt(12.0)
TANH_MULT = math.pi / SQRT3   # logistic → tanh 形式的尺度换算


# ---------------------------------------------------------------- Stage 1
def _perf_equation(p: float, others: Sequence[tuple[float, float, int]], my_rank: int) -> float:
    """Q_i(p) = Σ_j w_j·[ −tanh((p−μ_j)·a_j) + c_ij ]

    c_ij = −1 若 j 名次优于 i（i 输）；+1 若 j 名次劣于 i（i 赢）；0 若平局。
    """
    total = 0.0
    for mu, sig, rank in others:
        w = TANH_MULT / max(sig, 1e-9)
        a = w * 0.5
        val = -math.tanh((p - mu) * a) * w
        if rank < my_rank:
            val -= w
        elif rank > my_rank:
            val += w
        total += val
    return total


def _bisect(f, lo: float, hi: float, tol: float, max_iter: int) -> float:
    flo, fhi = f(lo), f(hi)
    guard = 0
    while flo * fhi > 0 and guard < 60:  # 自动扩张括号区间
        span = hi - lo
        lo -= span
        hi += span
        flo, fhi = f(lo), f(hi)
        guard += 1
    if flo * fhi > 0:
        return (lo + hi) / 2.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(fm) < tol or (hi - lo) < tol:
            return mid
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def estimate_performances(
    players: Sequence[tuple[str, Rating, int]],
    cfg: EloConfig,
) -> dict[str, float]:
    """players: [(entity_id, rating, rank)]，rank 从 0 开始，允许并列。"""
    terms = [(r.mu, math.sqrt(r.sig ** 2 + cfg.beta ** 2), rk) for _e, r, rk in players]
    out: dict[str, float] = {}
    for i, (eid, rating, rank) in enumerate(players):
        others = terms  # 含自身，其贡献为纯 tanh 项，不影响零点存在性
        lo = min(t[0] for t in others) - 6 * cfg.beta
        hi = max(t[0] for t in others) + 6 * cfg.beta
        out[eid] = _bisect(
            lambda p: _perf_equation(p, others, rank),
            lo, hi, cfg.solver_tol, cfg.solver_max_iter,
        )
    return out


# ---------------------------------------------------------------- Stage 2
def _rating_derivative(s: float, mu0: float, beta0: float,
                       hist: Sequence[tuple[float, float]]) -> float:
    """L'(s) = 2(s−p₀)/β₀² + Σ_k (2π/(√12·β_k))·tanh(π(s−p_k)/(√12·β_k))"""
    total = 2.0 * (s - mu0) / (beta0 ** 2)
    for pk, bk in hist:
        c = math.pi / (SQRT12 * max(bk, 1e-9))
        total += 2.0 * c * math.tanh(c * (s - pk))
    return total


def update_rating(rating: Rating, performance: float, cfg: EloConfig) -> Rating:
    """扩散 → 追加本场表现 → 鲁棒求解新 μ → 收缩 σ。"""
    sig_d = math.sqrt(rating.sig ** 2 + cfg.gamma ** 2)
    kappa = (rating.sig ** 2) / (sig_d ** 2) if sig_d > 0 else 1.0
    # 扩散：保持当前评分不变的前提下让不确定性精确增加 γ² ⇒ 膨胀历史项尺度
    hist = [(pk, bk / math.sqrt(max(kappa, 1e-9))) for pk, bk in rating.history]
    hist.append((performance, cfg.beta))
    if len(hist) > cfg.rho_history:   # ρ：超出部分丢给正态锚点
        hist = hist[-cfg.rho_history:]

    mu0, beta0 = cfg.mu_init, cfg.sig_init
    lo = min([mu0] + [p for p, _ in hist]) - 6 * cfg.beta
    hi = max([mu0] + [p for p, _ in hist]) + 6 * cfg.beta
    new_mu = _bisect(
        lambda s: _rating_derivative(s, mu0, beta0, hist),
        lo, hi, cfg.solver_tol, cfg.solver_max_iter,
    )
    new_sig = max(cfg.sig_floor, 1.0 / math.sqrt(1.0 / sig_d ** 2 + 1.0 / cfg.beta ** 2))
    return Rating(mu=new_mu, sig=new_sig, history=hist, n_races=rating.n_races + 1)


# ---------------------------------------------------------------- 赛季引擎
@dataclass
class EloMMR:
    cfg: EloConfig = field(default_factory=EloConfig)
    ratings: dict[str, Rating] = field(default_factory=dict)

    def get(self, entity: str) -> Rating:
        if entity not in self.ratings:
            self.ratings[entity] = Rating(mu=self.cfg.mu_init, sig=self.cfg.sig_init)
        return self.ratings[entity]

    def run_race(self, ranked: Sequence[tuple[str, int]]) -> dict[str, float]:
        """ranked: [(entity_id, rank)]，rank 从 0 开始，允许并列（平局）。"""
        if len(ranked) < 2:
            return {}
        players = [(e, self.get(e), rk) for e, rk in ranked]
        perfs = estimate_performances(players, self.cfg)
        for e, rating, _rk in players:
            self.ratings[e] = update_rating(rating, perfs[e], self.cfg)
        return perfs

    def leaderboard(self, min_races: int = 1) -> list[dict]:
        """展示值用 TrueSkill 式保守分位 μ−3σ：样本少 → σ 大 → 排不上前面。
        这一条直接解决了「4 笔交易 100% 胜率」的问题。"""
        rows = [
            {"entity": e, "display": r.display, "mu": r.mu, "sigma": r.sig, "n_races": r.n_races}
            for e, r in self.ratings.items() if r.n_races >= min_races
        ]
        rows.sort(key=lambda x: -x["display"])
        return rows
