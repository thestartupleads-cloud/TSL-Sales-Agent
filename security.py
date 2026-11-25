import base64
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def verify_ghl_signature(raw_body: bytes, signature: str | None, public_key: str) -> bool:
    """Verify an Ed25519 signature; reject missing or malformed configuration."""
    if not signature or not public_key:
        return False
    try:
        key_data = base64.b64decode(public_key, validate=True)
        sig_data = base64.b64decode(signature, validate=True)
        Ed25519PublicKey.from_public_bytes(key_data).verify(sig_data, raw_body)
        return True
    except (ValueError, InvalidSignature):
        return False

