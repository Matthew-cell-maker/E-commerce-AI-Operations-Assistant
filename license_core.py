"""
license_core.py
客户端授权验证 —— 纯 Python 实现的 Ed25519 签名验证，零第三方依赖。

设计：
  - 私钥仅存在于卖家的生成器(keygen.py)中，永不进入客户端。
  - 客户端只内置公钥，用于验证"机器码 + 签名"是否由持有私钥者签发。
  - 攻击者即便完全反编译客户端，只能得到公钥，无法伪造通过验证的激活码
    （伪造等价于破解 Ed25519，现实中不可行）。

激活码格式（Base32，便于手输/复制，无易混字符）：
  payload = machine_code(规范化)
  signature = Ed25519_sign(private_key, payload)
  激活码 = base32(signature)   # 64 字节签名 → 104 个 Base32 字符，分组展示

验证：base32 解码得到签名 → Ed25519_verify(public_key, machine_code, signature)
"""

import hashlib

# ---------------------------------------------------------------------------
# 卖家公钥（32 字节，hex 表示）。由 keygen.py 首次运行生成后填入此处。
# 这是唯一需要随客户端分发的密钥；私钥务必离线保管，切勿泄露。
# ---------------------------------------------------------------------------
PUBLIC_KEY_HEX = "8069ee83a2370f98beb9637791cb9c07146065eba01869a1a6debd295b2a0ae0"

# ===========================================================================
# 以下为纯 Python 的 Ed25519 实现（仅验证所需部分）。
# 参考 RFC 8032。不依赖任何第三方库。
# ===========================================================================

_p = 2 ** 255 - 19
_d = (-121665 * pow(121666, _p - 2, _p)) % _p
_I = pow(2, (_p - 1) // 4, _p)
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _p - 2, _p)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = (4 * _inv(5)) % _p
_Bx = _xrecover(_By)
_B = [_Bx % _p, _By % _p, 1, (_Bx * _By) % _p]


def _edwards_add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = ((y1 - x1) * (y2 - x2)) % _p
    b = ((y1 + x1) * (y2 + x2)) % _p
    c = (t1 * 2 * _d * t2) % _p
    dd = (z1 * 2 * z2) % _p
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    x3 = e * f
    y3 = g * h
    t3 = e * h
    z3 = f * g
    return [x3 % _p, y3 % _p, z3 % _p, t3 % _p]


def _scalarmult(P, e):
    if e == 0:
        return [0, 1, 1, 0]
    Q = _scalarmult(P, e // 2)
    Q = _edwards_add(Q, Q)
    if e & 1:
        Q = _edwards_add(Q, P)
    return Q


def _encodeint(y):
    return y.to_bytes(32, "little")


def _decodeint(s):
    return int.from_bytes(s, "little")


def _decodepoint(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if x & 1 != (int.from_bytes(s, "little") >> 255) & 1:
        x = _p - x
    P = [x, y, 1, (x * y) % _p]
    if not _isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _isoncurve(P):
    x, y, z, t = P
    return (
        z % _p != 0
        and (x * y) % _p == (z * t) % _p
        and (y * y - x * x - z * z - _d * t * t) % _p == 0
    )


def _hint(m):
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """验证 Ed25519 签名。public_key 32 字节，signature 64 字节。"""
    if len(signature) != 64 or len(public_key) != 32:
        return False
    try:
        R = _decodepoint(signature[:32])
        A = _decodepoint(public_key)
        S = _decodeint(signature[32:])
        h = _hint(signature[:32] + public_key + message)
        ra = _scalarmult(_B, S)
        rb = _edwards_add(R, _scalarmult(A, h))
        # 比较 ra == rb（投影坐标需归一化后比较）
        x1, y1, z1, _ = ra
        x2, y2, z2, _ = rb
        if (x1 * z2 - x2 * z1) % _p != 0:
            return False
        if (y1 * z2 - y2 * z1) % _p != 0:
            return False
        return True
    except Exception:
        return False


# ===========================================================================
# Base32 编解码（标准 RFC 4648，去掉 padding）
# ===========================================================================
_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _b32encode(data: bytes) -> str:
    bits = 0
    value = 0
    out = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_B32_ALPHABET[(value >> bits) & 0x1F])
    if bits > 0:
        out.append(_B32_ALPHABET[(value << (5 - bits)) & 0x1F])
    return "".join(out)


def _b32decode(s: str) -> bytes:
    s = s.upper().replace(" ", "").replace("-", "")
    bits = 0
    value = 0
    out = bytearray()
    for ch in s:
        idx = _B32_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"非法字符: {ch}")
        value = (value << 5) | idx
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((value >> bits) & 0xFF)
    return bytes(out)


# ===========================================================================
# 对外接口
# ===========================================================================

def normalize_machine_code(machine_code: str) -> bytes:
    """规范化机器码，确保生成端与验证端一致。"""
    return machine_code.strip().upper().encode("utf-8")


def format_activation_key(raw: str) -> str:
    """把连续 Base32 字符串按每 5 个一组用 '-' 分隔，便于阅读和手输。"""
    s = raw.upper().replace(" ", "").replace("-", "")
    return "-".join(s[i:i + 5] for i in range(0, len(s), 5))


def verify_activation_key(machine_code: str, activation_key: str,
                          public_key_hex: str = None) -> bool:
    """
    验证激活码是否对该机器码有效。
    machine_code:   本机机器码
    activation_key: 用户输入的激活码（可含 '-' 和空格）
    """
    pub_hex = public_key_hex or PUBLIC_KEY_HEX
    try:
        public_key = bytes.fromhex(pub_hex)
        signature = _b32decode(activation_key)
        message = normalize_machine_code(machine_code)
        return ed25519_verify(public_key, message, signature)
    except Exception:
        return False
