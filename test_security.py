import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from clearcoat.security import verify_ghl_signature


def test_signature_verification():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    body = b'{"locationId":"test"}'
    signature = base64.b64encode(private.sign(body)).decode()
    assert verify_ghl_signature(body, signature, public)
    assert not verify_ghl_signature(body + b" ", signature, public)
    assert not verify_ghl_signature(body, None, public)

