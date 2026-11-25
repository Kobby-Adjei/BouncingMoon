"""Passkeys (WebAuthn), checked here with nothing but the `cryptography` package.

A passkey is a key pair the person's phone or computer makes and keeps (Face ID, a fingerprint, a
PIN unlock it). Signing up, the device hands the hub the public half; signing in, it signs a fresh
random challenge with the private half, which never leaves the device. There is no password to
steal or phish: a passkey only works on the site it was made for, which the browser enforces and
this module checks (the origin and the site's hash are in what is signed).

Only what Relay needs: "none" attestation (we don't need to know the device's maker), discoverable
credentials (the person picks their passkey; no username to type), and the three signature types
browsers use: ES256, EdDSA and RS256.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

ALGS = (-7, -8, -257)            # ES256, EdDSA, RS256: what we ask browsers for, in this order


class PasskeyError(Exception):
    """The passkey response didn't check out; the message says why, for the log and the person."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))
    except (ValueError, TypeError):
        raise PasskeyError("not base64url") from None


# --------------------------------------------------------------------------- CBOR (only what WebAuthn sends)
def cbor(data: bytes, pos: int = 0) -> tuple[Any, int]:
    """Decode one CBOR item from data[pos:]; returns (value, next position)."""
    if pos >= len(data):
        raise PasskeyError("truncated CBOR")
    first = data[pos]
    major, info = first >> 5, first & 0x1F
    pos += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        if pos + n > len(data):
            raise PasskeyError("truncated CBOR")
        arg = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    else:
        raise PasskeyError("unsupported CBOR")
    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major in (2, 3):
        if pos + arg > len(data):
            raise PasskeyError("truncated CBOR")
        raw = data[pos:pos + arg]
        return (raw if major == 2 else raw.decode("utf-8")), pos + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, pos = cbor(data, pos)
            out.append(v)
        return out, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            k, pos = cbor(data, pos)
            v, pos = cbor(data, pos)
            out[k] = v
        return out, pos
    if major == 6:
        return cbor(data, pos)                         # a tag: keep the value
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], pos
    raise PasskeyError("unsupported CBOR")


# --------------------------------------------------------------------------- keys
def public_key(cose: bytes):
    """A COSE public key (as the authenticator sends it) -> (cryptography key, alg)."""
    k, _ = cbor(cose)
    if not isinstance(k, dict):
        raise PasskeyError("bad key")
    kty, alg = k.get(1), k.get(3)
    try:
        if kty == 2 and alg == -7 and k.get(-1) == 1:            # EC2, P-256
            x, y = int.from_bytes(k[-2], "big"), int.from_bytes(k[-3], "big")
            return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key(), alg
        if kty == 1 and alg == -8 and k.get(-1) == 6:            # OKP, Ed25519
            return ed25519.Ed25519PublicKey.from_public_bytes(k[-2]), alg
        if kty == 3 and alg == -257:                             # RSA
            n, e = int.from_bytes(k[-1], "big"), int.from_bytes(k[-2], "big")
            return rsa.RSAPublicNumbers(e, n).public_key(), alg
    except (KeyError, ValueError, TypeError):
        raise PasskeyError("bad key") from None
    raise PasskeyError("unsupported key type")


def check_signature(cose: bytes, signed: bytes, sig: bytes) -> None:
    key, alg = public_key(cose)
    try:
        if alg == -7:
            key.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == -8:
            key.verify(sig, signed)
        else:
            key.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise PasskeyError("signature doesn't match") from None


# --------------------------------------------------------------------------- what the browser sends
def auth_data(data: bytes) -> dict:
    if len(data) < 37:
        raise PasskeyError("authenticator data too short")
    out = {"rp_hash": data[:32], "flags": data[32], "count": struct.unpack(">I", data[33:37])[0]}
    if out["flags"] & 0x40:                            # AT: a new credential is attached
        if len(data) < 55:
            raise PasskeyError("authenticator data too short")
        n = struct.unpack(">H", data[53:55])[0]
        out["cred_id"] = data[55:55 + n]
        _, end = cbor(data, 55 + n)
        out["cose"] = data[55 + n:end]
    return out


def client_data(raw: bytes, kind: str, challenge: str, origin: str) -> None:
    try:
        cd = json.loads(raw)
    except ValueError:
        raise PasskeyError("bad client data") from None
    if cd.get("type") != kind:
        raise PasskeyError("wrong ceremony")
    if cd.get("challenge") != challenge:
        raise PasskeyError("challenge doesn't match")
    if cd.get("origin") != origin:
        raise PasskeyError(f"made for another site ({str(cd.get('origin'))[:80]})")
    if cd.get("crossOrigin"):
        raise PasskeyError("cross-origin use refused")


def check_flags(ad: dict, rp_id: str) -> None:
    if ad["rp_hash"] != hashlib.sha256(rp_id.encode()).digest():
        raise PasskeyError("made for another site")
    if not ad["flags"] & 0x01:                         # UP: someone was there
        raise PasskeyError("no one was present")
    if not ad["flags"] & 0x04:                         # UV: and it was them (Face ID, a fingerprint, the PIN)
        raise PasskeyError("the device didn't check who it was")


def registration(body: dict, challenge: str, origin: str, rp_id: str) -> dict:
    """navigator.credentials.create() result -> {"cred_id", "cose", "count"}."""
    resp = body.get("response") or {}
    cdj = unb64u(resp.get("clientDataJSON", ""))
    client_data(cdj, "webauthn.create", challenge, origin)
    att, _ = cbor(unb64u(resp.get("attestationObject", "")))
    if not isinstance(att, dict) or "authData" not in att:
        raise PasskeyError("bad attestation")
    ad = auth_data(att["authData"])
    check_flags(ad, rp_id)
    if "cred_id" not in ad:
        raise PasskeyError("no credential in the response")
    public_key(ad["cose"])                             # refuse key types we couldn't check later
    return {"cred_id": b64u(ad["cred_id"]), "cose": ad["cose"], "count": ad["count"]}


def assertion(body: dict, challenge: str, origin: str, rp_id: str, cose: bytes, stored_count: int) -> int:
    """navigator.credentials.get() result, checked against the stored key -> the new sign count."""
    resp = body.get("response") or {}
    cdj = unb64u(resp.get("clientDataJSON", ""))
    client_data(cdj, "webauthn.get", challenge, origin)
    adb = unb64u(resp.get("authenticatorData", ""))
    ad = auth_data(adb)
    check_flags(ad, rp_id)
    check_signature(cose, adb + hashlib.sha256(cdj).digest(), unb64u(resp.get("signature", "")))
    if stored_count and ad["count"] and ad["count"] <= stored_count:
        raise PasskeyError("this passkey may have been copied (its counter went backwards)")
    return ad["count"]


def user_handle(body: dict) -> Optional[str]:
    h = (body.get("response") or {}).get("userHandle")
    return unb64u(h).decode("utf-8", "replace") if h else None
