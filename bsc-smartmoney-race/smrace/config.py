"""全局配置。所有魔法数字集中在这里，方便调参与回测敏感性分析。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import yaml  # 可选
except Exception:  # pragma: no cover
    yaml = None


@dataclass
class PurifyConfig:
    """净化层阈值。每一条都对应 docs/02 里的一个已知污染模式。"""
    bundler_block_delta: int = 0        # 与建池同块建仓 → bundler，绝对不可跟单
    sniper_block_delta_max: int = 3     # 1~3 块内（≈1.35s）→ sniper，单独分赛道
    mev_hold_blocks_max: int = 1        # 中位持仓 < 1 块 → 三明治 MEV
    flip_ratio_max: float = 50.0        # 等量买卖翻转比 > 50 → bump bot
    wash_scc_occurrence: int = 100      # SCC 出现次数 ≥ 100 → wash trader（Victor&Weintraud）
    wash_net_position_tol: float = 0.01 # 净头寸变化 ≤ 均值成交量的 1% 视为 wash
    wash_windows_sec: tuple[int, ...] = (60, 3600, 86400)  # memecoin 生命周期短，收紧到分钟级
    transfer_in_usd_ratio_max: float = 0.30  # transfer_in 占流入 USD > 30% → 成本不可信
    cost_coverage_min: float = 0.80     # 单仓位成本覆盖率下限
    min_trades: int = 20                # 最少交易笔数才有资格进榜


@dataclass
class SybilConfig:
    """实体聚类。不合并 sybil，则「20 个分身里最幸运的那个」会占据榜首。"""
    funding_fanout_min: int = 5         # 同一地址向 ≥5 个地址转出近似数额 → 同簇
    funding_amount_tol: float = 0.05    # 数额相对差 ≤ 5% 视为「近似」
    funding_window_sec: int = 3600
    cooccur_jaccard_min: float = 0.60   # 建仓区块集合 Jaccard
    cooccur_min_count: int = 5
    token_jaccard_min: float = 0.70     # 交易过的代币集合 Jaccard
    token_first_buy_block_gap: int = 3
    enable_profit_sink: bool = True     # 利润归集到同一地址 → 最强证据


@dataclass
class RaceConfig:
    """赛制。一场比赛 = 一个代币 × 一个时间窗，名次 = 窗口内净收益率。
    横截面比较自动剔除代币 beta —— 牛市普涨/熊市普跌不再污染排名。"""
    window_hours: int = 24
    max_windows_per_token: int = 3      # 只跑代币生命周期前 3 个窗口
    min_participants: int = 5           # 参赛者太少的比赛信息量低，丢弃
    draw_margin_ret: float = 0.05       # 收益率差 < 5% 视为平局
    min_invested_usd: float = 50.0      # 仓位太小视为噪音/女巫探针


@dataclass
class EloConfig:
    """Elo-MMR(ρ) 参数。见 docs/02 第 4 节。"""
    mu_init: float = 1500.0
    sig_init: float = 350.0
    beta: float = 200.0                 # 单场表现噪声
    gamma: float = 40.0                 # 每期扩散（不确定性增长），抗 alpha 衰减
    rho_history: int = 30               # 保留的 logistic 历史项数（ρ）
    sig_floor: float = 40.0
    solver_tol: float = 1e-9
    solver_max_iter: int = 200


@dataclass
class GateConfig:
    """统计显著性闸门。从上万地址里挑「高手」，不做多重检验校正必然全是噪音。"""
    fdr_target: float = 0.10            # BHY 目标 FDR
    dsr_min: float = 0.95               # Deflated Sharpe Ratio 门槛
    dsr_as_gate: bool = False           # ⚠️ 默认只诊断不拦截，理由见 gates.apply_gates
    min_profit_factor: float = 1.2      # 经济显著性：盈亏比下限
    min_races: int = 10                 # 参赛场次下限
    noise_floor_B: int = 200            # 置换检验标定噪音基准线的重采样次数
    pi0_lambda: float = 0.50            # Barras-Scaillet-Wermers 的 π₀ 估计阈值
    bootstrap_B: int = 2000             # cluster bootstrap 次数（按代币分块）
    bootstrap_alpha: float = 0.05
    use_emax_sr_floor: bool = True      # 低于 E[max SR] 基准线的榜首直接判为噪音


@dataclass
class CopyConfig:
    """可跟单性回测。系统最有商业价值的部分：展示「你能赚多少」而不是「他赚了多少」。"""
    entry_lag_blocks: int = 3           # 看到领头者上链后 N 块才能进场
    exit_lag_blocks: int = 3            # 出场同理 —— 结构性劣势：你是他的对手方
    position_usd: float = 500.0
    taker_fee_bps: float = 25.0         # PancakeSwap V3 默认 0.25%
    gas_usd_per_tx: float = 0.15
    token_tax_bps: float = 0.0          # fee-on-transfer，按代币覆盖
    min_pool_liquidity_usd: float = 50_000.0  # 池子太浅，滑点吃光 alpha
    max_hold_filter_sec: int = 300      # 领头者持仓 < 5min 的直接判为跟不上


@dataclass
class Config:
    data_source: str = "synthetic"      # synthetic | rpc | bitquery | dune
    lookback_days: int = 90
    start_ts: int = 0                   # 0 = 由 end_ts − lookback_days 推算
    end_ts: int = 0                     # 0 = 现在
    out_dir: str = "out"
    seed: int = 42
    purify: PurifyConfig = field(default_factory=PurifyConfig)
    sybil: SybilConfig = field(default_factory=SybilConfig)
    race: RaceConfig = field(default_factory=RaceConfig)
    elo: EloConfig = field(default_factory=EloConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        if path is None:
            return cfg
        p = Path(path)
        if not p.exists():
            return cfg
        if yaml is None:
            raise RuntimeError("需要 PyYAML 才能读取 yaml 配置：pip install pyyaml")
        raw: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
        sub = {
            "purify": PurifyConfig, "sybil": SybilConfig, "race": RaceConfig,
            "elo": EloConfig, "gate": GateConfig, "copy": CopyConfig,
        }
        for k, v in raw.items():
            if k in sub and isinstance(v, dict):
                setattr(cfg, k, sub[k](**v))
            elif hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)
