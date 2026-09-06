"""Zhihu web API `x-zse-96` header (v3, the `2.0_` prefix).

Port of the reference implementation in `earss_source_zhihu` (`zse96.ex`),
which itself ports RSSHub's `lib/routes/zhihu/execlib/x-zse-96-v3.ts`. The
deterministic vectors in `test_zse96.py` were generated upstream with a fixed
random byte, so this port is pinned against the real browser behaviour, not
merely self-consistent.

Algorithm:

    xzse93  = "101_3_3.0"
    payload = xzse93 + "+" + api_path + "+" + d_c0
    md5     = md5_hex(payload)              # 32 lowercase hex chars
    x-zse-96 = "2.0_" + encrypt(md5)

`encrypt` is an SM4-like transform followed by a custom base64 alphabet. The
original is JavaScript, so the 32-bit semantics are load-bearing: every word
operation masks to 32 bits, `>>>` is unsigned, and intermediates are kept
unsigned because every consumer re-masks before use (the ToInt32 round-trips
in the reference implementation are no-ops under that rule).

Pure by design: no clock, no network, fully deterministic given `rand_byte` —
which is what makes the test vectors possible and what makes a signature
reproducible for debugging a rejected request.
"""

from __future__ import annotations

import hashlib
import secrets

XZSE93 = "101_3_3.0"

# The custom base64 alphabet Zhihu's script uses for the encrypted digest.
SALT = "6fpLRqJO8M/c3jnYxFkUVC4ZIG12SiH=5v0mXDazWBTsuw7QetbKdoPyAl+hN9rgE"

# Fixed 16-byte pad XORed into the front block, from the original script.
FIX_ARR = [48, 53, 57, 48, 53, 51, 102, 55, 100, 49, 53, 101, 48, 49, 100, 55]

# SM4-like round constants (signed 32-bit as they appear in the JS source).
_ZK = (
    1_170_614_578,
    1_024_848_638,
    1_413_669_199,
    -343_334_464,
    -766_094_290,
    -1_373_058_082,
    -143_119_608,
    -297_228_157,
    1_933_479_194,
    -971_186_181,
    -406_453_910,
    460_404_854,
    -547_427_574,
    -1_891_326_262,
    -1_679_095_901,
    2_119_585_428,
    -2_029_270_069,
    2_035_090_028,
    -1_521_520_070,
    -5_587_175,
    -77_751_101,
    -2_094_365_853,
    -1_243_052_806,
    1_579_901_135,
    1_321_810_770,
    456_816_404,
    -1_391_643_889,
    -229_302_305,
    330_002_838,
    -788_960_546,
    363_569_021,
    -1_947_871_109,
)

# SM4-like S-box, flattened from the original script's 2D table.
_ZB = (
    20,
    223,
    245,
    7,
    248,
    2,
    194,
    209,
    87,
    6,
    227,
    253,
    240,
    128,
    222,
    91,
    237,
    9,
    125,
    157,
    230,
    93,
    252,
    205,
    90,
    79,
    144,
    199,
    159,
    197,
    186,
    167,
    39,
    37,
    156,
    198,
    38,
    42,
    43,
    168,
    217,
    153,
    15,
    103,
    80,
    189,
    71,
    191,
    97,
    84,
    247,
    95,
    36,
    69,
    14,
    35,
    12,
    171,
    28,
    114,
    178,
    148,
    86,
    182,
    32,
    83,
    158,
    109,
    22,
    255,
    94,
    238,
    151,
    85,
    77,
    124,
    254,
    18,
    4,
    26,
    123,
    176,
    232,
    193,
    131,
    172,
    143,
    142,
    150,
    30,
    10,
    146,
    162,
    62,
    224,
    218,
    196,
    229,
    1,
    192,
    213,
    27,
    110,
    56,
    231,
    180,
    138,
    107,
    242,
    187,
    54,
    120,
    19,
    44,
    117,
    228,
    215,
    203,
    53,
    239,
    251,
    127,
    81,
    11,
    133,
    96,
    204,
    132,
    41,
    115,
    73,
    55,
    249,
    147,
    102,
    48,
    122,
    145,
    106,
    118,
    74,
    190,
    29,
    16,
    174,
    5,
    177,
    129,
    63,
    113,
    99,
    31,
    161,
    76,
    246,
    34,
    211,
    13,
    60,
    68,
    207,
    160,
    65,
    111,
    82,
    165,
    67,
    169,
    225,
    57,
    112,
    244,
    155,
    51,
    236,
    200,
    233,
    58,
    61,
    47,
    100,
    137,
    185,
    64,
    17,
    70,
    234,
    163,
    219,
    108,
    170,
    166,
    59,
    149,
    52,
    105,
    24,
    212,
    78,
    173,
    45,
    0,
    116,
    226,
    119,
    136,
    206,
    135,
    175,
    195,
    25,
    92,
    121,
    208,
    126,
    139,
    3,
    75,
    141,
    21,
    130,
    98,
    241,
    40,
    154,
    66,
    184,
    49,
    181,
    46,
    243,
    88,
    101,
    183,
    8,
    23,
    72,
    188,
    104,
    179,
    210,
    134,
    250,
    201,
    164,
    89,
    216,
    202,
    220,
    50,
    221,
    152,
    140,
    33,
    235,
    214,
)

_MASK32 = 0xFFFFFFFF


def md5_hex(data: str) -> str:
    """Lowercase 32-char hex MD5, matching the browser's md5 helper."""
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def header_value(api_path: str, dc0: str, *, rand_byte: int | None = None) -> str:
    """Full `x-zse-96` value including the `2.0_` prefix."""
    return "2.0_" + sign(api_path, dc0, rand_byte=rand_byte)


def sign(api_path: str, dc0: str, *, rand_byte: int | None = None) -> str:
    """Encrypted digest for one API path and cookie, without the prefix."""
    return encrypt(md5_hex(f"{XZSE93}+{api_path}+{dc0}"), rand_byte=rand_byte)


def encrypt(md5: str, *, rand_byte: int | None = None) -> str:
    """The core transform: pad, XOR-front, SM4-like rounds, custom base64.

    `rand_byte` (0..126) overrides the random leading byte that the browser
    picks per request. Zhihu accepts any value; tests pin one for determinism.
    """
    if rand_byte is None:
        # JS: Math.floor(Math.random() * 127) → 0..126.
        rand_byte = secrets.randbelow(127)
    if not 0 <= rand_byte <= 126:
        raise ValueError("rand_byte must be in 0..126")
    return _encode(_preprocess(md5, rand_byte))


def signed_headers(
    api_path: str, *, dc0: str, cookie: str | None = None, user_agent: str | None = None
) -> dict[str, str]:
    """The header set one signed Zhihu API request needs.

    `dc0` is the `d_c0` cookie value the signature is bound to; `cookie` is the
    full Cookie header to replay (defaulting to just `d_c0=<dc0>`). The
    `x-zse-93`/`x-api-version` pairs are pinned by the web client as of this
    port and travel with the signature, not with configuration.
    """
    if not dc0:
        raise ValueError("signing requires the d_c0 cookie value")
    headers = {
        "cookie": cookie if cookie else f"d_c0={dc0}",
        "x-zse-93": XZSE93,
        "x-zse-96": header_value(api_path, dc0),
        "x-app-za": "OS=Web",
        "x-api-version": "3.0.91",
    }
    if user_agent:
        headers["user-agent"] = user_agent
    return headers


# --------------------------------------------------------------------------
# The transform
# --------------------------------------------------------------------------


def _preprocess(md5: str, rand_byte: int) -> list[int]:
    """Pad and XOR the digest into the 48-byte block the rounds consume.

    Layout, matching the JS exactly: [rand, 0] + 32 char codes + fifteen 14s.
    The front 16 bytes are XOR-padded; the remaining 32 are encrypted in
    chained 16-byte chunks — and the fifteenth 14 is dropped, because the
    back slice reads exactly 32 elements. Reproduced as-is: "fixing" the
    dropped byte would break every real signature.
    """
    codes = [ord(char) for char in md5]
    arr = [rand_byte, 0, *codes, *([14] * 15)]
    front = arr[:16]
    back = arr[16:48]

    new_front = [(front[i] ^ FIX_ARR[i]) ^ 42 for i in range(16)]
    g_r = _sm4_block(new_front)
    return g_r + _sm4_chain(back, g_r)


def _sm4_block(block16: list[int]) -> list[int]:
    """One SM4-like block: 32 Feistel rounds over four chained words."""
    n = [0] * 36
    n[0] = _word(block16, 0)
    n[1] = _word(block16, 4)
    n[2] = _word(block16, 8)
    n[3] = _word(block16, 12)
    for r in range(32):
        o = _round(_u32(n[r + 1] ^ n[r + 2] ^ n[r + 3] ^ _u32(_ZK[r])))
        n[r + 4] = _u32(n[r] ^ o)
    bytes_out: list[int] = []
    for word in (n[35], n[34], n[33], n[32]):
        bytes_out += _word_to_bytes(word)
    return bytes_out


def _sm4_chain(data: list[int], key: list[int]) -> list[int]:
    """Encrypt `data` in 16-byte chunks, each chained to the previous output.

    The chunk count is derived from the data, not assumed: the back half is
    32 bytes today, and a future layout change degrades loudly instead of
    silently re-reading a fixed two chunks.
    """
    out: list[int] = []
    t = key
    for start in range(0, len(data), 16):
        chunk = data[start : start + 16]
        a = [chunk[c] ^ t[c] for c in range(len(chunk))]
        t = _sm4_block(a)
        out += t
    return out


def _round(e: int) -> int:
    """The SM4-like round function: S-box substitution then linear diffusion."""
    t = _word_to_bytes(e)
    n = [_ZB[t[0]], _ZB[t[1]], _ZB[t[2]], _ZB[t[3]]]
    r = _word(n, 0)
    return _u32(r ^ _rotl32(r, 2) ^ _rotl32(r, 10) ^ _rotl32(r, 18) ^ _rotl32(r, 24))


def _word_to_bytes(e: int) -> list[int]:
    e &= _MASK32
    return [(e >> 24) & 255, (e >> 16) & 255, (e >> 8) & 255, e & 255]


def _word(block: list[int], offset: int) -> int:
    b0, b1, b2, b3 = block[offset : offset + 4]
    return ((b0 & 255) << 24) | ((b1 & 255) << 16) | ((b2 & 255) << 8) | (b3 & 255)


def _rotl32(e: int, t: int) -> int:
    e &= _MASK32
    return ((e << t) | (e >> (32 - t))) & _MASK32


def _u32(n: int) -> int:
    return n & _MASK32


def _encode(processed: list[int]) -> str:
    """Serialise the 48 transformed bytes into the 64-char signature.

    Iterated from the tail; every third byte folds into one 24-bit word that
    is emitted as four 6-bit symbols over the custom alphabet. The 58's shift
    produces 58 only at i%4==0 and 0 otherwise — an obfuscation constant,
    kept as-written.
    """
    length = len(processed)
    result: list[str] = []
    current = 0
    for i in range(length):
        pop = processed[length - i - 1]
        b = 58 >> (8 * (i % 4))
        d = pop ^ (b & 255)
        current |= d << (8 * (i % 3))
        if i % 3 == 2:
            result.append("".join(SALT[(current >> x) & 63] for x in (0, 6, 12, 18)))
            current = 0
    return "".join(result)
