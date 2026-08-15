"""统计显著性闸门 —— 整个系统最容易被跳过、也最致命的一层。

问题：从 10,000 个地址里按 PnL 挑前 100 名，即使所有地址都是零 alpha 的赌徒，
你也一定能挑出「看起来很神」的一批。不做多重检验校正，榜单 = 噪音排序。

参照量级：Barras-Scaillet-Wermers 对 2,076 只共同基金（1975-2006）的结论是
费后真有技能者仅 0.6%。在匿名、高摩擦、充斥机器人的 BSC memecoin 中，
先验上应该预期「真聪明钱」占比是个位数千分比甚至更低。
系统必须默认「绝大多数候选是噪音」，而不是默认「榜单前 100 都是高手」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam 逆正态近似，避免引入 scipy。"""
    p = min(max(p, 1e-15), 1 - 1e-15)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """Bailey & López de Prado Eq.1：N 次独立试验下，**纯运气**能达到的期望最大 Sharpe。

        E[max SR] ≈ √V[SR] · [ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ]

    用法：sr_variance 传全体候选地址 Sharpe 的**横截面方差**，n_trials 传候选数。
    任何低于这条线的榜首都是纯噪音，直接丢弃。这是最省事也最有力的一道闸门。
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    s = math.sqrt(sr_variance)
    t1 = _norm_ppf(1.0 - 1.0 / n_trials)
    t2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return s * ((1.0 - EULER_GAMMA) * t1 + EULER_GAMMA * t2)


def deflated_sharpe_ratio(sr: float, n_obs: int, skew: float, kurt: float, sr0: float) -> float:
    """DSR = 以 E[max SR] 为门槛的 Probabilistic Sharpe Ratio。DSR > 0.95 才认技能。

    分母中的偏度/峰度修正对 memecoin 至关重要 —— 极端右偏会让朴素 Sharpe 的
    置信区间严重失真。
    """
    if n_obs < 3:
        return 0.0
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    if denom <= 1e-12:
        return 0.0
    return _norm_cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def bhy_adjust(pvalues: list[float] | np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg-Yekutieli 校正（控制 FDR，允许任意相关结构）。

    本场景必须用 BHY 而不是 BH：地址收益高度相关（大家买的是同一批代币）。

        p_adj(i) = min[ p_adj(i+1),  M·c(M)/i · p(i) ],  c(M) = Σ_{j=1..M} 1/j

    参考量级：M=10,000 时 c(M)≈9.79，FDR=10% 下最小 p 值需 ≤ 1.0e-6
    （约对应 t≈4.9）。相比之下 t>1.645 的常见门槛在这种选择空间下几乎无效。
    """
    p = np.asarray(list(pvalues), dtype=float)
    M = len(p)
    if M == 0:
        return p
    order = np.argsort(p)
    ps = p[order]
    cM = float(np.sum(1.0 / np.arange(1, M + 1)))
    adj = np.empty(M)
    running = 1.0
    for i in range(M - 1, -1, -1):
        val = M * cM / (i + 1) * ps[i]
        running = min(running, val)
        adj[i] = min(running, 1.0)
    out = np.empty(M)
    out[order] = adj
    return out


def pvalue_from_t(t: float, n: int) -> float:
    """双尾 p 值。n 较大时用正态近似即可（本场景 n 通常 ≥ 20）。"""
    if n < 3:
        return 1.0
    return float(2.0 * (1.0 - _norm_cdf(abs(t))))


def estimate_pi0(pvalues: list[float] | np.ndarray, lam: float = 0.5) -> float:
    """Barras-Scaillet-Wermers Eq.5：估计「零 alpha 地址」的比例。

        π̂₀(λ) = [W(λ)/M] · 1/(1−λ)，W(λ)=p 值 > λ 的地址数

    原理：真零 alpha 的 p 值在 [0,1] 上均匀分布，取高阈值外推即可。
    论文指出 λ 固定在 0.5 或 0.6 效果与 bootstrap 选优相近。
    """
    p = np.asarray(list(pvalues), dtype=float)
    M = len(p)
    if M == 0 or lam >= 1.0:
        return 1.0
    return float(min(1.0, (p > lam).sum() / M / (1.0 - lam)))


def fdr_plus(pvalues: list[float] | np.ndarray, gamma: float, pi0: float | None = None) -> dict:
    """右尾 FDR 分解（Eq.7 / Eq.12）：本期估计有多少个地址是**真有技能**的。

        T̂⁺(γ) = Ŝ⁺(γ) − π̂₀·γ/2        真技能比例
        FDR⁺(γ) = π̂₀·(γ/2) / Ŝ⁺(γ)     该显著性水平下的假发现率

    实操价值：可以直接构造「FDR ≤ 10% 的跟单组合」，而不是拍脑袋取 Top 50。
    """
    p = np.asarray(list(pvalues), dtype=float)
    M = len(p)
    if M == 0:
        return {"pi0": 1.0, "S_plus": 0.0, "T_plus": 0.0, "fdr_plus": 1.0, "n_skilled": 0}
    if pi0 is None:
        pi0 = estimate_pi0(p)
    s_plus = float((p < gamma).sum() / M)   # 调用方需保证只传右尾（收益为正）的地址
    lucky = pi0 * gamma / 2.0
    t_plus = max(0.0, s_plus - lucky)
    fdr = float(lucky / s_plus) if s_plus > 1e-12 else 1.0
    return {"pi0": float(pi0), "S_plus": s_plus, "T_plus": t_plus,
            "fdr_plus": min(1.0, fdr), "n_skilled": int(round(t_plus * M))}


def race_zscore(percentiles: list[float] | np.ndarray) -> tuple[float, int]:
    """序数统计量：名次分位均值相对 Uniform(0,1) 零假设的 z 值。

        H₀: 无技能 ⇒ 分位 ~ U(0,1)，均值 0.5，方差 1/12
        z = (p̄ − 0.5) / sqrt( (1/12) / n )

    **为什么用它做主闸门**：零假设方差是解析已知的（1/12）。而 Sharpe 的
    横截面方差会被「系统性亏钱的赌徒」污染 —— 在 memecoin 人群里 SR 均值可以
    低到 −0.6，此时横截面方差反映的是真实技能离散度 + 系统性亏损，
    根本不是零假设下的噪音幅度，直接代入 E[max SR] 公式会得到荒谬的高基准线，
    把所有人（包括真高手）一刀切掉。
    """
    p = np.asarray(list(percentiles), dtype=float)
    n = len(p)
    if n < 5:
        return 0.0, n
    return float((p.mean() - 0.5) / math.sqrt((1.0 / 12.0) / n)), n


def expected_max_z(n_trials: int, corr_inflation: float = 1.0) -> float:
    """N 个零技能选手中，**纯运气**能达到的最高 z 值。

    corr_inflation：选手之间的收益是高度相关的（大家买同一批代币），
    有效独立试验数小于 N，但相关也会把极值往上推。实践中建议用合成数据
    或置换检验（打乱名次重跑）标定这个系数，而不是拍一个数。
    """
    if n_trials < 2:
        return 0.0
    return corr_inflation * expected_max_sharpe(1.0, n_trials)


def permutation_noise_floor(
    n_per_entity: list[int], n_trials: int, B: int = 200, seed: int = 0
) -> float:
    """置换检验标定的噪音基准线：在完全无技能的零世界里重采样 B 次，
    取「每次的最大 z」的 95 分位。比解析公式更贴合真实的相关结构。

    这是整个闸门里最值得投入算力的一步 —— 它直接回答
    「我榜首那个 z=8.5 的人，在纯运气世界里有多常见」。
    """
    rng = np.random.default_rng(seed)
    # 真实数据里会出现 0 场次的实体（它的比赛全部因参与人数不足被判无效）。
    # 它们产生不了 z，也会让 sqrt(1/12/n) 除零 —— 从零世界重采样中剔除。
    ns = np.asarray([int(n) for n in n_per_entity if int(n) >= 1])
    if ns.size == 0:
        return 0.0
    maxes = np.empty(B)
    for b in range(B):
        zs = np.array([
            (rng.random(int(n)).mean() - 0.5) / math.sqrt((1.0 / 12.0) / int(n))
            for n in ns
        ])
        maxes[b] = zs.max()
    return float(np.quantile(maxes, 0.95))


@dataclass
class GateResult:
    entity: str
    race_z: float = 0.0
    n_races: int = 0
    sharpe: float = 0.0
    t_stat: float = 0.0
    p_raw: float = 1.0
    p_bhy: float = 1.0
    dsr: float = 0.0
    passed: bool = False
    reason: str = ""


def apply_gates(
    rows: list[dict],
    fdr_target: float = 0.10,
    dsr_min: float = 0.95,
    use_emax_floor: bool = True,
    noise_floor_B: int = 200,
    seed: int = 0,
    dsr_as_gate: bool = False,
    min_profit_factor: float = 1.2,
    min_races: int = 10,
) -> tuple[list[GateResult], dict]:
    """双闸门。rows 需要包含：
        entity, race_pct(list[float]),               ← 序数（赛马名次）
        sharpe, t_stat, n, skew, kurt, mean_ret, ci_low, profit_factor  ← 基数

    闸门 A · 序数（主，做统计显著性）：
        名次 z > 置换噪音基准线，且 BHY 校正后 p ≤ FDR 目标
    闸门 B · 基数（辅，做经济显著性）：
        平均收益 > 0、盈亏比 ≥ 阈值、cluster bootstrap CI 下界 > 0

    ⚠️ **DSR 默认不作为硬闸门**（dsr_as_gate=False，只计算并展示）。
    原因：DSR / Sharpe 的推断假设收益近似 iid、偏度峰度温和。memecoin 的
    单笔收益分布是「八成 −100%、少数 +10000%」，偏度可以到 5 以上，
    此时 DSR 的分母 1 − γ₃·SR + (γ₄−1)/4·SR² 会塌陷，**所有人的 DSR 都趋近 0，
    包括真有技能的人**。实测中它会把 8/8 真高手全部误杀。
    序数统计量没有这个问题：名次分位的零假设是 Uniform(0,1)，方差解析已知
    (1/12)，与收益分布形状完全无关 —— 这就是「赛马」相对「PnL 榜单」
    在统计上的根本优势，也是本框架把序数放在主闸门位置的理由。
    若你的标的池换成流动性更好、收益分布更温和的资产，可以把 DSR 打开。
    """
    if not rows:
        return [], {}
    n_trials = len(rows)

    zs, ns = [], []
    for r in rows:
        z, n = race_zscore(r.get("race_pct", []))
        zs.append(z)
        ns.append(n)
    zs_a = np.asarray(zs)

    floor = permutation_noise_floor(ns, n_trials, B=noise_floor_B, seed=seed) \
        if (use_emax_floor and noise_floor_B > 0) else expected_max_z(n_trials)

    p_raw = np.array([pvalue_from_t(z, max(n, 3)) for z, n in zip(zs, ns)])
    p_adj = bhy_adjust(p_raw)

    srs = np.array([r["sharpe"] for r in rows], dtype=float)
    # DSR 的 sr0 用**零假设下**的 Sharpe 估计量标准差，而不是横截面方差
    #   Var(SR_hat) ≈ (1 + SR²/2)/n  ⇒ SR≈0 时 ≈ 1/n
    med_n = float(np.median([max(r["n"], 3) for r in rows]))
    sr0 = expected_max_sharpe(1.0 / med_n, n_trials) if use_emax_floor else 0.0

    out: list[GateResult] = []
    for i, r in enumerate(rows):
        dsr = deflated_sharpe_ratio(r["sharpe"], r["n"], r.get("skew", 0.0),
                                    r.get("kurt", 3.0), sr0)
        reasons = []
        if ns[i] < min_races:
            reasons.append(f"参赛场次 {ns[i]} < {min_races}，样本不足")
        if use_emax_floor and zs[i] <= floor:
            reasons.append(f"名次 z={zs[i]:.2f} ≤ 置换噪音基准 {floor:.2f}")
        if p_adj[i] > fdr_target:
            reasons.append(f"BHY p={p_adj[i]:.3g} > FDR 目标 {fdr_target}")
        if r.get("mean_ret", 0.0) <= 0:
            reasons.append("平均收益 ≤ 0")
        if r.get("profit_factor", 0.0) < min_profit_factor:
            reasons.append(f"盈亏比 {r.get('profit_factor', 0.0):.2f} < {min_profit_factor}")
        if r.get("ci_low", 0.0) <= 0:
            reasons.append("bootstrap CI 下界 ≤ 0")
        if dsr_as_gate and dsr < dsr_min:
            reasons.append(f"DSR={dsr:.3f} < {dsr_min}")
        out.append(GateResult(
            entity=r["entity"], race_z=zs[i], n_races=ns[i],
            sharpe=r["sharpe"], t_stat=r["t_stat"],
            p_raw=float(p_raw[i]), p_bhy=float(p_adj[i]), dsr=dsr,
            passed=not reasons, reason="；".join(reasons),
        ))

    right = p_raw[[i for i, r in enumerate(rows) if r.get("mean_ret", 0.0) > 0]]
    diag = {
        "n_candidates": n_trials,
        "race_z_noise_floor": floor,
        "race_z_max_observed": float(zs_a.max()) if len(zs_a) else 0.0,
        "sr_cross_sectional_var": float(srs.var(ddof=1)) if n_trials > 1 else 0.0,
        "dsr_sr0": sr0,
        "dsr_used_as_gate": dsr_as_gate,
        "n_passed": sum(1 for g in out if g.passed),
        **fdr_plus(right, gamma=0.10),
    }
    return out, diag
