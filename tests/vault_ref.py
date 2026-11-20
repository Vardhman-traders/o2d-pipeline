# ruff: noqa: E501
"""TEST-ONLY reference reader for the encrypted export, written from the format description in app/vault_crypto.py.
The app itself has no decryption code. Tests make a throwaway key pair, give the app the public half, and read the result with this."""
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

REC = 12 + 4 + 1024 * 1024 + 16
HEADER = 65 + 16 + 12 + 48


def new_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return priv, pub


def decrypt_export(blob: bytes, priv) -> bytes:
    eph_pub, export_id = blob[:65], blob[65:81]
    nonce, wrapped = blob[81:93], blob[93:141]
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
    kek = HKDF(hashes.SHA256(), 32, export_id, b"vault-v2-export-kek" + eph_pub + pub).derive(secret)
    dek = AESGCM(kek).decrypt(nonce, wrapped, b"vault-v2-wrap" + eph_pub + export_id)
    body = blob[HEADER:]
    assert body and len(body) % REC == 0
    n, out = len(body) // REC, []
    for i in range(n):
        rec = body[i * REC:(i + 1) * REC]
        aad = b"vault-v2-chunk" + export_id + struct.pack(">I", i) + (b"\x01" if i == n - 1 else b"\x00")
        inner = AESGCM(dek).decrypt(rec[:12], rec[12:], aad)
        (ln,) = struct.unpack(">I", inner[:4])
        out.append(inner[4:4 + ln])
    return b"".join(out)


def decrypt_file(record: bytes, priv) -> bytes:
    """One attachment record from a pack (see app/vault_crypto.py encrypt_file)."""
    eph_pub, file_id = record[:65], record[65:81]
    wrap_nonce, wrapped = record[81:93], record[93:141]
    nonce, body = record[141:153], record[153:]
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    secret = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
    kek = HKDF(hashes.SHA256(), 32, file_id, b"vault-v2a-file-kek" + eph_pub + pub).derive(secret)
    dek = AESGCM(kek).decrypt(wrap_nonce, wrapped, b"vault-v2a-wrap" + eph_pub + file_id)
    inner = AESGCM(dek).decrypt(nonce, body, b"vault-v2a-file" + file_id)
    (ln,) = struct.unpack(">I", inner[:4])
    return inner[4:4 + ln]
