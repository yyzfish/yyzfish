"""BSC 链上常量：合约地址、事件签名、报价资产、桥/CEX 白名单。

✅ topic0 已于 2026-08-15 用真实链上日志核对（Bitquery Events 的 SignatureHash）：
pancake_v3_swap 与 infinity_cl_swap 均逐字节一致，后者直接取自
INFINITY_CL_POOL_MANAGER 发出的 Swap 事件。Infinity Bin 池的 Swap ABI 仍未拿到，
TOPIC0 里也尚无对应条目（见 docs/01 未验证项清单）。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 链参数
CHAIN_ID = 56
# Fermi 硬分叉（2026-01-14）后 BSC 出块 0.45s。所有「区块距离 → 秒」换算走这里。
BLOCK_TIME_SEC = 0.45
BLOCKS_PER_DAY = int(86400 / BLOCK_TIME_SEC)  # ≈ 192,000

# ---------------------------------------------------------------- PancakeSwap
PANCAKE_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
PANCAKE_V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"

PANCAKE_V3_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
PANCAKE_V3_ROUTER = "0x1b81d678ffb9c0263b24a97847620c99d213eb14"

# Infinity 是 singleton：Swap 事件从 PoolManager 发出，用 PoolId(bytes32) 区分池子。
# 「按 pair 地址订阅」的逻辑在这里完全失效 —— 必须订阅下面两个地址 + 维护 PoolId 映射表。
INFINITY_CL_POOL_MANAGER = "0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b"
INFINITY_BIN_POOL_MANAGER = "0xc697d2898e0d09264376196696c51d7abbbaa4a9"
INFINITY_UNIVERSAL_ROUTER = "0xd9c500dff816a1da21a48a732d3498bf09dc9aeb"

# ---------------------------------------------------------------- Four.meme
FOURMEME_EXCHANGE = "0x5c952063c7fc8610ffdb798152d69f0b9550762b"
# Binance Meme Rush 发的币合约地址以 0x4444 开头 —— 极好的低成本过滤特征
MEMERUSH_ADDR_PREFIX = "0x4444"
FOURMEME_CURVE_TOTAL_SUPPLY = 1_000_000_000
FOURMEME_CURVE_SELLABLE = 800_000_000   # initialRealTokenReserves
FOURMEME_GRADUATE_RESERVE = 200_000_000  # 毕业时注入 PancakeSwap V3

# ---------------------------------------------------------------- 事件 topic0
TOPIC0 = {
    # Swap(address,uint256,uint256,uint256,uint256,address)
    "pancake_v2_swap": "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
    # Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)
    # 注意：比 Uniswap V3 多两个 protocolFees 字段，topic0 与 Uniswap 完全不同
    "pancake_v3_swap": "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83",
    # Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)
    "infinity_cl_swap": "0x04206ad2b7c0f463bff3dd4f33c5735b0f2957a351e4f79763a4fa9e775dd237",
    # ERC20 Transfer(address,address,uint256)
    "erc20_transfer": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
}

VENUES = ("pancake_v2", "pancake_v3", "infinity_cl", "infinity_bin", "fourmeme")

# ---------------------------------------------------------------- 报价资产
# swap 定价一律用「对手方代币的实际执行价」，因此只需要给这些 quote 资产喂 USD 价。
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
USD1 = "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"
CAKE = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"

QUOTE_TOKENS = {WBNB, USDT, USDC, BUSD, USD1, CAKE}
STABLE_QUOTES = {USDT, USDC, BUSD, USD1}  # USD 价恒为 1.0，其余需喂价

# ---------------------------------------------------------------- 归因白名单
# 这些地址出现在 swap 的 sender/recipient 时不是真人 —— 必须回退到 tx.from。
ROUTER_ADDRESSES = {
    PANCAKE_V2_ROUTER,
    PANCAKE_V3_ROUTER,
    INFINITY_UNIVERSAL_ROUTER,
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # SmartRouter
}

# 桥合约：转入按「转入时市价」记成本，并打 bridge_in 标记（不是买入）
# 来源（2026-08-15 核验）：Stargate gitbook + stargate-v2 部署 JSON、LayerZero 官方
# metadata API、cbridge-docs、axelar-contract-deployments、across-protocol/contracts、
# BscScan 标签（dawsbot/eth-labels 与 brianleect/etherscan-labels 两份独立抓取交叉一致）。
BRIDGE_ADDRESSES = {
    # --- Stargate V1 ---
    "0x4a364f8c717caad9a442737eb7b8a55cc6cf18d8",  # Stargate V1: Router
    "0x6694340fc020c5e6b96567843da2df01b2ce1eb6",  # Stargate V1: Bridge
    "0xe7ec689f432f29383f217e36e680b5c855051f25",  # Stargate V1: Factory
    "0x9aa83081aa06af7208dcc7a4cb72c94d057d2cda",  # Stargate V1: USDT Pool
    "0x98a5737749490856b401db5dc27f522fc314a4e1",  # Stargate V1: BUSD Pool
    "0x4e145a589e4c03cbe3d28520e4bf3089834289df",  # Stargate V1: USDD Pool
    "0x7bfd7f2498c4796f10b6c611d9db393d3052510c",  # Stargate V1: MAI Pool
    "0x68c6c27fb0e02285829e69240be16f32c5f8befe",  # Stargate V1: metis.USDT Pool
    # --- Stargate V2 ---
    "0x6e3d884c96d640526f273c61dfcf08915ebd7e2b",  # Stargate V2: TokenMessaging
    "0x138eb30f73bc423c6455c53df6d89cb01d9ebc63",  # Stargate V2: StargatePoolUSDT
    "0x962bd449e630b0d928f308ce63f1a21f02576057",  # Stargate V2: StargatePoolUSDC
    "0x25bbf59ef9246dc65bfac8385d55c5e524a7b9ea",  # Stargate V2: CreditMessaging
    # --- LayerZero ---
    "0x3c2269811836af69497e5f486a85d7316753cf62",  # LayerZero V1: Endpoint
    "0x4d73adb72bc3dd368966edd0f0b2148401a178e2",  # LayerZero V1: UltraLightNodeV2
    "0xa27a2ca24dd28ce14fb5f5844b59851f03dcf182",  # LayerZero V1: RelayerV2
    "0x1a44076050125825900e736c501f859c50fe728c",  # LayerZero V2: EndpointV2
    "0x9f8c645f2d0b2159767bd6e0839de4be49e823de",  # LayerZero V2: SendUln302
    "0xb217266c3a98c8b2709ee26836c98cf12f6ccec1",  # LayerZero V2: ReceiveUln302
    "0x3ebd570ed38b1b3b4bc886999fcf507e9d584859",  # LayerZero V2: Executor
    "0x821a99c061c00f6c9da0302aaec348945ba40284",  # LayerZero V2: LzExecutor
    # --- Celer cBridge ---
    "0xdd90e5e87a2081dcf0391920868ebc2ffb81a1af",  # Celer: cBridge 流动性池桥
    "0x78bc5ee9f11d133a08b331c2e18fe81be0ed02dc",  # Celer: OriginalTokenVault
    "0x11a0c9270d88c99e221360bca50c2f6fda44a980",  # Celer: OriginalTokenVault V2
    "0xd443fe6bf23a4c9b78312391a30ff881a097580e",  # Celer: PeggedTokenBridge
    "0x26c76f7fef00e02a5dd4b5cc8a0f717eb61e1e4b",  # Celer: PeggedTokenBridge V2
    "0x3d85b598b734a0e7c8c1b62b00e972e9265da541",  # Celer: Transfer Agent
    "0x5d96d4287d1ff115ee50fac0526cf43ecf79bfc6",  # Celer: cBridge 2.0（BscScan 标签）
    # --- Binance Bridge / BSC 系统合约 ---
    "0x0000000000000000000000000000000000001004",  # BSC: Token Hub（原生跨链到账）
    # --- Wormhole / Portal ---
    "0xb6f6d86a8f9879a9c87f643768d9efc38c1da6e7",  # Wormhole: Token Bridge / Portal
    "0x98f3c9e6e3face36baad05fe09d375ef1464288b",  # Wormhole: Core Bridge
    "0x5a58505a96d1dbf8df91cb21b54419fc36e93fde",  # Wormhole: NFT Bridge
    # --- Multichain（2023 起停摆，留作历史转入归因）---
    "0x92c079d3155c2722dbf7e65017a5baf9cd15561c",  # Multichain: Bridge
    "0xd1c5966f9f5ee6881ff6b261bbeda45972b1b5f3",  # Multichain: Router V4
    "0xabd380327fe66724ffda91a87c772fb8d00be488",  # Multichain: Router V4 2
    "0xe1d592c3322f1f714ca11f05b6bc0efef1907859",  # Multichain: Router V6
    "0x400b971099e0ebfda2c03a3063739cb5398734a6",  # Multichain: Router V7
    # --- Axelar ---
    "0x304acf330bbe08d1e512eefaa92f6a57871fd895",  # Axelar: Gateway
    "0xb5fb4be02232b1bba4dc8f81dc24c26980de9e3c",  # Axelar: InterchainTokenService
    # --- Across ---
    "0x4e8e101924ede233c13e2d8622dc8aed2872d505",  # Across: SpokePool
}

# CEX 提币地址：转入按市价记成本，同时给该实体打 has_offchain_leg
# 来源：BscScan exchange 标签，dawsbot/eth-labels 与 brianleect/etherscan-labels
# 两份独立抓取完全一致；OKX/Bybit 另经 BscScan 地址页标题逐一核验。
# Bitget 无法核验到 BSC 标签地址，宁缺毋滥未收录。
CEX_HOT_WALLETS = {
    # --- Binance ---
    "0x631fc1ea2270e98fbd9d92658ece0f5a269aa161",  # Binance: Hot Wallet
    "0xb1256d6b31e4ae87da1d56e5890c66be7f1c038e",  # Binance: Hot Wallet 2
    "0x17b692ae403a8ff3a3b2ed7676cf194310dde9af",  # Binance: Hot Wallet 3
    "0x8ff804cc2143451f454779a40de386f913dcff20",  # Binance: Hot Wallet 4
    "0xad9ffffd4573b642959d3b854027735579555cbc",  # Binance: Hot Wallet 5
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3",  # Binance: Hot Wallet 6
    "0xe2fc31f816a9b94326492132018c3aecc4a93ae1",  # Binance: Hot Wallet 7
    "0x3c783c21a0383057d128bae431894a5c19f9cf06",  # Binance: Hot Wallet 8
    "0xdccf3b77da55107280bd850ea519df3705d1a75a",  # Binance: Hot Wallet 9
    "0xeb2d2f1b8c558a40207669291fda468e50c8a0bb",  # Binance: Hot Wallet 10
    "0x01c952174c24e1210d26961d456a77a39e1f0bb0",  # Binance: Hot Wallet 10（两份抓取编号漂移，均归因 Binance）
    "0x161ba15a5f335c9f06bb5bbb0a9ce14076fbb645",  # Binance: Hot Wallet 11
    "0x515b72ed8a97f42c568d6a143232775018f133c8",  # Binance: Hot Wallet 12
    "0xbd612a3f30dca67bf60a39fd0d35e39b7ab80774",  # Binance: Hot Wallet 13
    "0x7a8a34db9acd10c3b6277473b192fe47192569ca",  # Binance: Hot Wallet 14
    "0xa180fe01b906a1be37be6c534a3300785b20d947",  # Binance: Hot Wallet 16
    "0x29bdfbf7d27462a2d115748ace2bd71a2646946c",  # Binance: Hot Wallet 17
    "0x73f5ebe90f27b46ea12e5795d16c4b408b19cc6f",  # Binance: Hot Wallet 18
    "0x1fbe2acee135d991592f167ac371f3dd893a508b",  # Binance: Hot Wallet 19
    "0xf977814e90da44bfa03b6295a0616a897441acec",  # Binance: Hot Wallet 20
    # --- OKX ---
    "0x7c0629bbbaf7d68ffaa393e3fedc9b633679fa5f",  # OKX: Hot Wallet
    "0x559432e18b281731c054cd703d4b49872be4ed53",  # OKX: Hot Wallet 5
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",  # OKX（BSC 主交易所钱包）
    "0xa7efae728d2936e78bda97dc267687568dd593f3",  # OKX 3
    "0x3b5a23f6207d87b423c6789d2625ea620423b32d",  # OKX 35
    # --- Bybit ---
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40",  # Bybit: Hot Wallet
    "0xee5b5b923ffce93a870b3104b7ca09c3db80047a",  # Bybit: Hot Wallet 4
    # --- Gate.io / KuCoin / MEXC ---
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe",  # Gate.io
    "0x53f78a071d04224b8e254e243fffc6d9f2f3fa23",  # KuCoin: Hot Wallet 2
    "0x4982085c9e2f89f2ecb8131eca71afad896e89cb",  # MEXC
    "0x0211f3cedbef3143223d3acf0e589747933e8527",  # MEXC 2
    "0x2e8f79ad740de90dc5f5a9f0d8d9661a60725e64",  # MEXC 3
}

# 已知 MEV builder / bribe 收款方：出现直接转账即为 MEV 参与者的强证据
# 来源：bnb-chain 官方 builder 注册表 good-will-alliance/mev-info/bsc-mainnet/
# builder-list.toml（现行）+ bsc-mev-info/mainnet/builder-list.toml（已弃用的旧表，
# 标注「旧表」的条目仅见于其中；两表重叠部分地址逐字节一致）。
MEV_BUILDERS = {
    # --- 48Club Puissant ---
    "0x48a5ed9abc1a8fbe86cec4900483f43a7f2dbb48",  # 48Club builder (ap)
    "0x487e5dfe70119c1b320b8219b190a6fa95a5bb48",  # 48Club builder (eu)
    "0x48fee1bb3823d72fdf80671ebad5646ae397bb48",  # 48Club builder (us)
    "0x48b4bbebf0655557a461e91b8905b85864b8bb48",  # 48Club builder (x)
    "0x4827b423d03a349b7519dda537e9a28d31ecbb48",  # 48Club builder (y)
    "0x48b2665e5e9a343409199d70f7495c8ab660bb48",  # 48Club builder (z)
    # --- bloXroute ---
    "0xd4376fdc9b49d90e6526daa929f2766a33bffd52",  # bloXroute builder (dublin)
    "0x2873fc7ad9122933becb384f5856f0e87918388d",  # bloXroute builder (frankfurt)
    "0x432101856a330aafdeb049dd5fa03a756b3f1c66",  # bloXroute builder (japan)
    "0x2b217a4158933aade6d6494e3791d454b4d13ae7",  # bloXroute builder (nyc)
    "0xe1ec1aece7953ecb4539749b9aa2eef63354860a",  # bloXroute builder (singapore)
    "0x89434fc3a09e583f2cb4e47a8b8fe58de8be6a15",  # bloXroute builder (virginia)
    "0x0da52e9673529b6e06f444fbbed2904a37f66415",  # bloXroute builder (relay，旧表)
    "0x10353562e662e333c0c2007400284e0e21cf74ff",  # bloXroute builder (x，旧表)
    # --- BlockRazor ---
    "0x5532cdb3c0c4278f9848fc4560b495b70ba67455",  # BlockRazor builder (dublin)
    "0xba4233f6e478db76698b0a5000972af0196b7be1",  # BlockRazor builder (frankfurt)
    "0x539e24781f616f0d912b60813ab75b7b80b75c53",  # BlockRazor builder (nyc)
    "0x49d91b1ab0cc6a1591c2e5863e602d7159d36149",  # BlockRazor builder (relay)
    "0x50061047b9c7150f0dc105f79588d1b07d2be250",  # BlockRazor builder (tokyo)
    "0x0557e8cb169f90f6ef421a54e29d7dd0629ca597",  # BlockRazor builder (virginia)
    "0x488e37fcb2024a5b2f4342c7de636f0825de6448",  # BlockRazor builder (x)
    # --- 其余注册 builder ---
    "0x36cb523286d57680efbbfb417c63653115bcebb5",  # JetBuilder (ap)
    "0x3ad6121407f6edb65c8b2a518515d45863c206a8",  # JetBuilder (eu)
    "0x345324dc15f1cdcf9022e3b7f349e911fb823b4c",  # JetBuilder (us)
    "0xfd38358475078f81a45077f6e59dff8286e0dca1",  # JetBuilder (dublin)
    "0x7f5fbfd8e2eb3160df4c96757deef29e26f969a3",  # JetBuilder (tokyo)
    "0xa0cde9891c6966fce740817cc5576de2c669ab43",  # JetBuilder (virginia)
    "0x79102db16781dddff63f301c9be557fd1dd48fa0",  # NodeReal builder (ap-1)
    "0x5b526b45e833704d84b5c2eb0f41323da9466c48",  # NodeReal builder (ap-2)
    "0xd0d56b330a0dea077208b96910ce452fd77e1b6f",  # NodeReal builder (eu-1)
    "0xa547f87b2bade689a404544859314cbc01f2605e",  # NodeReal builder (eu-2)
    "0x4f24ce4cd03a6503de97cf139af2c26347930b99",  # NodeReal builder (us-1)
    "0xfd3f1ad459d585c50cf4630649817c6e0cec7335",  # NodeReal builder (us-2)
    "0x9c6b0870752cdd1b3f9aac28c0207e8126f8e1b8",  # Flashblock builder (us)
    "0x89b08890751b28511541f5fed08d7d964caae911",  # Flashblock builder (eu)
    "0x3fc0c936c00908c07723ffbf2d536d6e0f62c3a4",  # BlockBus builder (dublin，旧表)
    "0x17e9f0d7e45a500f0148b29c6c98efd19d95f138",  # BlockBus builder (tokyo，旧表)
    "0x1319be8b8ec4aa81f501924bdcf365fbcaa8d753",  # BlockBus builder (virginia，旧表)
    "0x6dddf681c908705472d09b1d7036b2241b50e5c7",  # Blocksmith builder (ap，旧表)
    "0x76736159984ae865a9b9cc0df61484a49da68191",  # Blocksmith builder (eu，旧表)
    "0x5054b21d8baea3d602dca8761b235ee10bc0231e",  # Blocksmith builder (us，旧表)
    "0xa6d6086222812efd5292ff284b0f7ff2a2b86af4",  # DarwinBuilder (ap，旧表)
    "0x3265a3243ee84e667a73073504ca4cded1413d82",  # DarwinBuilder (eu，旧表)
    "0xdf11cd23992fd48cf2d245ac144010673275f285",  # DarwinBuilder (us，旧表)
    "0x9a3234b450518fada098388b88e00decad96ad38",  # inBlock builder (ap，旧表)
    "0xb49f86586a840ab9920d2f340a85586e50fd30a2",  # inBlock builder (eu，旧表)
    "0x0f6d8b72f3687de6f2824903a83b3ba13c0e88a0",  # inBlock builder (us，旧表)
    "0x812720cb4639550d7bdb1d8f2be463f4a9663762",  # XZBuilder（旧表）
    "0x2d3cc0a25a05e6eb3d5d3ea21d72c8d71b436a7f",  # TrustNet TEE builder（旧表）
}

# NFT 市场协议：Bitquery 的 DEXTrades 会混入这些成交（实测 2026-08-15 出现
# seaport_v1.4）。一侧是 WBNB 的 NFT 成交会被误判成 memecoin 买卖污染 PnL，
# 摄入时按 ProtocolName 前缀丢弃。
NFT_MARKETPLACE_PROTOCOLS = ("seaport", "opensea", "blur", "looksrare", "element")
