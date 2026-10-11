"""A stand-in for Apple's App Attest service. It signs attestation objects with its own root certificate."""

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.support import Json

NONCE_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
APP_ID = "ABCDE12345.com.example.app"
AAGUID = {"production": b"appattest" + bytes(7), "development": b"appattestdevelop"}


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Inc."),
        ]
    )


def _ca_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


@dataclass
class Attest:
    """The parts of one attestation. A test changes one part to make an attestation that must fail."""

    challenge: str
    app_id: str = APP_ID
    environment: str = "production"
    counter: int = 0
    category: int | None = 4
    fmt: str = "apple-appattest"
    # The challenge that goes into the nonce. None is the challenge that the client sends.
    nonce_challenge: str | None = None
    sent_key_id: bytes | None = None
    credential_id: bytes | None = None
    valid_for: timedelta = timedelta(days=3)
    with_intermediate: bool = True
    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))


class FakeAppAttest:
    def __init__(self, now: float) -> None:
        self._now = datetime.fromtimestamp(now, UTC)
        self._root_key = ec.generate_private_key(ec.SECP384R1())
        self._intermediate_key = ec.generate_private_key(ec.SECP384R1())
        self.root = self._ca("Test App Attestation Root CA", self._root_key, None)
        self._intermediate = self._ca("Test App Attestation CA 1", self._intermediate_key, self.root)

    def _ca(self, name: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Certificate | None) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(name))
            .issuer_name(issuer.subject if issuer else _name(name))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(self._now - timedelta(days=365))
            .not_valid_after(self._now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(_ca_usage(), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        )
        if issuer:
            authority = x509.AuthorityKeyIdentifier.from_issuer_public_key(self._root_key.public_key())
            builder = builder.add_extension(authority, critical=False)
        return builder.sign(self._root_key, hashes.SHA384())

    def _credential_certificate(self, parts: Attest, nonce: bytes) -> x509.Certificate:
        nonce_der = b"\x30\x24\xa1\x22\x04\x20" + nonce
        key_id = hashlib.sha256(_point(parts.key)).digest()
        return (
            x509.CertificateBuilder()
            .subject_name(_name(key_id.hex()))
            .issuer_name(self._intermediate.subject)
            .public_key(parts.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(self._now - timedelta(hours=1))
            .not_valid_after(self._now - timedelta(hours=1) + parts.valid_for)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.UnrecognizedExtension(NONCE_OID, nonce_der), critical=False)
            .sign(self._intermediate_key, hashes.SHA256())
        )

    def attest(self, parts: Attest) -> Json:
        """The `attestation` object of `POST /v1/accounts`."""
        key_id = hashlib.sha256(_point(parts.key)).digest()
        numbers = parts.key.public_key().public_numbers()
        cose_key = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        credential_id = key_id if parts.credential_id is None else parts.credential_id
        auth_data = (
            hashlib.sha256(parts.app_id.encode()).digest()
            + b"\x40"
            + parts.counter.to_bytes(4, "big")
            + AAGUID[parts.environment]
            + len(credential_id).to_bytes(2, "big")
            + credential_id
            + cbor2.dumps(cose_key)
        )
        if parts.category is not None:
            extensions = {
                "apple_bundle_version_01": "1.0",
                "apple_validation_category_01": parts.category.to_bytes(4, "little"),
            }
            auth_data += cbor2.dumps(extensions)
        client_data_hash = hashlib.sha256((parts.nonce_challenge or parts.challenge).encode()).digest()
        nonce = hashlib.sha256(auth_data + client_data_hash).digest()
        chain = [self._credential_certificate(parts, nonce)]
        if parts.with_intermediate:
            chain.append(self._intermediate)
        statement = {"x5c": [item.public_bytes(serialization.Encoding.DER) for item in chain], "receipt": b"receipt"}
        attestation = cbor2.dumps({"fmt": parts.fmt, "attStmt": statement, "authData": auth_data})
        return {
            "key_id": base64.b64encode(parts.sent_key_id or key_id).decode(),
            "challenge": parts.challenge,
            "object": base64.b64encode(attestation).decode(),
        }


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
