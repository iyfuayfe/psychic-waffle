"""纯 Python AES-128-ECB + PKCS7 + Base64（零第三方依赖）。

为什么需要它：城市热点 Dr.COM / 哆点 ePortal 的门户 JS（a41.js 的 util._jsonp）在
page.login_method != 0 时，会把整个登录参数 JSON **AES 加密**后塞进单个 `params` 字段：

    params.data = { 'params': util.aes_en(JSON.stringify(params.data)) }

    aes_en: function (str, key='5c1d5ad4dea0e8dd') {
      return CryptoJS.AES.encrypt(str, CryptoJS.enc.Utf8.parse(key), {
        mode: CryptoJS.mode.ECB,
        padding: CryptoJS.pad.Pkcs7
      }).toString();          // key 是 WordArray => 不加 OpenSSL 盐头，输出就是 Base64(密文)
    }

所以裸发明文参数会被服务端判为非法请求（"认证出现异常"）。
这里用纯 Python 复刻同一套算法，保持"零第三方依赖"。

正确性由两重验证保证：
  1. FIPS-197 官方测试向量（见 tools/test_aes.py）
  2. 与门户自己的 CryptoJS 逐字节比对（tools/test_aes.py --node）
"""

from __future__ import annotations

import base64

# ---------------------------------------------------------------- 基础运算


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法。"""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        b >>= 1
        a = _xtime(a)
    return result


def _build_tables() -> tuple[list[int], list[int]]:
    inverse = [0] * 256
    for i in range(1, 256):
        for j in range(1, 256):
            if _gmul(i, j) == 1:
                inverse[i] = j
                break
    sbox = [0] * 256
    for i in range(256):
        x = inverse[i]
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        sbox[i] = s ^ 0x63
    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i
    return sbox, inv_sbox


SBOX, INV_SBOX = _build_tables()
RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


# ---------------------------------------------------------------- 密钥扩展


def _expand_key(key: bytes) -> list[bytes]:
    if len(key) != 16:
        raise ValueError("本实现只支持 AES-128（16 字节密钥）")
    w = [list(key[i * 4 : i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [bytes(b for word in w[r * 4 : r * 4 + 4] for b in word) for r in range(11)]


# ---------------------------------------------------------------- 分组加解密
# 状态按列优先存放：flat[r + 4*c]


def _add_round_key(state: list[int], rk: bytes) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _sub_bytes(state: list[int], box: list[int]) -> None:
    for i in range(16):
        state[i] = box[state[i]]


def _shift_rows(state: list[int]) -> None:
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        for c in range(4):
            state[r + 4 * c] = row[(c + r) % 4]


def _inv_shift_rows(state: list[int]) -> None:
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        for c in range(4):
            state[r + 4 * c] = row[(c - r) % 4]


def _mix_columns(state: list[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        state[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        state[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        state[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)


def _inv_mix_columns(state: list[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        state[i + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        state[i + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        state[i + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[0])
    for rnd in range(1, 10):
        _sub_bytes(state, SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])
    _sub_bytes(state, SBOX)
    _shift_rows(state)
    _add_round_key(state, round_keys[10])
    return bytes(state)


def decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[10])
    for rnd in range(9, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, INV_SBOX)
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, INV_SBOX)
    _add_round_key(state, round_keys[0])
    return bytes(state)


# ---------------------------------------------------------------- PKCS7


def pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("密文长度不是 16 的整数倍")
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充不合法")
    return data[:-pad]


# ---------------------------------------------------------------- 对外接口

DEFAULT_KEY = "5c1d5ad4dea0e8dd"  # a41.js: aes_en 的默认 key


def encrypt_ecb(plaintext: bytes, key: str = DEFAULT_KEY) -> bytes:
    round_keys = _expand_key(key.encode("utf-8"))
    data = pkcs7_pad(plaintext)
    return b"".join(encrypt_block(data[i : i + 16], round_keys) for i in range(0, len(data), 16))


def decrypt_ecb(ciphertext: bytes, key: str = DEFAULT_KEY) -> bytes:
    if len(ciphertext) % 16:
        raise ValueError("密文长度不是 16 的整数倍")
    round_keys = _expand_key(key.encode("utf-8"))
    data = b"".join(
        decrypt_block(ciphertext[i : i + 16], round_keys) for i in range(0, len(ciphertext), 16)
    )
    return pkcs7_unpad(data)


def encrypt_b64(text: str, key: str = DEFAULT_KEY) -> str:
    """等价于门户 JS 的 util.aes_en(text)。"""
    return base64.b64encode(encrypt_ecb(text.encode("utf-8"), key)).decode("ascii")


def decrypt_b64(token: str, key: str = DEFAULT_KEY) -> str:
    return decrypt_ecb(base64.b64decode(token), key).decode("utf-8")
