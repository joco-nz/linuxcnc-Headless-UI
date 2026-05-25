"""OIDC token validation and user extraction for the fleet gateway."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, Optional

import jwt

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class User:
    """Extracted user identity and attributes from a validated OIDC token."""

    sub: str  # subject (unique user identifier)
    name: str  # display name
    email: Optional[str] = None
    role: str = "viewer"  # viewer, operator, programmer, maintainer, admin
    facility: Optional[str] = None
    machine_tags: list[str] = dataclasses.field(default_factory=list)


class TokenValidationError(Exception):
    """Raised when JWT validation fails."""

    def __init__(self, message: str, error_code: int = 401):
        super().__init__(message)
        self.error_code = error_code


class AuthManager:
    """Validates OIDC access tokens and extracts user identity + attributes.

    Supports HS256 (symmetric) and RS256/RS384/RS512 (asymmetric) algorithms.
    For asymmetric keys, uses JWKS endpoint to fetch signing keys by kid header.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: Optional[str] = None,
        secret_key: Optional[str] = None,
        algorithms: list[str] = None,
    ):
        if algorithms is None:
            algorithms = ["RS256", "HS256"]

        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.secret_key = secret_key
        self.algorithms = algorithms
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_expires_at: float = 0.0

    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch JWKS document from the configured endpoint with caching."""
        import urllib.request

        if self.jwks_url is None:
            raise TokenValidationError("JWKS URL not configured")

        # Return cached result if still valid (cache for 5 minutes)
        if self._jwks_cache and time.time() < self._jwks_expires_at:
            return self._jwks_cache

        req = urllib.request.Request(self.jwks_url)
        with urllib.request.urlopen(req, timeout=10) as response:
            jwks_data = json.loads(response.read())

        self._jwks_cache = jwks_data
        self._jwks_expires_at = time.time() + 300  # 5 minute cache
        return jwks_data

    def _get_signing_key(self, header: dict[str, Any]) -> Any:
        """Get the signing key for the token based on its algorithm and kid."""
        alg = header.get("alg", "HS256")
        kid = header.get("kid")

        if not self.algorithms or alg not in self.algorithms:
            raise TokenValidationError(f"Algorithm {alg} not allowed")

        # Symmetric key (HS256)
        if alg.startswith("HS"):
            if self.secret_key is None:
                raise TokenValidationError("No secret key configured for symmetric signing")
            return self.secret_key

        # Asymmetric key (RS256/RS384/RS512)
        if self.jwks_url is None:
            raise TokenValidationError("JWKS URL not configured for asymmetric signing")

        jwks = self._fetch_jwks()
        keys = jwks.get("keys", [])

        if kid:
            matching_keys = [k for k in keys if k.get("kid") == kid]
        else:
            matching_keys = keys

        if not matching_keys:
            raise TokenValidationError(f"No signing key found for kid={kid}")

        return self._convert_jwk_to_pem(matching_keys[0], alg)

    @staticmethod
    def _convert_jwk_to_pem(jwk: dict[str, Any], alg: str) -> str:
        """Convert a JWK to PEM format for PyJWT."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        if jwk.get("kty") != "RSA":
            raise TokenValidationError(f"Unsupported key type: {jwk.get('kty')}")

        n = int.from_bytes(jwt.api_jwk._decode_base64(jwk["n"]), "big")  # noqa: SLF001
        e = int.from_bytes(jwt.api_jwk._decode_base64(jwk["e"]), "big")  # noqa: SLF001

        # Build RSA public key from components
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

        rsa_numbers = RSAPublicNumbers(e, n)
        public_key = rsa_numbers.public_key()
        pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return pem.decode("utf-8")

    def extract_user(self, metadata: dict[str, str]) -> User:
        """Extract user identity from gRPC metadata.

        Expects metadata key 'authorization' with value 'Bearer <token>'.
        """
        auth_header = metadata.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise TokenValidationError("Missing or invalid Authorization header")

        token = auth_header[7:]  # Remove 'Bearer ' prefix
        return self.validate_token(token)

    def validate_token(self, token: str) -> User:
        """Validate an OIDC access token and extract user attributes.

        Validates:
        - Signature (JWKS or secret key)
        - Expiration (exp claim)
        - Issuer (iss claim)
        - Audience (aud claim)

        Extracts:
        - sub, name, email from standard claims
        - role, facility, machine_tags from custom claims
        """
        try:
            # Decode header to get kid and algorithm without full verification
            unverified_header = jwt.get_unverified_header(token)
            signing_key = self._get_signing_key(unverified_header)

            # Decode and verify with all standard checks
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                    "verify_sub": True,
                },
            )

        except jwt.ExpiredSignatureError:
            raise TokenValidationError("Token has expired")
        except jwt.InvalidIssuerError:
            raise TokenValidationError(f"Invalid issuer: expected {self.issuer}")
        except jwt.InvalidAudienceError:
            raise TokenValidationError(f"Invalid audience: expected {self.audience}")
        except jwt.InvalidTokenError as e:
            raise TokenValidationError(f"Invalid token: {e}")

        return self._parse_claims(payload)

    def _parse_claims(self, payload: dict[str, Any]) -> User:
        """Parse JWT claims into a User object."""
        return User(
            sub=payload.get("sub", ""),
            name=payload.get("name", payload.get("preferred_username", "")),
            email=payload.get("email"),
            role=payload.get("role", "viewer"),
            facility=payload.get("facility"),
            machine_tags=payload.get("machine_tags", []),
        )

    def clear_jwks_cache(self) -> None:
        """Clear the JWKS cache (useful for testing)."""
        self._jwks_cache = None
        self._jwks_expires_at = 0.0


def create_test_auth_manager(secret_key: str = "test-secret-key") -> AuthManager:
    """Create an AuthManager configured for testing with HS256."""
    return AuthManager(
        issuer="https://test.auth.example.com",
        audience="linuxcnc-fleet",
        secret_key=secret_key,
        algorithms=["HS256"],
    )


def create_test_token(claims: dict[str, Any], secret_key: str = "test-secret-key") -> str:
    """Create a test JWT for unit tests."""
    now = int(time.time())
    payload = {
        "exp": now + 3600,  # 1 hour expiry
        "iss": "https://test.auth.example.com",
        "aud": "linuxcnc-fleet",
        **claims,
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")
