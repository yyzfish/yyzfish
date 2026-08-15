"""日志解码：PancakeSwap V2 / V3 / Infinity + Four.meme。

三个必须处理的架构差异（照抄 Uniswap 的解析器会全线出错）：

1. **Pancake V3 ≠ Uniswap V3**。Swap 事件多了 protocolFeesToken0/1 两个字段，
   topic0 完全不同；费率档也不同（Pancake 0.25% / tickSpacing 50）。

2. **Infinity 是 singleton**。Swap 从 CLPoolManager / BinPoolManager 单一合约
   发出，用 PoolId(bytes32) 区分池子 —— 「按 pair 地址订阅」的逻辑完全失效。
   必须先用 Initialize 事件建立 PoolId → (currency0, currency1, fee, hooks) 映射。

3. **sender/recipient 常常是 Router，不是真人**。归因必须回退到 tx.from，
   或解析 UniversalRouter 的 calldata。这是聪明钱归因的核心难点 ——
   搞错了会把成千上万个用户全归成一个「Router 地址」。

另外：base_amount 必须取 **ERC20 Transfer 事件里的实际到账量**，
不是 Swap 事件的 amountOut —— BSC 上大量 memecoin 有 3~10% 的 fee-on-transfer。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..constants import QUOTE_TOKENS, ROUTER_ADDRESSES, TOPIC0


@dataclass
class RawLog:
    address: str
    topics: list[str]
    data: str
    block_number: int
    log_index: int
    tx_hash: str
    tx_from: str


def _i256(word: str) -> int:
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def _i128(word: str) -> int:
    v = int(word, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >= (1 << 127) else v


def _words(data: str) -> list[str]:
    h = data[2:] if data.startswith("0x") else data
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def attribute_wallet(log: RawLog, candidate: str) -> str:
    """归因真人钱包：候选地址是 Router 则回退到 tx.from。"""
    return log.tx_from.lower() if candidate.lower() in ROUTER_ADDRESSES else candidate.lower()


def decode_v2_swap(log: RawLog, pool_tokens: Mapping[str, tuple[str, str]]) -> dict[str, Any] | None:
    """Swap(address sender, uint a0In, uint a1In, uint a0Out, uint a1Out, address to)"""
    if log.topics[0].lower() != TOPIC0["pancake_v2_swap"]:
        return None
    w = _words(log.data)
    if len(w) < 4:
        return None
    a0in, a1in, a0out, a1out = (int(x, 16) for x in w[:4])
    pair = pool_tokens.get(log.address.lower())
    if pair is None:
        return None
    t0, t1 = pair
    to = _addr_from_topic(log.topics[2]) if len(log.topics) > 2 else log.tx_from
    return {
        "venue": "pancake_v2", "pool": log.address.lower(),
        "token0": t0, "token1": t1,
        "amount0": a0in - a0out, "amount1": a1in - a1out,   # 正 = 流入池子
        "wallet": attribute_wallet(log, to),
    }


def decode_v3_swap(log: RawLog, pool_tokens: Mapping[str, tuple[str, str]]) -> dict[str, Any] | None:
    """Swap(address sender, address recipient, int256 a0, int256 a1, uint160 sqrtP,
            uint128 liq, int24 tick, uint128 pFee0, uint128 pFee1)"""
    if log.topics[0].lower() != TOPIC0["pancake_v3_swap"]:
        return None
    w = _words(log.data)
    if len(w) < 7:
        return None
    pair = pool_tokens.get(log.address.lower())
    if pair is None:
        return None
    t0, t1 = pair
    recipient = _addr_from_topic(log.topics[2]) if len(log.topics) > 2 else log.tx_from
    return {
        "venue": "pancake_v3", "pool": log.address.lower(),
        "token0": t0, "token1": t1,
        "amount0": _i256(w[0]), "amount1": _i256(w[1]),
        "sqrt_price_x96": int(w[2], 16), "liquidity": int(w[3], 16),
        "wallet": attribute_wallet(log, recipient),
    }


def decode_infinity_cl_swap(
    log: RawLog, pool_registry: Mapping[str, tuple[str, str]]
) -> dict[str, Any] | None:
    """Swap(bytes32 id, address sender, int128 a0, int128 a1, uint160 sqrtP,
            uint128 liq, int24 tick, uint24 fee, uint16 protocolFee)

    pool_registry: PoolId(hex) -> (currency0, currency1)，由 Initialize 事件维护。
    """
    if log.topics[0].lower() != TOPIC0["infinity_cl_swap"]:
        return None
    pool_id = log.topics[1].lower() if len(log.topics) > 1 else ""
    pair = pool_registry.get(pool_id)
    if pair is None:
        return None          # PoolId 未注册 —— 说明 Initialize 事件回填不完整
    t0, t1 = pair
    w = _words(log.data)
    if len(w) < 5:
        return None
    sender = _addr_from_topic(log.topics[2]) if len(log.topics) > 2 else log.tx_from
    return {
        "venue": "infinity_cl", "pool": pool_id,
        "token0": t0, "token1": t1,
        "amount0": _i128(w[0]), "amount1": _i128(w[1]),
        # Infinity 的 sender 几乎总是 UniversalRouter —— 必须回退 tx.from
        "wallet": attribute_wallet(log, sender),
    }


def to_base_quote(decoded: dict[str, Any]) -> dict[str, Any] | None:
    """把 (token0, amount0, token1, amount1) 归一化为 base/quote 视角。

    base = 被交易的 memecoin，quote = WBNB/USDT/... 两边都是报价资产时跳过
    （那是套利腿，不是选币行为）。
    """
    t0, t1 = decoded["token0"], decoded["token1"]
    a0, a1 = decoded["amount0"], decoded["amount1"]
    q0, q1 = t0 in QUOTE_TOKENS, t1 in QUOTE_TOKENS
    if q0 == q1:
        return None
    if q1:
        base, quote, ab, aq = t0, t1, a0, a1
    else:
        base, quote, ab, aq = t1, t0, a1, a0
    # ab > 0 表示 base 流入池子 ⇒ 用户在卖出 base
    side = "sell" if ab > 0 else "buy"
    return {**decoded, "base": base, "quote": quote,
            "base_raw": abs(ab), "quote_raw": abs(aq), "side": side}


FOURMEME_EVENTS = ("TokenCreate", "TokenPurchase", "TokenSale", "LiquidityAdded",
                   "PairCreated", "PoolCreated")


def fourmeme_curve_progress(left_tokens: float,
                            initial_real_reserves: float = 800_000_000) -> float:
    """BondingCurveProgress = 100 − (leftTokens × 100 / initialRealTokenReserves)

    ⚠️ Four.meme 曲线阶段的交易走自定义 exchange 合约，**不会出现在 dex.trades
    或任何标准 DEX 索引里**。想覆盖「从发射到毕业」的完整生命周期必须单独解析
    Four.meme 事件 —— 这是聪明钱系统最容易漏的一段，而恰恰是 alpha 最集中的一段。
    另注意 TokenManager 有 V1/V2 两代合约，只索引 V2 会丢失早期代币历史。
    """
    if initial_real_reserves <= 0:
        return 0.0
    return 100.0 - (left_tokens * 100.0 / initial_real_reserves)
