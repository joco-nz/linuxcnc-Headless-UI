"""OIDC token validation and user extraction for the fleet gateway."""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import ssl
import time
from collections.abc import Mapping
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
        secret_keys: Optional[Mapping[str, str]] = None,
    ):
        if algorithms is None:
            algorithms = ["RS256", "HS256"]

        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.secret_key = secret_key
        self.algorithms = algorithms
        self._symmetric_keys: dict[str, str] = dict(secret_keys) if secret_keys else {}
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_expires_at: float = 0.0

    def add_symmetric_key(self, kid: str, key: str) -> None:
        """Add a symmetric signing key for HS256 token verification.

        Useful for key rotation — adds a new key while keeping the old one
        active so tokens signed with either key are accepted.
        """
        self._symmetric_keys[kid] = key

    def remove_symmetric_key(self, kid: str) -> None:
        """Remove a symmetric signing key by kid."""
        self._symmetric_keys.pop(kid, None)

    def _get_symmetric_key(self, kid: Optional[str]) -> str:
        """Resolve the symmetric key to use for HS256 verification.

        Priority: exact kid match in secret_keys → first key in dict if no kid
                  → legacy secret_key (backward compat).
        Returns the single key string when a specific kid is requested,
        or raises TokenValidationError if no key can be resolved.
        """
        if self._symmetric_keys:
            if kid and kid in self._symmetric_keys:
                return self._symmetric_keys[kid]
            if not kid:
                # No kid specified — use the first available symmetric key
                # (typically the oldest/primary key)
                return next(iter(self._symmetric_keys.values()))

        if self.secret_key is not None:
            return self.secret_key

        raise TokenValidationError("No secret key configured for symmetric signing")

    def _get_symmetric_keys_for_verification(self) -> list[str]:
        """Return all symmetric keys for multi-key verification during token decode.

        Used in validate_token when we have multiple symmetric keys —
        PyJWT will try each one until the signature validates.
        """
        if self._symmetric_keys:
            return list(self._symmetric_keys.values())
        if self.secret_key is not None:
            return [self.secret_key]
        return []

    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch JWKS document from the configured endpoint with caching."""
        import urllib.request

        if self.jwks_url is None:
            raise TokenValidationError("JWKS URL not configured")

        # Return cached result if still valid (cache for 5 minutes)
        if self._jwks_cache and time.time() < self._jwks_expires_at:
            return self._jwks_cache

        if self.jwks_url.startswith("https://"):
            ssl_context = ssl.create_default_context()
        else:
            log.warning(
                "JWKS URL uses HTTP (not HTTPS) — token validation is vulnerable to MitM attacks: %s",
                self.jwks_url,
            )
            ssl_context = None

        req = urllib.request.Request(self.jwks_url)
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
            jwks_data = json.loads(response.read())

        self._jwks_cache = jwks_data
        self._jwks_expires_at = time.time() + 300  # 5 minute cache
        return jwks_data

    def _get_signing_key(self, header: dict[str, Any]) -> Any:
        """Get the signing key for the token based on its algorithm and kid.

        Returns a single key string for backward-compatible jwt.decode().
        Multi-key HS256 is handled manually in validate_token via retry loop.
        """
        alg = header.get("alg", "HS256")
        kid = header.get("kid")

        if not self.algorithms or alg not in self.algorithms:
            raise TokenValidationError(f"Algorithm {alg} not allowed")

        # Symmetric key (HS256) — resolve to single key; multi-key handled in validate_token
        if alg.startswith("HS"):
            return self._get_symmetric_key(kid)

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
    def _jwk_decode_base64(urlsafe_b64: str) -> bytes:
        """Decode base64url-encoded data with proper padding."""
        padding = 4 - len(urlsafe_b64) % 4
        if padding != 4:
            urlsafe_b64 += "=" * padding
        return base64.urlsafe_b64decode(urlsafe_b64)

    @staticmethod
    def _convert_jwk_to_pem(jwk: dict[str, Any], alg: str) -> str:
        """Convert a JWK to PEM format for PyJWT."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

        if jwk.get("kty") != "RSA":
            raise TokenValidationError(f"Unsupported key type: {jwk.get('kty')}")

        n = int.from_bytes(AuthManager._jwk_decode_base64(jwk["n"]), "big")
        e = int.from_bytes(AuthManager._jwk_decode_base64(jwk["e"]), "big")

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
        - Signature (JWKS or secret key; tries all symmetric keys on failure)
        - Expiration (exp claim)
        - Issuer (iss claim)
        - Audience (aud claim)

        Extracts:
        - sub, name, email from standard claims
        - role, facility, machine_tags from custom claims
        """
        # Decode header to get kid and algorithm without full verification
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise TokenValidationError(f"Invalid token: {e}")

        alg = unverified_header.get("alg", "HS256")

        decode_opts: dict[str, Any] = {
            "algorithms": self.algorithms,
            "audience": self.audience,
            "issuer": self.issuer,
            "options": {
                "require": ["exp", "iss", "aud", "sub"],
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "verify_sub": True,
            },
        }

        # Try multi-key HS256 verification if we have multiple symmetric keys
        if alg.startswith("HS") and len(self._symmetric_keys) > 1:
            for key in self._symmetric_keys.values():
                try:
                    payload = jwt.decode(token, key, **decode_opts)
                    return self._parse_claims(payload)
                except jwt.ExpiredSignatureError:
                    raise TokenValidationError("Token has expired")
                except jwt.InvalidIssuerError:
                    raise TokenValidationError(f"Invalid issuer: expected {self.issuer}")
                except jwt.InvalidAudienceError:
                    raise TokenValidationError(f"Invalid audience: expected {self.audience}")
                except jwt.InvalidSignatureError:
                    continue  # Try next key
                except jwt.InvalidTokenError as e:
                    raise TokenValidationError(f"Invalid token: {e}")

            # All symmetric keys failed — fall back to legacy secret_key if configured
            if self.secret_key is not None:
                try:
                    payload = jwt.decode(token, self.secret_key, **decode_opts)
                    return self._parse_claims(payload)
                except jwt.InvalidSignatureError:
                    pass  # Legacy key also failed
                except jwt.ExpiredSignatureError:
                    raise TokenValidationError("Token has expired")
                except jwt.InvalidIssuerError:
                    raise TokenValidationError(f"Invalid issuer: expected {self.issuer}")
                except jwt.InvalidAudienceError:
                    raise TokenValidationError(f"Invalid audience: expected {self.audience}")
                except jwt.InvalidTokenError as e:
                    raise TokenValidationError(f"Invalid token: {e}")

            raise TokenValidationError("Invalid token signature")

        # Single-key path (backward compatible)
        try:
            signing_key = self._get_signing_key(unverified_header)
            payload = jwt.decode(token, signing_key, **decode_opts)
        except jwt.ExpiredSignatureError:
            raise TokenValidationError("Token has expired")
        except jwt.InvalidIssuerError:
            raise TokenValidationError(f"Invalid issuer: expected {self.issuer}")
        except jwt.InvalidAudienceError:
            raise TokenValidationError(f"Invalid audience: expected {self.audience}")
        except jwt.InvalidSignatureError:
            # Multi-key HS256 fallback: try legacy secret_key if all configured keys failed.
            # This handles the case where a token was signed with an old key that's
            # no longer in secret_keys but is still the legacy secret_key.
            if self.secret_key is not None:
                try:
                    payload = jwt.decode(token, self.secret_key, **decode_opts)
                except jwt.InvalidSignatureError:
                    raise TokenValidationError("Invalid token signature")
                except jwt.ExpiredSignatureError:
                    raise TokenValidationError("Token has expired")
                except jwt.InvalidIssuerError:
                    raise TokenValidationError(f"Invalid issuer: expected {self.issuer}")
                except jwt.InvalidAudienceError:
                    raise TokenValidationError(f"Invalid audience: expected {self.audience}")
                except jwt.InvalidTokenError as e:
                    raise TokenValidationError(f"Invalid token: {e}")
            else:
                raise TokenValidationError("Invalid token signature")

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


_TEST_SECRET_KEY = os.environ.get(
    "TEST_SECRET_KEY",
    "test-secret-key-for-testing-32bytes!",  # noqa: S105 — test-only, never used in production
)


def create_test_auth_manager(
    secret_key: str | None = None,
    secret_keys: dict[str, str] | None = None,
) -> AuthManager:
    """Create an AuthManager configured for testing with HS256.

    Args:
        secret_key: Optional override; defaults to TEST_SECRET_KEY env var or fallback.
        secret_keys: Optional mapping of kid → key for multi-key rotation testing.
    """
    return AuthManager(
        issuer="https://test.auth.example.com",
        audience="linuxcnc-fleet",
        secret_key=secret_key or _TEST_SECRET_KEY,
        algorithms=["HS256"],
        secret_keys=secret_keys,
    )


def create_test_token(
    claims: dict[str, Any],
    secret_key: str | None = None,
    kid: str | None = None,
) -> str:
    """Create a test JWT for unit tests.

    Args:
        claims: JWT payload claims to include.
        secret_key: Optional override; defaults to TEST_SECRET_KEY env var or fallback.
        kid: Optional key ID header for multi-key testing.
    """
    now = int(time.time())
    payload = {
        "exp": now + 3600,  # 1 hour expiry
        "iss": "https://test.auth.example.com",
        "aud": "linuxcnc-fleet",
        **claims,
    }
    headers = {"kid": kid} if kid else None
    return jwt.encode(
        payload,
        secret_key or _TEST_SECRET_KEY,
        algorithm="HS256",
        headers=headers,
    )
