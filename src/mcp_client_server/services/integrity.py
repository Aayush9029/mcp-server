import base64
import hashlib
import logging
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

from cryptography import x509

from mcp_client_server import appattest, totp
from mcp_client_server.appattest import AttestationError
from mcp_client_server.clock import Clock
from mcp_client_server.config import Settings
from mcp_client_server.errors import AppCodeInvalid, AttestationInvalid, AttestationRequired
from mcp_client_server.limits import ATTEST_CHALLENGE_TTL_SECONDS
from mcp_client_server.models import AttestationInput, AttestChallenge
from mcp_client_server.store import AttestedKey, Store

logger = logging.getLogger(__name__)


class IntegrityService:
    """Proof that a request comes from the real app. Each check is off until its setting is there."""

    def __init__(self, store: Store, clock: Clock, settings: Settings, roots: Sequence[x509.Certificate]) -> None:
        self._store = store
        self._clock = clock
        self._app_ids = settings.attest_app_ids
        self._environment: appattest.Environment = settings.app_attest_environment
        self._totp_key = settings.totp_key
        self._roots = roots

    def check_app_code(self, code: str | None) -> None:
        if self._totp_key is None:
            return
        if not code or not totp.is_valid(self._totp_key, code.strip(), self._clock.now()):
            raise AppCodeInvalid("Missing or wrong app code.")

    async def new_challenge(self) -> AttestChallenge:
        challenge = secrets.token_urlsafe(32)
        await self._store.put_challenge(challenge, self._clock.now())
        return AttestChallenge(challenge=challenge, expires_in=ATTEST_CHALLENGE_TTL_SECONDS)

    async def attested_key(self, data: AttestationInput | None) -> AttestedKey | None:
        """Validate the attestation of a new account. None if the check is off."""
        if not self._app_ids:
            return None
        if data is None:
            raise AttestationRequired("This server needs an App Attest attestation to make an account.")
        issued_at = await self._store.take_challenge(data.challenge)
        if issued_at is None or self._clock.now() - issued_at > ATTEST_CHALLENGE_TTL_SECONDS:
            raise AttestationInvalid("The challenge is not known, is used, or is expired.")
        try:
            attestation = appattest.verify_attestation(
                attestation=data.attestation_object,
                key_id=data.key_id,
                client_data_hash=hashlib.sha256(data.challenge.encode()).digest(),
                app_ids=self._app_ids,
                environment=self._environment,
                roots=self._roots,
                now=datetime.fromtimestamp(self._clock.now(), UTC),
            )
        except AttestationError as exc:
            logger.warning("attestation refused: %s", exc)
            raise AttestationInvalid("The attestation is not valid.") from exc
        return AttestedKey(
            key_id=base64.b64encode(attestation.key_id).decode(),
            public_key=base64.b64encode(attestation.public_key).decode(),
            counter=attestation.counter,
        )
