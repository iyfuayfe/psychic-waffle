"""AES 实现正确性验证。

两重验证：
  1. FIPS-197 官方测试向量 —— 证明 AES 核心算法没错
  2. 与门户 a41.js 自带的 CryptoJS 加密结果逐字节比对 —— 证明密文格式（ECB/PKCS7/Base64）
     与浏览器实际发出的完全一致

用法：
    python tools\\test_aes.py            # 只跑标准向量
    python tools\\test_aes.py --node     # 额外用 node + 门户 CryptoJS 交叉验证
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from campusnet import aes  # noqa: E402

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}\n         got : {got}\n         want: {want}")


# ------------------------------------------------ 1. FIPS-197 标准向量

print("1) FIPS-197 / SP 800-38A 标准测试向量")
key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
rk = aes._expand_key(key)
plain = bytes.fromhex("00112233445566778899aabbccddeeff")
cipher = aes.encrypt_block(plain, rk)
check("AES-128 单分组加密", cipher.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
check("AES-128 单分组解密", aes.decrypt_block(cipher, rk).hex(), plain.hex())

# ECB 多分组（SP 800-38A F.1.1 —— 注意官方这条向量用的是另一把密钥）
rk_ecb = aes._expand_key(bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c"))
ecb_pt = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
ecb_ct = (
    "3ad77bb40d7a3660a89ecaf32466ef97"
    "f5d3d58503b9699de785895a96fdbaaf"
    "43b1cd7f598ece23881b00e3ed030688"
    "7b0c785e27e8ad3f8223207104725dd4"
)
got = b"".join(aes.encrypt_block(ecb_pt[i : i + 16], rk_ecb) for i in range(0, 64, 16)).hex()
check("AES-128-ECB 四分组", got, ecb_ct)

# ------------------------------------------------ 2. PKCS7

print("\n2) PKCS7 填充")
check("16 字节 -> 补满一整块", len(aes.pkcs7_pad(b"x" * 16)), 32)
check("15 字节 -> 补 1 字节", aes.pkcs7_pad(b"x" * 15)[-1], 1)
check("空输入 -> 补 16 字节", len(aes.pkcs7_pad(b"")), 16)
check("往返一致", aes.pkcs7_unpad(aes.pkcs7_pad("哆点".encode("utf-8"))), "哆点".encode("utf-8"))

# ------------------------------------------------ 3. 自往返

print("\n3) 本实现加解密自往返")
for text in ["", "a", "hello world", "account@cmcc", "哆点 ePortal 认证"]:
    token = aes.encrypt_b64(text)
    check(f"往返 {text[:16]!r}", aes.decrypt_b64(token), text)

# ------------------------------------------------ 4. 与门户 CryptoJS 比对

if "--node" in sys.argv:
    print("\n4) 与门户自带 CryptoJS 逐字节比对")
    node = None
    for candidate in (
        Path("node.exe"),
    ):
        if candidate.exists():
            node = candidate
            break
    if node is None:
        from shutil import which

        found = which("node")
        node = Path(found) if found else None

    script = ROOT / "tools" / "_node_aes_check.js"
    if node is None:
        print("  [SKIP] 找不到 node，跳过")
    elif not script.exists():
        print(f"  [SKIP] 找不到 {script}，跳过")
    else:
        samples = [
            "",
            "a",
            "1234567890abcdef",
            "哆点 ePortal 认证测试",
            '{"login_method":1,"user_account":",0,account@cmcc","user_password":"your_password",'
            '"wlan_user_ip":"10.0.0.1","wlan_user_ipv6":"","wlan_user_mac":"000000000000",'
            '"wlan_ac_ip":"117.132.17.39","wlan_ac_name":"0013.0533.531.00","jsVersion":"4.2.1"}',
        ]
        tmp_dir = ROOT / "test" / "_tmp_aes"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            in_file = tmp_dir / "in.json"
            out_file = tmp_dir / "out.json"
            in_file.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
            proc = subprocess.run(
                [str(node), str(script), str(in_file), str(out_file)],
                capture_output=True,
                timeout=60,
            )
            if proc.returncode != 0:
                print(f"  [SKIP] node 执行失败：{proc.stderr.decode('utf-8', 'replace')[:300]}")
            else:
                expected = json.loads(out_file.read_text(encoding="utf-8"))
                for src, want in zip(samples, expected):
                    check(f"CryptoJS 一致 {src[:24]!r}", aes.encrypt_b64(src), want)
        finally:
            for leftover in tmp_dir.glob("*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass

print()
if FAILED:
    print(f"结果：{len(FAILED)} 项失败 -> {FAILED}")
    raise SystemExit(1)
print("结果：全部通过 ✅")
