# ruff: noqa: E501
"""Encrypted-export format (vault v2), WRITE SIDE ONLY.

This server holds just a PUBLIC key, so it can lock an export but can never unlock one: the ephemeral private key used for each export
is thrown away as soon as the key is wrapped, and no decryption code or private key exists in this app.

Layout (all big-endian):
  header  ephemeralPub(65) | exportId(16) | wrapNonce(12) | wrappedDEK(48)
  body    chunk records, each exactly 1 MiB of data + 4 length + 12 nonce + 16 tag (padded), AES-256-GCM
  wrap    KEK = HKDF-SHA256(ECDH(ephemeral, recipient), salt=exportId, info="vault-v2-export-kek"||ephemeralPub||recipientPub)
  AAD     wrap: "vault-v2-wrap"||ephemeralPub||exportId     chunk: "vault-v2-chunk"||exportId||u32(index)||u8(final)
"""
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PUB_LEN = 65
DATA_PER_CHUNK = 1024 * 1024
CHUNK_LABEL = b"vault-v2-chunk"


class BadPublicKey(ValueError):
    pass


def load_public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    """A 65-byte uncompressed P-256 point. Anything else is refused (so a typo in the setting fails loudly, not silently)."""
    if len(raw) != PUB_LEN:
        raise BadPublicKey("public key must be 65 bytes")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise BadPublicKey("not a valid P-256 public key") from None


def _chunks(dek: bytes, export_id: bytes, data: bytes) -> bytes:
    pieces = [data[i:i + DATA_PER_CHUNK] for i in range(0, len(data), DATA_PER_CHUNK)] or [b""]
    aes = AESGCM(dek)
    out = []
    for i, piece in enumerate(pieces):
        inner = struct.pack(">I", len(piece)) + piece + b"\x00" * (DATA_PER_CHUNK - len(piece))
        nonce = os.urandom(12)
        aad = CHUNK_LABEL + export_id + struct.pack(">I", i) + (b"\x01" if i == len(pieces) - 1 else b"\x00")
        out.append(nonce + aes.encrypt(nonce, inner, aad))
    return b"".join(out)


def encrypt_export(data: bytes, recipient_pub_raw: bytes) -> tuple[bytes, bytes]:
    """Returns (vault_bytes, export_id). export_id is also inside the file; keep it in the audit log to trace a leaked copy."""
    recipient = load_public_key(recipient_pub_raw)
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    export_id, nonce, dek = os.urandom(16), os.urandom(12), os.urandom(32)
    secret = eph.exchange(ec.ECDH(), recipient)
    kek = HKDF(hashes.SHA256(), 32, export_id, b"vault-v2-export-kek" + eph_pub + recipient_pub_raw).derive(secret)
    wrapped = AESGCM(kek).encrypt(nonce, dek, b"vault-v2-wrap" + eph_pub + export_id)
    return eph_pub + export_id + nonce + wrapped + _chunks(dek, export_id, data), export_id


# ------------------------------------------------------------------ one photo / document (attachment pack records)
# record = ephemeralPub(65) | fileId(16) | wrapNonce(12) | wrappedDEK(48) | nonce(12) | AES-GCM(DEK, inner)
#   inner = u32(len) | data | zero padding up to a size bucket (64 KiB doubling), so the file's real size is not visible
#   wrap AAD "vault-v2a-wrap"||ephemeralPub||fileId ; body AAD "vault-v2a-file"||fileId ; KEK info "vault-v2a-file-kek"||ephemeralPub||recipientPub
# Every file has its own key, so opening one photo reveals nothing about any other.
MIN_BUCKET = 64 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
FILE_HEADER = PUB_LEN + 16 + 12 + 48


def bucket_for(n: int) -> int:
    inner = 4 + n
    b = MIN_BUCKET
    while b < inner:
        b *= 2
    return b


def encrypt_file(data: bytes, recipient_pub_raw: bytes) -> tuple[bytes, bytes]:
    """Returns (record, file_id). Refuses files over 64 MiB (the app itself caps photos at 8 MiB)."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    recipient = load_public_key(recipient_pub_raw)
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    file_id, wrap_nonce, dek, nonce = os.urandom(16), os.urandom(12), os.urandom(32), os.urandom(12)
    kek = HKDF(hashes.SHA256(), 32, file_id, b"vault-v2a-file-kek" + eph_pub + recipient_pub_raw).derive(eph.exchange(ec.ECDH(), recipient))
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, b"vault-v2a-wrap" + eph_pub + file_id)
    inner = struct.pack(">I", len(data)) + data + b"\x00" * (bucket_for(len(data)) - 4 - len(data))
    body = AESGCM(dek).encrypt(nonce, inner, b"vault-v2a-file" + file_id)
    return eph_pub + file_id + wrap_nonce + wrapped + nonce + body, file_id
