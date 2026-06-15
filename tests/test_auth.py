"""Tests for OIDC token validation and user extraction."""

import json
import logging
import time
from unittest.mock import MagicMock, patch

import jwt
import pytest

from gateway.auth import (
    AuthManager,
    TokenValidationError,
    User,
    create_test_auth_manager,
    create_test_token,
)


class TestUser:
    """Tests for the frozen User dataclass."""

    def test_defaults(self):
        user = User(sub="user-1", name="Test User")
        assert user.sub == "user-1"
        assert user.name == "Test User"
        assert user.email is None
        assert user.role == "viewer"
        assert user.facility is None
        assert user.machine_tags == []

    def test_all_fields(self):
        user = User(
            sub="user-2",
            name="Full Name",
            email="full@example.com",
            role="admin",
            facility="shop-1",
            machine_tags=["mill", "cnc"],
        )
        assert user.sub == "user-2"
        assert user.name == "Full Name"
        assert user.email == "full@example.com"
        assert user.role == "admin"
        assert user.facility == "shop-1"
        assert user.machine_tags == ["mill", "cnc"]

    def test_frozen_cannot_modify(self):
        user = User(sub="u1", name="U1")
        with pytest.raises(Exception):
            user.role = "admin"


class TestTokenValidationError:
    def test_default_error_code(self):
        err = TokenValidationError("test error")
        assert err.error_code == 401

    def test_custom_error_code(self):
        err = TokenValidationError("test", error_code=403)
        assert err.error_code == 403


class TestAuthManager:
    """Tests for AuthManager token validation and user extraction."""

    def test_create_test_auth_manager(self):
        auth = create_test_auth_manager()
        assert auth.issuer == "https://test.auth.example.com"
        assert auth.audience == "linuxcnc-fleet"
        assert auth.algorithms == ["HS256"]

    def test_create_test_auth_manager_custom_secret(self):
        auth = create_test_auth_manager(secret_key="custom-secret-key-for-test-32bytes!!")
        assert auth.secret_key == "custom-secret-key-for-test-32bytes!!"

    def test_validate_token_success(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1", "name": "Test User"})
        user = auth.validate_token(token)
        assert user.sub == "user-1"
        assert user.name == "Test User"

    def test_validate_token_with_role(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "u2", "name": "RoleUser", "role": "admin"})
        user = auth.validate_token(token)
        assert user.role == "admin"

    def test_validate_token_with_facility(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "u3", "name": "FacUser", "facility": "shop-1"})
        user = auth.validate_token(token)
        assert user.facility == "shop-1"

    def test_validate_token_with_machine_tags(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "u4", "name": "TagUser", "machine_tags": ["mill", "lathe"]})
        user = auth.validate_token(token)
        assert user.machine_tags == ["mill", "lathe"]

    def test_validate_token_with_email(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "u5", "name": "Email User", "email": "test@example.com"})
        user = auth.validate_token(token)
        assert user.email == "test@example.com"

    def test_validate_token_expired(self):
        auth = create_test_auth_manager()
        past_exp = int(time.time()) - 3600
        payload = {
            "exp": past_exp,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "expired-user",
        }
        token = jwt.encode(payload, "test-secret-key-for-testing-32bytes!", algorithm="HS256")
        with pytest.raises(TokenValidationError, match="expired"):
            auth.validate_token(token)

    def test_validate_token_invalid_issuer(self):
        auth = create_test_auth_manager()
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://evil.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "evil-user",
        }
        token = jwt.encode(payload, "test-secret-key-for-testing-32bytes!", algorithm="HS256")
        with pytest.raises(TokenValidationError, match="Invalid issuer"):
            auth.validate_token(token)

    def test_validate_token_invalid_audience(self):
        auth = create_test_auth_manager()
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.auth.example.com",
            "aud": "wrong-audience",
            "sub": "aud-user",
        }
        token = jwt.encode(payload, "test-secret-key-for-testing-32bytes!", algorithm="HS256")
        with pytest.raises(TokenValidationError, match="Invalid audience"):
            auth.validate_token(token)

    def test_validate_token_wrong_secret(self):
        auth = create_test_auth_manager(secret_key="correct-secret-key-for-test-32bytes!!!")
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "wrong-secret-user",
        }
        token = jwt.encode(payload, "wrong-secret-key-for-test-32bytes!!!!", algorithm="HS256")
        with pytest.raises(TokenValidationError):
            auth.validate_token(token)

    def test_validate_token_missing_claims(self):
        auth = create_test_auth_manager()
        # Missing required claims (exp, iss, aud, sub)
        payload = {"sub": "no-exp"}
        token = jwt.encode(payload, "test-secret-key-for-testing-32bytes!", algorithm="HS256")
        with pytest.raises(TokenValidationError):
            auth.validate_token(token)

    def test_extract_user_success(self):
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1", "name": "Test User"})
        metadata = {"authorization": f"Bearer {token}"}
        user = auth.extract_user(metadata)
        assert user.sub == "user-1"

    def test_extract_user_missing_authorization(self):
        auth = create_test_auth_manager()
        with pytest.raises(TokenValidationError, match="Missing or invalid"):
            auth.extract_user({})

    def test_extract_user_invalid_prefix(self):
        auth = create_test_auth_manager()
        with pytest.raises(TokenValidationError, match="Missing or invalid"):
            auth.extract_user({"authorization": "Token some-token"})

    def test_clear_jwks_cache(self):
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://test.example.com/.well-known/jwks.json",
        )
        auth._jwks_cache = {"keys": []}
        auth._jwks_expires_at = time.time() + 100
        auth.clear_jwks_cache()
        assert auth._jwks_cache is None
        assert auth._jwks_expires_at == 0.0

    def test_extract_user_from_test_token(self):
        """Integration: create_test_token → extract_user works end-to-end."""
        auth = create_test_auth_manager()
        token = create_test_token({
            "sub": "user-123",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "role": "operator",
            "facility": "shop-2",
            "machine_tags": ["mill"],
        })
        user = auth.extract_user({"authorization": f"Bearer {token}"})
        assert user.sub == "user-123"
        assert user.name == "Jane Doe"
        assert user.email == "jane@example.com"
        assert user.role == "operator"
        assert user.facility == "shop-2"
        assert user.machine_tags == ["mill"]

    def test_validate_token_preferred_username_fallback(self):
        """Test preferred_username claim used as name fallback."""
        auth = create_test_auth_manager()
        token = create_test_token({
            "sub": "user-1",
            "preferred_username": "fallback-name",
        })
        user = auth.validate_token(token)
        assert user.name == "fallback-name"

    def test_validate_token_no_name_claim(self):
        """Test name defaults to empty string when no name or preferred_username."""
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1"})
        user = auth.validate_token(token)
        assert user.name == ""

    def test_validate_token_default_role(self):
        """Test role defaults to 'viewer' when not in claims."""
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1"})
        user = auth.validate_token(token)
        assert user.role == "viewer"

    def test_validate_token_default_facility(self):
        """Test facility defaults to None when not in claims."""
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1"})
        user = auth.validate_token(token)
        assert user.facility is None

    def test_validate_token_default_machine_tags(self):
        """Test machine_tags defaults to empty list when not in claims."""
        auth = create_test_auth_manager()
        token = create_test_token({"sub": "user-1"})
        user = auth.validate_token(token)
        assert user.machine_tags == []

    def test_different_secret_keys(self):
        """Test that different AuthManagers can use different secrets."""
        auth1 = create_test_auth_manager(secret_key="secret-key-one-for-testing-32bytes!!!")
        auth2 = create_test_auth_manager(secret_key="secret-key-two-for-testing-32bytes!!!!")

        token = create_test_token({"sub": "user-1"}, secret_key="secret-key-one-for-testing-32bytes!!!")

        # Token signed with secret-1 validates against auth1
        user1 = auth1.validate_token(token)
        assert user1.sub == "user-1"

        # Same token fails against auth2 (different secret)
        with pytest.raises(TokenValidationError):
            auth2.validate_token(token)

    def test_jwks_url_not_configured(self):
        """Test that JWKS fetch raises when URL not configured."""
        auth = AuthManager(issuer="https://test.example.com", audience="fleet")
        with pytest.raises(TokenValidationError, match="JWKS URL not configured"):
            auth._fetch_jwks()

    def test_algorithm_not_allowed(self):
        """Test that disallowed algorithm raises error."""
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            secret_key="key-for-testing-32bytes-minimum-length!!!",
            algorithms=["RS256"],  # Only RS256 allowed, no HS256
        )
        # Token with HS256 should fail because it's not in the allowed list
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, "key-for-testing-32bytes-minimum-length!!!", algorithm="HS256")
        with pytest.raises(TokenValidationError, match="not allowed"):
            auth.validate_token(token)

    def test_secret_key_not_configured_for_hs256(self):
        """Test that HS256 fails when no secret key is configured."""
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            algorithms=["HS256"],
        )
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, "some-secret-key-for-testing-32bytes!!!", algorithm="HS256")
        with pytest.raises(TokenValidationError, match="No secret key"):
            auth.validate_token(token)


class TestJWKSErrorPaths:
    """Tests for JWKS error paths in AuthManager."""

    @staticmethod
    def _make_rsa_jwk(private_key):
        from cryptography.hazmat.primitives import serialization
        import base64

        public_key = private_key.public_key()
        public_numbers = public_key.public_numbers()

        def encode_jwk_base64(value):
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

        n_bytes = public_numbers.n.to_bytes(
            (public_numbers.n.bit_length() + 7) // 8, "big"
        )
        e_bytes = public_numbers.e.to_bytes(
            (public_numbers.e.bit_length() + 7) // 8, "big"
        )

        return {
            "kty": "RSA",
            "kid": "test-key-1",
            "n": encode_jwk_base64(n_bytes),
            "e": encode_jwk_base64(e_bytes),
        }

    @staticmethod
    def _generate_rsa_keys():
        from cryptography.hazmat.primitives.asymmetric import rsa as rsa_primitives
        from cryptography.hazmat.primitives import serialization

        private_key = rsa_primitives.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()
        pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return private_key, pem.decode("utf-8")

    def test_rs256_no_jwks_url(self):
        """RS256 token fails when JWKS URL is not configured."""
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            algorithms=["RS256"],
        )
        private_key, public_pem = self._generate_rsa_keys()
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key-1"})
        with pytest.raises(TokenValidationError, match="JWKS URL not configured"):
            auth.validate_token(token)

    def test_no_matching_kid(self):
        """Token with unknown kid raises TokenValidationError."""
        private_key, public_pem = self._generate_rsa_keys()
        jwk = self._make_rsa_jwk(private_key)
        jwks_data = {"keys": [jwk]}

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = jwks_data
        auth._jwks_expires_at = time.time() + 300

        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "unknown-key"})
        with pytest.raises(TokenValidationError, match="No signing key found"):
            auth.validate_token(token)

    def test_non_rsa_key_type(self):
        """Non-RSA key type in JWKS raises TokenValidationError."""
        ec_jwk = {
            "kty": "EC",
            "kid": "ec-key-1",
            "crv": "P-256",
            "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
            "y": "x_FEzRu9mW6HLZLdNtwSBZPThXpsFwHFzeV1k2iiUwg",
        }

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = {"keys": [ec_jwk]}
        auth._jwks_expires_at = time.time() + 300

        private_key, public_pem = self._generate_rsa_keys()
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "ec-key-1"})
        with pytest.raises(TokenValidationError, match="Unsupported key type"):
            auth.validate_token(token)

    def test_jwks_fetch_http_error(self):
        """JWKS fetch raises on HTTP error from network."""
        import urllib.error

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )

        http_error = urllib.error.HTTPError(
            "https://example.com/.well-known/jwks.json",
            404,
            "Not Found",
            {},
            None,
        )
        with pytest.raises(urllib.error.URLError):
            auth._fetch_jwks()

    def test_jwks_fetch_https_creates_ssl_context(self):
        """JWKS fetch over HTTPS creates and uses an SSL context for certificate verification (H7)."""
        import ssl

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = None
        auth._jwks_expires_at = 0.0

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({"keys": []}).encode()
            mock_context_manager = MagicMock()
            mock_context_manager.__enter__ = MagicMock(return_value=mock_response)
            mock_context_manager.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_context_manager

            auth._fetch_jwks()

        call_args = mock_urlopen.call_args
        assert call_args is not None, "urlopen was not called"
        context_arg = call_args.kwargs.get("context", None) if hasattr(call_args, "kwargs") else None
        if context_arg is None and call_args.args:
            context_arg = call_args.args[1] if len(call_args.args) > 1 else None
        assert isinstance(context_arg, ssl.SSLContext), \
            f"Expected ssl.SSLContext for HTTPS JWKS URL, got {type(context_arg)}"

    def test_jwks_fetch_http_logs_warning(self, caplog):
        """JWKS fetch over HTTP logs a MitM warning and passes no SSL context (H7)."""
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="http://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = None
        auth._jwks_expires_at = 0.0

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({"keys": []}).encode()
            mock_context_manager = MagicMock()
            mock_context_manager.__enter__ = MagicMock(return_value=mock_response)
            mock_context_manager.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_context_manager

            with caplog.at_level(logging.WARNING, logger="gateway.auth"):
                auth._fetch_jwks()

        assert "MitM" in caplog.text or "HTTP" in caplog.text or "vulnerable" in caplog.text, \
            f"Expected MitM warning in logs, got: {caplog.text}"

        call_args = mock_urlopen.call_args
        assert call_args is not None, "urlopen was not called"
        context_arg = call_args.kwargs.get("context", None) if hasattr(call_args, "kwargs") else None
        if context_arg is None and call_args.args:
            context_arg = call_args.args[1] if len(call_args.args) > 1 else None
        assert context_arg is None, \
            f"Expected no SSL context for HTTP JWKS URL, got {type(context_arg)}"

    def test_jwks_cache_hit(self):
        """Second JWKS fetch returns cached data without network call."""
        private_key, public_pem = self._generate_rsa_keys()
        jwk = self._make_rsa_jwk(private_key)
        jwks_data = {"keys": [jwk]}

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = jwks_data
        auth._jwks_expires_at = time.time() + 300

        result1 = auth._fetch_jwks()
        result2 = auth._fetch_jwks()

        assert result1 is result2
        assert result1 == jwks_data

    def test_rs256_no_kid_uses_first_key(self):
        """RS256 token with no kid uses first matching key from JWKS."""
        private_key, public_pem = self._generate_rsa_keys()
        jwk = self._make_rsa_jwk(private_key)
        jwks_data = {"keys": [jwk]}

        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = jwks_data
        auth._jwks_expires_at = time.time() + 300

        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        user = auth.validate_token(token)
        assert user.sub == "user-1"

    def test_jwks_missing_keys_field(self):
        """JWKS without 'keys' field results in no matching keys."""
        auth = AuthManager(
            issuer="https://test.example.com",
            audience="fleet",
            jwks_url="https://example.com/.well-known/jwks.json",
            algorithms=["RS256"],
        )
        auth._jwks_cache = {"some_other_field": "value"}
        auth._jwks_expires_at = time.time() + 300

        private_key, public_pem = self._generate_rsa_keys()
        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://test.example.com",
            "aud": "fleet",
            "sub": "user-1",
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        with pytest.raises(TokenValidationError, match="No signing key found"):
            auth.validate_token(token)


class TestSymmetricKeyRotation:
    """Tests for HS256 multi-key support and rotation (H3)."""

    def test_multi_key_accepts_old_key(self):
        """Token signed with the old key is accepted when both keys are configured."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key, "new": new_key},
            algorithms=["HS256"],
        )

        token = create_test_token(
            {"sub": "user-1", "name": "OldKeyUser"},
            secret_key=old_key,
        )
        user = auth.validate_token(token)
        assert user.sub == "user-1"

    def test_multi_key_accepts_new_key(self):
        """Token signed with the new key is accepted when both keys are configured."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key, "new": new_key},
            algorithms=["HS256"],
        )

        token = create_test_token(
            {"sub": "user-2", "name": "NewKeyUser"},
            secret_key=new_key,
        )
        user = auth.validate_token(token)
        assert user.sub == "user-2"

    def test_multi_key_rejects_wrong_key(self):
        """Token signed with an unknown key is rejected even with multiple keys."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"
        wrong_key = "wrong-secret-key-for-testing-32bytes!!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key, "new": new_key},
            algorithms=["HS256"],
        )

        token = create_test_token(
            {"sub": "user-3"},
            secret_key=wrong_key,
        )
        with pytest.raises(TokenValidationError):
            auth.validate_token(token)

    def test_multi_key_with_kid_selects_correct_key(self):
        """Token with kid header selects the correct key from secret_keys."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key, "new": new_key},
            algorithms=["HS256"],
        )

        token = create_test_token(
            {"sub": "user-4"},
            secret_key=new_key,
            kid="new",
        )
        user = auth.validate_token(token)
        assert user.sub == "user-4"

    def test_add_symmetric_key_rotation(self):
        """Dynamically adding a key during rotation works."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-rotation-test-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key},
            algorithms=["HS256"],
        )

        # Old key works before adding new one
        token_old = create_test_token({"sub": "user-5"}, secret_key=old_key)
        user = auth.validate_token(token_old)
        assert user.sub == "user-5"

        # Add new key mid-rotation
        auth.add_symmetric_key("new", new_key)

        # Both keys should work now
        token_new = create_test_token({"sub": "user-6"}, secret_key=new_key)
        user = auth.validate_token(token_new)
        assert user.sub == "user-6"

        token_old2 = create_test_token({"sub": "user-7"}, secret_key=old_key)
        user = auth.validate_token(token_old2)
        assert user.sub == "user-7"

    def test_remove_symmetric_key(self):
        """Removing a key revokes tokens signed with it."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"old": old_key, "new": new_key},
            algorithms=["HS256"],
        )

        # Both keys work
        token_old = create_test_token({"sub": "user-8"}, secret_key=old_key)
        user = auth.validate_token(token_old)
        assert user.sub == "user-8"

        # Remove old key
        auth.remove_symmetric_key("old")

        # Old key should no longer work (no fallback to secret_key)
        with pytest.raises(TokenValidationError):
            auth.validate_token(token_old)

        # New key still works
        token_new = create_test_token({"sub": "user-9"}, secret_key=new_key)
        user = auth.validate_token(token_new)
        assert user.sub == "user-9"

    def test_secret_keys_only_no_legacy_key(self):
        """When only secret_keys is set (no legacy secret_key), keys still work."""
        key1 = "rotation-key-one-for-testing-32bytes!!!"
        key2 = "rotation-key-two-for-testing-32bytes!!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"k1": key1, "k2": key2},
            algorithms=["HS256"],
        )

        token1 = create_test_token({"sub": "user-10"}, secret_key=key1)
        user = auth.validate_token(token1)
        assert user.sub == "user-10"

        token2 = create_test_token({"sub": "user-11"}, secret_key=key2)
        user = auth.validate_token(token2)
        assert user.sub == "user-11"

    def test_secret_keys_only_rejects_unknown(self):
        """No legacy fallback when only secret_keys is configured."""
        key1 = "rotation-key-one-for-testing-32bytes!!!"
        wrong_key = "wrong-key-for-testing-32bytes-minimum-length!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"k1": key1},
            algorithms=["HS256"],
        )

        token = create_test_token({"sub": "user-12"}, secret_key=wrong_key)
        with pytest.raises(TokenValidationError):
            auth.validate_token(token)

    def test_create_test_auth_manager_with_secret_keys(self):
        """create_test_auth_manager accepts secret_keys parameter."""
        auth = create_test_auth_manager(
            secret_keys={"old": "test-old-key-32bytes-minimum!!!", "new": "test-new-key-32bytes-minimum!!!"},
        )
        assert "old" in auth._symmetric_keys
        assert "new" in auth._symmetric_keys

    def test_create_test_token_with_kid(self):
        """create_test_token accepts kid parameter for header."""
        token = create_test_token({"sub": "user-13"}, kid="my-key")
        header = jwt.get_unverified_header(token)
        assert header["kid"] == "my-key"

    def test_legacy_secret_key_fallback_in_multi_key(self):
        """Legacy secret_key is tried as last resort when all keys fail."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        new_key = "new-secret-key-for-testing-32bytes!!!"
        legacy_key = "legacy-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_key=legacy_key,
            secret_keys={"new": new_key},
            algorithms=["HS256"],
        )

        # Token signed with legacy key is accepted via fallback
        token = create_test_token({"sub": "user-14"}, secret_key=legacy_key)
        user = auth.validate_token(token)
        assert user.sub == "user-14"

    def test_expired_token_not_fallen_back(self):
        """Expired tokens raise immediately without trying fallback key."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        legacy_key = "legacy-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_key=legacy_key,
            secret_keys={"old": old_key},
            algorithms=["HS256"],
        )

        past_exp = int(time.time()) - 3600
        payload = {
            "exp": past_exp,
            "iss": "https://test.auth.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "expired-user",
        }
        token = jwt.encode(payload, old_key, algorithm="HS256")
        with pytest.raises(TokenValidationError, match="expired"):
            auth.validate_token(token)

    def test_invalid_issuer_not_fallen_back(self):
        """Invalid issuer tokens raise immediately without trying fallback key."""
        old_key = "old-secret-key-for-testing-32bytes!!!!"
        legacy_key = "legacy-secret-key-for-testing-32bytes!!!"

        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_key=legacy_key,
            secret_keys={"old": old_key},
            algorithms=["HS256"],
        )

        payload = {
            "exp": int(time.time()) + 3600,
            "iss": "https://evil.example.com",
            "aud": "linuxcnc-fleet",
            "sub": "evil-user",
        }
        token = jwt.encode(payload, old_key, algorithm="HS256")
        with pytest.raises(TokenValidationError, match="Invalid issuer"):
            auth.validate_token(token)

    def test_remove_nonexistent_key_is_noop(self):
        """Removing a non-existent kid is a no-op."""
        auth = AuthManager(
            issuer="https://test.auth.example.com",
            audience="linuxcnc-fleet",
            secret_keys={"k1": "key-one-32bytes-minimum-length!!!"},
            algorithms=["HS256"],
        )
        # Should not raise
        auth.remove_symmetric_key("nonexistent")
        assert "k1" in auth._symmetric_keys
