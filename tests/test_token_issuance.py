"""Tests for HTTP token issuance — TokenIssuanceServicer and CLI args."""

import pytest

from gateway.auth import AuthManager, TokenValidationError, create_test_auth_manager
from gateway.policies import PolicyEngine, create_test_policy_engine
from gateway.server import TokenIssuanceServicer


def _make_servicer(**kwargs):
    auth = kwargs.pop("auth_manager", create_test_auth_manager())
    policy = kwargs.pop("policy_engine", create_test_policy_engine())
    return TokenIssuanceServicer(auth_manager=auth, policy_engine=policy, **kwargs)


# ── Default security (AND mode) ───────────────────────────────────────────────

class TestTokenIssuanceDefaultAndMode:
    def test_issue_token_default_success(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
        )
        result = servicer.issue_token(role="viewer", sub="fleet-ui", client_ip="127.0.0.1")
        assert "token" in result
        assert "expires_in" in result
        assert result["expires_in"] == 900

    def test_issue_token_rejects_unknown_sub(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
        )
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="viewer", sub="unknown-app", client_ip="127.0.0.1")
        assert "subject not pre-registered" in str(exc_info.value)

    def test_issue_token_rejects_unknown_ip(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
        )
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="viewer", sub="fleet-ui", client_ip="10.0.0.5")
        assert "source IP not in allowed list" in str(exc_info.value)

    def test_issue_token_rejects_both_unknown(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
        )
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="viewer", sub="unknown", client_ip="10.0.0.5")
        assert "source IP not in allowed list" in str(exc_info.value)


# ── OR / permissive mode ─────────────────────────────────────────────────────

class TestTokenIssuanceOrMode:
    def test_permissive_ip_match_only(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
            permissive=True,
        )
        result = servicer.issue_token(role="viewer", sub="unknown-app", client_ip="127.0.0.1")
        assert "token" in result

    def test_permissive_sub_match_only(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
            permissive=True,
        )
        result = servicer.issue_token(role="viewer", sub="fleet-ui", client_ip="10.0.0.5")
        assert "token" in result

    def test_permissive_rejects_both_unknown(self):
        servicer = _make_servicer(
            allowed_ips=["127.0.0.1"],
            allowed_subjects=["fleet-ui"],
            permissive=True,
        )
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="viewer", sub="unknown", client_ip="10.0.0.5")
        assert "IP not allowed and subject not pre-registered" in str(exc_info.value)


# ── Role validation ───────────────────────────────────────────────────────────

class TestTokenIssuanceRoleValidation:
    def test_viewer_role_allowed(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator"])
        result = servicer.issue_token(role="viewer")
        assert "token" in result

    def test_operator_role_allowed(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator"])
        result = servicer.issue_token(role="operator")
        assert "token" in result

    def test_programmer_role_rejected_by_default(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator"])
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="programmer")
        assert "not in allowed roles" in str(exc_info.value)

    def test_admin_role_rejected_without_flag(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator"])
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="admin")
        assert "--allow-admin-token" in str(exc_info.value)

    def test_admin_role_allowed_with_flag(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator", "admin"], allow_admin_token=True)
        result = servicer.issue_token(role="admin")
        assert "token" in result

    def test_default_roles_are_viewer_operator(self):
        servicer = _make_servicer()
        result = servicer.issue_token(role="viewer")
        assert "token" in result
        with pytest.raises(TokenValidationError):
            servicer.issue_token(role="programmer")


# ── TTL configuration ────────────────────────────────────────────────────────

class TestTokenIssuanceTTL:
    def test_default_ttl_is_900(self):
        servicer = _make_servicer()
        result = servicer.issue_token()
        assert result["expires_in"] == 900

    def test_custom_ttl(self):
        servicer = _make_servicer(token_ttl=1800)
        result = servicer.issue_token()
        assert result["expires_in"] == 1800

    def test_custom_ttl_3600(self):
        servicer = _make_servicer(token_ttl=3600)
        result = servicer.issue_token()
        assert result["expires_in"] == 3600


# ── Default subjects and IPs ─────────────────────────────────────────────────

class TestTokenIssuanceDefaults:
    def test_default_subject_fleet_ui(self):
        servicer = _make_servicer()
        result = servicer.issue_token(sub="fleet-ui", client_ip="127.0.0.1")
        assert "token" in result

    def test_default_ip_localhost_ipv4(self):
        servicer = _make_servicer()
        result = servicer.issue_token(sub="fleet-ui", client_ip="127.0.0.1")
        assert "token" in result

    def test_default_ip_localhost_ipv6(self):
        servicer = _make_servicer()
        result = servicer.issue_token(sub="fleet-ui", client_ip="::1")
        assert "token" in result

    def test_default_ip_rejects_non_localhost(self):
        servicer = _make_servicer()
        with pytest.raises(TokenValidationError):
            servicer.issue_token(sub="fleet-ui", client_ip="192.168.1.10")


# ── Token content validation ─────────────────────────────────────────────────

class TestTokenContent:
    def test_token_contains_role_claim(self, monkeypatch):
        import jwt as pyjwt
        servicer = _make_servicer(allowed_roles=["viewer", "operator"])
        result = servicer.issue_token(role="operator")
        decoded = pyjwt.decode(result["token"], options={"verify_signature": False})
        assert decoded["role"] == "operator"

    def test_token_contains_sub_claim(self, monkeypatch):
        import jwt as pyjwt
        servicer = _make_servicer(allowed_subjects=["my-app"])
        result = servicer.issue_token(sub="my-app")
        decoded = pyjwt.decode(result["token"], options={"verify_signature": False})
        assert decoded["sub"] == "my-app"

    def test_token_contains_issuer(self, monkeypatch):
        import jwt as pyjwt
        auth = create_test_auth_manager()
        servicer = _make_servicer(auth_manager=auth)
        result = servicer.issue_token()
        decoded = pyjwt.decode(result["token"], options={"verify_signature": False})
        assert decoded["iss"] == auth.issuer

    def test_token_contains_audience(self, monkeypatch):
        import jwt as pyjwt
        auth = create_test_auth_manager()
        servicer = _make_servicer(auth_manager=auth)
        result = servicer.issue_token()
        decoded = pyjwt.decode(result["token"], options={"verify_signature": False})
        assert decoded["aud"] == auth.audience

    def test_token_has_expiration(self, monkeypatch):
        import jwt as pyjwt
        servicer = _make_servicer(token_ttl=600)
        result = servicer.issue_token()
        decoded = pyjwt.decode(result["token"], options={"verify_signature": False})
        assert "exp" in decoded
        assert "iat" in decoded
        assert decoded["exp"] - decoded["iat"] == 600


# ── Multiple allowed values ──────────────────────────────────────────────────

class TestTokenIssuanceMultipleAllowed:
    def test_multiple_subjects(self):
        servicer = _make_servicer(allowed_subjects=["fleet-ui", "app1", "app2"])
        assert "token" in servicer.issue_token(sub="app1")
        assert "token" in servicer.issue_token(sub="app2")

    def test_multiple_ips(self):
        servicer = _make_servicer(allowed_ips=["127.0.0.1", "192.168.1.50"])
        assert "token" in servicer.issue_token(client_ip="192.168.1.50")

    def test_multiple_roles(self):
        servicer = _make_servicer(allowed_roles=["viewer", "operator", "programmer"])
        assert "token" in servicer.issue_token(role="programmer")


# ── Error codes ───────────────────────────────────────────────────────────────

class TestTokenIssuanceErrorCodes:
    def test_ip_rejection_error_code(self):
        servicer = _make_servicer(allowed_ips=["127.0.0.1"])
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(client_ip="10.0.0.5")
        assert exc_info.value.error_code == 403

    def test_subject_rejection_error_code(self):
        servicer = _make_servicer(allowed_subjects=["fleet-ui"])
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(sub="unknown", client_ip="127.0.0.1")
        assert exc_info.value.error_code == 403

    def test_role_rejection_error_code(self):
        servicer = _make_servicer()
        with pytest.raises(TokenValidationError) as exc_info:
            servicer.issue_token(role="admin")
        assert exc_info.value.error_code == 403


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestTokenIssuanceEdgeCases:
    def test_empty_allowed_subjects_rejects_all(self):
        servicer = _make_servicer(allowed_subjects=[])
        with pytest.raises(TokenValidationError):
            servicer.issue_token(sub="anything", client_ip="127.0.0.1")

    def test_empty_allowed_ips_rejects_all(self):
        servicer = _make_servicer(allowed_ips=[])
        with pytest.raises(TokenValidationError):
            servicer.issue_token(client_ip="anywhere")

    def test_default_sub_is_fleet_ui(self):
        servicer = _make_servicer()
        result = servicer.issue_token(sub="fleet-ui", client_ip="127.0.0.1")
        assert "token" in result

    def test_default_role_is_viewer(self):
        servicer = _make_servicer(allowed_roles=["viewer"])
        result = servicer.issue_token()
        assert "token" in result
