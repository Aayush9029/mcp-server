"""Validation of an App Attest attestation object.

The steps are those of Apple, "Validating apps that connect to your server", section "Verify the attestation":
https://developer.apple.com/documentation/devicecheck/validating-apps-that-connect-to-your-server
The step for the `aclBlob` of a key on macOS is not here, because the app has no macOS version.
"""

import hashlib
import io
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeIs

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import verification

# Apple App Attestation Root CA, from https://www.apple.com/certificateauthority/Apple_App_Attestation_Root_CA.pem
# (linked as "App Attest root certificate" on https://www.apple.com/certificateauthority/private/).
# SHA-256 fingerprint 1C:B9:82:3B:A2:8B:A6:AD:2D:33:A0:06:94:1D:E2:AE:4F:51:3E:F1:D4:E8:31:B9:F7:E0:FA:7B:62:42:C9:32.
APPLE_APP_ATTESTATION_ROOT_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIICITCCAaegAwIBAgIQC/O+DvHN0uD7jG5yH2IXmDAKBggqhkjOPQQDAzBSMSYw
JAYDVQQDDB1BcHBsZSBBcHAgQXR0ZXN0YXRpb24gUm9vdCBDQTETMBEGA1UECgwK
QXBwbGUgSW5jLjETMBEGA1UECAwKQ2FsaWZvcm5pYTAeFw0yMDAzMTgxODMyNTNa
Fw00NTAzMTUwMDAwMDBaMFIxJjAkBgNVBAMMHUFwcGxlIEFwcCBBdHRlc3RhdGlv
biBSb290IENBMRMwEQYDVQQKDApBcHBsZSBJbmMuMRMwEQYDVQQIDApDYWxpZm9y
bmlhMHYwEAYHKoZIzj0CAQYFK4EEACIDYgAERTHhmLW07ATaFQIEVwTtT4dyctdh
NbJhFs/Ii2FdCgAHGbpphY3+d8qjuDngIN3WVhQUBHAoMeQ/cLiP1sOUtgjqK9au
Yen1mMEvRq9Sk3Jm5X8U62H+xTD3FE9TgS41o0IwQDAPBgNVHRMBAf8EBTADAQH/
MB0GA1UdDgQWBBSskRBTM72+aEH/pwyp5frq5eWKoTAOBgNVHQ8BAf8EBAMCAQYw
CgYIKoZIzj0EAwMDaAAwZQIwQgFGnByvsiVbpTKwSga0kP0e8EeDS4+sQmTvb7vn
53O5+FRXgeLhpJ06ysC5PrOyAjEAp5U4xDgEgllF7En3VcE3iexZZtKeYnpqtijV
oyFraWVIyd/dganmrduC1bmTBGwD
-----END CERTIFICATE-----
"""

Environment = Literal["production", "development"]

_FORMAT = "apple-appattest"
_NONCE_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
_AAGUID: dict[Environment, bytes] = {
    "production": b"appattest" + bytes(7),
    "development": b"appattestdevelop",
}
# Apple's table of validation categories: 0 is "Invalid", and 7, 8, and 9 "aren't appropriate".
_ACCEPTED_CATEGORIES = frozenset({1, 2, 3, 4, 5, 6})
_CATEGORY_EXTENSION = "apple_validation_category_01"
_VERSION_EXTENSION = "apple_bundle_version_01"
_HASH_LENGTH = 32
_COUNTER_END = 37
_AAGUID_END = 53
_ID_LENGTH_END = 55
_DER_SEQUENCE = 0x30
_DER_CONTEXT_1 = 0xA1
_DER_OCTET_STRING = 0x04
_DER_LONG_FORM = 0x80
_UINT32_BYTES = 4


class AttestationError(Exception):
    """The attestation is not valid. The message names the step that failed, for the server log."""


@dataclass(frozen=True, slots=True)
class Attestation:
    key_id: bytes
    # The public key of the attested key pair, as an X9.62 uncompressed point.
    public_key: bytes
    counter: int


def apple_roots() -> list[x509.Certificate]:
    return [x509.load_pem_x509_certificate(APPLE_APP_ATTESTATION_ROOT_CA_PEM)]


def _is_map(value: object) -> TypeIs[dict[object, object]]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def _decode(data: bytes) -> tuple[list[bytes], bytes]:
    """The certificates and the authenticator data of an attestation object."""
    try:
        decoded: object = cbor2.loads(data)
    except Exception as exc:
        raise AttestationError("the object is not CBOR") from exc
    if not _is_map(decoded) or decoded.get("fmt") != _FORMAT:
        raise AttestationError("the format is not apple-appattest")
    statement, auth_data = decoded.get("attStmt"), decoded.get("authData")
    chain = statement.get("x5c") if _is_map(statement) else None
    if not _is_list(chain) or not isinstance(auth_data, bytes):
        raise AttestationError("the object has no x5c or no authData")
    certificates = [item for item in chain if isinstance(item, bytes)]
    if len(certificates) != len(chain) or len(certificates) < 2:  # noqa: PLR2004  the leaf and one intermediate
        raise AttestationError("x5c must have the credential certificate and the intermediate certificate")
    return certificates, auth_data


def _verified_leaf(certificates: list[bytes], roots: Sequence[x509.Certificate], now: datetime) -> x509.Certificate:
    try:
        leaf, *intermediates = [x509.load_der_x509_certificate(item) for item in certificates]
        # The credential certificate is not a TLS certificate, so only the CA certificates get the Web PKI rules.
        policy = verification.PolicyBuilder().store(verification.Store(list(roots))).time(now)
        policy = policy.extension_policies(
            ca_policy=verification.ExtensionPolicy.webpki_defaults_ca(),
            ee_policy=verification.ExtensionPolicy.permit_all(),
        )
        policy.build_client_verifier().verify(leaf, intermediates)
    except (ValueError, verification.VerificationError) as exc:
        raise AttestationError(f"the certificate chain is not valid: {exc}") from exc
    return leaf


def _der_item(data: bytes, tag: int) -> bytes:
    """The content of the one DER item that `data` is. Only the short and the one-byte long length forms."""
    if len(data) < 2 or data[0] != tag:  # noqa: PLR2004  a tag byte and a length byte
        raise AttestationError("the nonce extension is not the expected DER")
    length, start = data[1], 2
    if length == _DER_LONG_FORM + 1 and len(data) > start:
        length, start = data[2], 3
    elif length >= _DER_LONG_FORM:
        raise AttestationError("the nonce extension is not the expected DER")
    if len(data) != start + length:
        raise AttestationError("the nonce extension is not the expected DER")
    return data[start:]


def _nonce_in(leaf: x509.Certificate) -> bytes:
    try:
        extension = leaf.extensions.get_extension_for_oid(_NONCE_OID).value
    except x509.ExtensionNotFound as exc:
        raise AttestationError("the credential certificate has no nonce extension") from exc
    if not isinstance(extension, x509.UnrecognizedExtension):
        raise AttestationError("the nonce extension is not the expected DER")
    # SEQUENCE { [1] { OCTET STRING nonce } }
    return _der_item(_der_item(_der_item(extension.value, _DER_SEQUENCE), _DER_CONTEXT_1), _DER_OCTET_STRING)


def _public_point(leaf: x509.Certificate) -> bytes:
    key = leaf.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise AttestationError("the attested key is not a P-256 key")
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _check_extensions(rest: bytes) -> None:
    """The bytes after the credential ID: the COSE key, then the optional `extensions` dictionary."""
    stream = io.BytesIO(rest)
    decoder = cbor2.CBORDecoder(stream)
    try:
        decoder.decode()
        extensions: object = decoder.decode() if stream.tell() < len(rest) else None
    except Exception as exc:
        raise AttestationError("the authenticator data does not end with CBOR") from exc
    if extensions is None:
        return
    if not _is_map(extensions) or stream.tell() != len(rest):
        raise AttestationError("the extensions are not one CBOR dictionary")
    if _CATEGORY_EXTENSION in extensions:
        raw = extensions[_CATEGORY_EXTENSION]
        # A UInt32. Apple's sample has it as four bytes, the low byte first.
        category = int.from_bytes(raw, "little") if isinstance(raw, bytes) and len(raw) == _UINT32_BYTES else raw
        if category not in _ACCEPTED_CATEGORIES:
            raise AttestationError(f"the validation category {category!r} is not accepted")
    version = extensions.get(_VERSION_EXTENSION, "")
    if not isinstance(version, str):
        raise AttestationError("the bundle version is not text")


def verify_attestation(
    *,
    attestation: bytes,
    key_id: bytes,
    client_data_hash: bytes,
    app_ids: Collection[str],
    environment: Environment,
    roots: Sequence[x509.Certificate],
    now: datetime,
) -> Attestation:
    """Run each step of Apple's procedure. Raises `AttestationError` at the first step that fails."""
    certificates, auth_data = _decode(attestation)
    leaf = _verified_leaf(certificates, roots, now)

    nonce = hashlib.sha256(auth_data + client_data_hash).digest()
    if _nonce_in(leaf) != nonce:
        raise AttestationError("the nonce is not the one of this challenge")

    public_key = _public_point(leaf)
    if hashlib.sha256(public_key).digest() != key_id:
        raise AttestationError("the key ID is not the hash of the public key")

    if len(auth_data) < _ID_LENGTH_END:
        raise AttestationError("the authenticator data is too short")
    if auth_data[:_HASH_LENGTH] not in {hashlib.sha256(app_id.encode()).digest() for app_id in app_ids}:
        raise AttestationError("the RP ID is not one of the app IDs")
    counter = int.from_bytes(auth_data[_HASH_LENGTH + 1 : _COUNTER_END], "big")
    if counter != 0:
        raise AttestationError("the counter is not 0")
    if auth_data[_COUNTER_END:_AAGUID_END] != _AAGUID[environment]:
        raise AttestationError(f"the aaguid is not the one of the {environment} environment")
    id_end = _ID_LENGTH_END + int.from_bytes(auth_data[_AAGUID_END:_ID_LENGTH_END], "big")
    if auth_data[_ID_LENGTH_END:id_end] != key_id:
        raise AttestationError("the credential ID is not the key ID")
    _check_extensions(auth_data[id_end:])
    return Attestation(key_id=key_id, public_key=public_key, counter=counter)
