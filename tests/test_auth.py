"""Tests for OIDC token validation and user extraction."""

import time

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
