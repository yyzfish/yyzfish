"""适配器字段映射测试 —— 不需要 API key。

作用：把「归一化逻辑」和「真实网络」解耦。fixture 是按官方文档写的形状样本，
上线时先跑 `smrace.cli probe` 用真数据核对一次，然后把真实响应存回 fixture，
之后所有重构都由这些测试兜底。

三条最容易出错、也最难在生产中察觉的规则，全部在这里锁死：
  1. wallet 必须取 tx.From，不能取 Buyer/Seller（V3/Infinity 上那是 Router）
  2. 两边都是报价资产的套利腿必须跳过
  3. quote_usd 用本笔成交的 AmountInUSD 反推，不是外部 K 线
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smrace.constants import PANCAKE_V3_ROUTER, WBNB
from smrace.ingest.bitquery import BitquerySource
from smrace.ingest.cache import dump_jsonl, load_jsonl
from smrace.ingest.dune import DuneSource
from smrace.ingest.loader import (marks_from_trades, price_fn_from_trades,
                                  tokens_from_trades)
from smrace.ingest.prices import PriceOracle
from smrace.models import Side, Trade

FIX = Path(__file__).parent / "fixtures"
MEME = "0xmeme00000000000000000000000000000000beef"
USER1 = "0x1111111111111111111111111111111111111111"
USER2 = "0x2222222222222222222222222222222222222222"


def _oracle() -> PriceOracle:
    o = PriceOracle()
    o.fallback[WBNB] = 600.0
    return o


def _bq() -> BitquerySource:
    return BitquerySource(token="TEST", oracle=_oracle(), verbose=False,
                          gas_price_unit="gwei")


def _dune() -> DuneSource:
    return DuneSource(api_key="TEST", oracle=_oracle(), verbose=False)


def _bq_rows():
    d = json.loads((FIX / "bitquery_dextrades.json").read_text())
    return d["data"]["EVM"]["DEXTrades"]


def _dune_rows():
    d = json.loads((FIX / "dune_dex_trades.json").read_text())
    return d["result"]["rows"]


# ---------------------------------------------------------------- Bitquery
def test_bitquery_maps_buy_side():
    t = _bq()._map_trade(_bq_rows()[0])
    assert t is not None
    assert t.side is Side.BUY
    assert t.token == MEME and t.quote == WBNB
    assert abs(t.base_amount - 1_000_000) < 1e-6
    assert abs(t.usd - 500.0) < 0.1, t.usd
    assert t.block == 50_000_000 and t.log_index == 12


def test_bitquery_wallet_is_tx_from_not_router():
    """★ 最关键的一条。Buyer 是 PancakeSwap SmartRouter，绝不能当成真人钱包 ——
    搞错了会把成千上万个用户全归成一个地址，整个框架直接失效。"""
    row = _bq_rows()[0]
    assert row["Trade"]["Buy"]["Buyer"].lower() == PANCAKE_V3_ROUTER
    t = _bq()._map_trade(row)
    assert t.wallet == USER1, f"wallet 应取 tx.From，实际 {t.wallet}"


def test_bitquery_maps_sell_side_with_stable_quote():
    t = _bq()._map_trade(_bq_rows()[1])
    assert t.side is Side.SELL
    assert t.token == MEME
    assert abs(t.quote_usd - 1.0) < 1e-9, "稳定币的 USD 单价必须恒为 1"
    assert abs(t.usd - 1200.0) < 1e-6
    assert t.wallet == USER2


def test_bitquery_skips_quote_to_quote_arbitrage_leg():
    assert _bq()._map_trade(_bq_rows()[2]) is None


def test_bitquery_gas_unit_is_sane():
    """gas 单位错一个数量级是最隐蔽的 bug：净 PnL 会系统性偏，
    而且只在高频地址上体现出来。BSC 单笔典型 $0.05–0.3。"""
    t = _bq()._map_trade(_bq_rows()[0])
    assert 0.005 <= t.gas_usd <= 2.0, f"gas_usd={t.gas_usd} 不在合理区间"
    wrong = BitquerySource(token="T", oracle=_oracle(), verbose=False,
                           gas_price_unit="wei")._map_trade(_bq_rows()[0])
    assert wrong.gas_usd < 1e-6, "单位设成 wei 时应该明显偏小 —— 这正是探针要抓的错"


def test_bitquery_quote_usd_derived_from_trade_not_oracle():
    """quote_usd 应由本笔成交的 AmountInUSD 反推（与成交同源），
    而不是回退到 oracle 的 600 —— 前者天然含滑点，后者不含。"""
    t = _bq()._map_trade(_bq_rows()[0])
    assert abs(t.quote_usd - 500.0 / 0.8333333) < 1e-3


# ---------------------------------------------------------------- Dune
def test_dune_maps_buy_and_sell():
    d = _dune()
    a, b = d._map_row(_dune_rows()[0]), d._map_row(_dune_rows()[1])
    assert a.side is Side.BUY and a.token == MEME and a.wallet == USER1
    assert b.side is Side.SELL and b.token == MEME and b.wallet == USER2
    assert abs(a.usd - 500.0) < 0.1 and abs(b.usd - 1200.0) < 1e-6


def test_dune_skips_quote_to_quote():
    assert _dune()._map_row(_dune_rows()[2]) is None


def test_dune_gas_price_is_wei():
    """bnb.transactions.gas_price 是 wei：180000 × 1e9 / 1e18 × $600 = $0.108"""
    t = _dune()._map_row(_dune_rows()[0])
    assert abs(t.gas_usd - 0.108) < 1e-3, t.gas_usd


def _bare(**kw):
    s = DuneSource.__new__(DuneSource)
    s.with_gas = kw.get("with_gas", True)
    s.scoped = kw.get("scoped", True)
    s.min_usd = kw.get("min_usd", 10.0)
    s.min_traders = kw.get("min_traders", 5)
    s.max_tokens = kw.get("max_tokens", 2000)
    s.buffer_days = kw.get("buffer_days", 7)
    return s


def test_dune_sql_renders_with_and_without_gas():
    with_gas = DuneSource.render_sql(_bare(with_gas=True), 1, 2)
    without = DuneSource.render_sql(_bare(with_gas=False), 1, 2)
    assert "bnb.transactions" in with_gas and "bnb.transactions" not in without
    assert "blockchain = 'bnb'" in without


def test_scoped_sql_applies_all_three_filters():
    """窄范围版必须同时收敛：新币、参与地址数、Top-N。少一条都省不下来。"""
    s = DuneSource.render_sql(_bare(min_traders=7, max_tokens=123, buffer_days=3), 1, 2)
    assert "older" in s and "INTERVAL '3' DAY" in s, "缺少「排除老币」"
    assert "COUNT(DISTINCT w) >= 7" in s, "缺少参与地址数下限"
    assert "rn <= 123" in s, "缺少 Top-N 封顶"
    full = DuneSource.render_sql(_bare(scoped=False), 1, 2)
    assert "older" not in full


def test_estimate_sql_reports_truncation():
    """截断必须能被量化报出 —— 静默截断会让人误以为覆盖了全部。"""
    s = DuneSource.render_estimate_sql(_bare(max_tokens=500), 1, 2)
    for col in ("rows_full", "rows_eligible", "rows_scoped",
                "tokens_truncated", "rows_truncated"):
        assert col in s, f"估算查询缺少 {col}"


def test_estimate_cost_tiers_are_ordered_and_free_quota_checked():
    from smrace.ingest.dune import estimate_cost
    c = estimate_cost(250_000, bytes_per_row=420)
    assert c["free"]["credits"] > c["analyst"]["credits"] > c["plus"]["credits"]
    # Free 档超额单价最贵：同样的量比 Analyst 贵 2 倍以上
    assert c["free"]["usd_if_overage"] > 2 * c["analyst"]["usd_if_overage"]
    assert c["free"]["fits_in_free_quota"] is True
    assert estimate_cost(2_000_000)["free"]["fits_in_free_quota"] is False


def test_estimate_cost_scales_linearly():
    from smrace.ingest.dune import estimate_cost
    a, b = estimate_cost(100_000), estimate_cost(200_000)
    assert abs(b["free"]["credits"] / a["free"]["credits"] - 2.0) < 1e-9


# ---------------------------------------------------------------- 喂价
def test_price_oracle_stables_and_timeseries():
    o = PriceOracle()
    from smrace.constants import USDT
    assert o.price(USDT, 0) == 1.0
    o.load_series(WBNB, [(100, 500.0), (200, 700.0)])
    assert o.price(WBNB, 50) == 500.0    # 早于起点用第一个点
    assert o.price(WBNB, 150) == 500.0   # 阶梯：取 ≤ ts 的最后一点
    assert o.price(WBNB, 999) == 700.0


def test_price_oracle_strict_raises_on_missing():
    o = PriceOracle(strict=True)
    try:
        o.price("0xdead", 1)
    except KeyError:
        return
    raise AssertionError("strict 模式下缺价必须报错，不能静默返回 0")


# ---------------------------------------------------------------- loader 辅助
def _mk(block, token, base, quote_amt, ts=None):
    return Trade(ts=ts or block, block=block, tx_hash="0x", log_index=0,
                 wallet="w", token=token, quote=WBNB, side=Side.BUY,
                 base_amount=base, quote_amount=quote_amt, quote_usd=1.0)


def test_price_fn_is_step_function_of_last_trade():
    trades = [_mk(10, "T", 100, 100), _mk(20, "T", 100, 200)]
    fn = price_fn_from_trades(trades)
    assert abs(fn("T", 5) - 1.0) < 1e-9      # 早于第一笔 → 用第一笔
    assert abs(fn("T", 15) - 1.0) < 1e-9     # 区间内 → 上一笔
    assert abs(fn("T", 25) - 2.0) < 1e-9     # 第二笔之后
    assert fn("UNKNOWN", 1) == 0.0


def test_tokens_and_marks_from_trades():
    trades = [_mk(30, "T", 100, 300), _mk(10, "T", 100, 100)]
    assert tokens_from_trades(trades)["T"].launch_block == 10
    assert abs(marks_from_trades(trades)["T"] - 3.0) < 1e-9


# ---------------------------------------------------------------- 缓存
def test_cache_roundtrip_preserves_enum(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    p = d / "x.jsonl.gz"
    orig = [_mk(1, "T", 5, 5)]
    assert dump_jsonl(p, orig) == 1
    back = list(load_jsonl(p, Trade))
    assert len(back) == 1
    assert back[0].block == 1 and back[0].token == "T"
    assert Side(back[0].side) is Side.BUY, "枚举必须能从字符串还原"


# ---------------------------------------------------------------- 转账流
def _flow(wallet, cp, amount=100.0, tx="0xf1", kind=None):
    from smrace.models import Flow, FlowKind
    return Flow(ts=1, block=1, tx_hash=tx, wallet=wallet, token="T",
                amount=amount, kind=kind or FlowKind.UNKNOWN,
                mark_usd=1.0, counterparty=cp)


def test_classify_drops_swap_and_router_legs():
    """★ swap 腿必须**丢弃**而不是标成中性 —— 它已经在 Trade 里算过一次了。"""
    from smrace.constants import PANCAKE_V2_ROUTER
    from smrace.normalize.flows import classify_flow
    assert classify_flow("0xpool", "0xu", dex_pools={"0xpool"}) is None
    assert classify_flow(PANCAKE_V2_ROUTER, "0xu") is None
    assert classify_flow("0xu", "0x000000000000000000000000000000000000dead") is None


def test_classify_mint_is_airdrop_not_zero_cost():
    from smrace.models import FlowKind
    from smrace.normalize.flows import classify_flow
    z = "0x" + "0" * 40
    assert classify_flow(z, "0xu") is FlowKind.AIRDROP
    assert classify_flow("0xaaa", "0xbbb") is FlowKind.UNKNOWN


def test_drop_swap_legs_removes_same_tx():
    """同一笔 tx 里的 transfer 必须去掉，否则同一笔买入被记两次 ——
    这是接转账流时最容易漏、后果最严重的一步。"""
    from smrace.normalize.flows import drop_swap_legs
    tr = [_mk(1, "T", 10, 10)]
    tr[0].tx_hash = "0xswap"
    flows = [_flow("0xu", "0xp", tx="0xswap"), _flow("0xu", "0xp", tx="0xother")]
    kept = drop_swap_legs(flows, tr)
    assert len(kept) == 1 and kept[0].tx_hash == "0xother"


def test_label_internal_only_after_clustering():
    """簇内转账必须净额抵消 —— 不回标的话，操盘手在自己地址间倒仓
    每倒一次就凭空产生一次盈利。"""
    from smrace.models import FlowKind
    from smrace.normalize.flows import label_internal
    flows = [_flow("0xa", "0xb"), _flow("0xa", "0xc")]
    n = label_internal(flows, {"0xa": "E1", "0xb": "E1", "0xc": "E2"})
    assert n == 1
    assert flows[0].kind is FlowKind.INTERNAL
    assert flows[1].kind is not FlowKind.INTERNAL


def test_internal_flow_is_pnl_neutral_end_to_end():
    """回标之后，内部转账不能产生任何 realized PnL。"""
    from smrace.models import FlowKind
    from smrace.pnl.engine import compute_positions
    out = compute_positions([], [_flow("0xa", "0xb", amount=-500.0,
                                       kind=FlowKind.INTERNAL)])
    assert all(abs(p.realized_pnl) < 1e-9 for p in out.values())


def test_bitquery_map_flow_uses_receiver_as_wallet():
    row = {
        "Block": {"Number": "1", "Time": "2026-08-01T00:00:00Z"},
        "Transaction": {"Hash": "0xt1"},
        "Transfer": {"Amount": "1000", "Sender": "0x" + "0" * 40,
                     "Receiver": "0xdead0000000000000000000000000000000000aa",
                     "Currency": {"SmartContract": MEME, "Symbol": "MEME"}},
    }
    from smrace.models import FlowKind
    f = _bq()._map_flow(row, dex_pools=set(), marks={MEME: 0.002})
    assert f is not None
    assert f.wallet == "0xdead0000000000000000000000000000000000aa"
    assert f.counterparty == "0x" + "0" * 40
    assert f.kind is FlowKind.AIRDROP
    assert abs(f.mark_usd - 0.002) < 1e-12, "成本必须按转入时市价，不是零成本"


def test_dune_flows_sql_excludes_swap_tx():
    from smrace.ingest.dune import FLOWS_SQL
    s = FLOWS_SQL.format(start_ts=1, end_ts=2)
    assert "NOT IN (SELECT tx_hash FROM swap_tx)" in s, "缺少 swap 腿排除"


def _run(fn):
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {fn.__name__}: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"运行 {len(fns)} 个适配器测试：")
    ok = sum(_run(f) for f in fns)
    print(f"{ok}/{len(fns)} 通过")
    sys.exit(0 if ok == len(fns) else 1)
