"""Tests for OIDC authentication interceptor."""

import dataclasses
from unittest.mock import Mock, patch

import pytest


@dataclasses.dataclass(frozen=True)
class MockUser:
    """Mock user object for testing."""
    sub: str
    name: str = "Test User"
    email: str = "test@example.com"
    role: str = "viewer"
    facility: str = None
    machine_tags: list = None

    def __post_init__(self):
        if self.machine_tags is None:
            object.__setattr__(self, 'machine_tags', [])


def test_extract_token_with_bearer():
    from linuxcnc_fleet.auth import _extract_token
    metadata = [("authorization", "Bearer my-token")]
    token = _extract_token(metadata)
    assert token == "my-token"


def test_extract_token_without_bearer_prefix():
    from linuxcnc_fleet.auth import _extract_token
    metadata = [("authorization", "my-token")]
    token = _extract_token(metadata)
    assert token == "my-token"


def test_extract_token_no_authorization():
    from linuxcnc_fleet.auth import _extract_token
    metadata = [("some-key", "some-value")]
    token = _extract_token(metadata)
    assert token is None


def test_extract_token_empty_metadata():
    from linuxcnc_fleet.auth import _extract_token
    token = _extract_token([])
    assert token is None


def test_get_metadata_found():
    from linuxcnc_fleet.auth import _get_metadata
    metadata = [("key1", "value1"), ("key2", "value2")]
    value = _get_metadata(metadata, "key1")
    assert value == "value1"


def test_get_metadata_not_found():
    from linuxcnc_fleet.auth import _get_metadata
    metadata = [("key1", "value1")]
    value = _get_metadata(metadata, "key2")
    assert value is None


def test_auth_context_defaults():
    from linuxcnc_fleet.auth import AuthContext
    ctx = AuthContext(sub="user123")
    assert ctx.sub == "user123"
    assert ctx.name == ""
    assert ctx.email == ""
    assert ctx.role == "viewer"
    assert ctx.facility is None
    assert ctx.machine_tags == []


def test_auth_context_all_fields():
    from linuxcnc_fleet.auth import AuthContext
    ctx = AuthContext(
        sub="user123",
        name="Test User",
        email="test@example.com",
        role="admin",
        facility="facility1",
        machine_tags=["tag1", "tag2"],
    )
    assert ctx.sub == "user123"
    assert ctx.name == "Test User"
    assert ctx.email == "test@example.com"
    assert ctx.role == "admin"
    assert ctx.facility == "facility1"
    assert ctx.machine_tags == ["tag1", "tag2"]


def test_auth_context_frozen():
    from linuxcnc_fleet.auth import AuthContext
    ctx = AuthContext(sub="user123")
    with pytest.raises(Exception):
        ctx.name = "New Name"


def test_auth_interceptor_no_token():
    from linuxcnc_fleet.auth import AuthInterceptor
    
    mock_extractor = Mock(return_value=MockUser(sub="test-user"))
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = []
    mock_request.request_streaming = False
    mock_request.response_streaming = False
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    assert not mock_continuation.called
    # Should return a fail handler instead of passing through to continuation
    assert callable(result.unary_unary)


def test_auth_interceptor_with_valid_token():
    from linuxcnc_fleet.auth import AuthInterceptor, AuthContext
    
    mock_user = MockUser(
        sub="user123",
        name="Test User",
        email="test@example.com",
        role="admin",
        facility="facility1",
        machine_tags=["tag1"],
    )
    mock_extractor = Mock(return_value=mock_user)
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = [("authorization", "Bearer valid-token")]
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    assert mock_continuation.called
    enhanced_request = mock_continuation.call_args[0][0]
    assert isinstance(enhanced_request.auth_context, AuthContext)
    assert enhanced_request.auth_context.sub == "user123"
    assert enhanced_request.auth_context.name == "Test User"
    assert enhanced_request.auth_context.email == "test@example.com"
    assert enhanced_request.auth_context.role == "admin"
    assert enhanced_request.auth_context.facility == "facility1"
    assert enhanced_request.auth_context.machine_tags == ["tag1"]


def test_auth_interceptor_with_invalid_token():
    from linuxcnc_fleet.auth import AuthInterceptor
    
    def raise_exception(metadata):
        raise ValueError("Invalid token")
    
    interceptor = AuthInterceptor(raise_exception)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = [("authorization", "Bearer invalid-token")]
    mock_request.request_streaming = False
    mock_request.response_streaming = False
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    assert not mock_continuation.called
    # Should return a fail handler instead of passing through to continuation
    assert callable(result.unary_unary)


def test_auth_interceptor_no_token_streaming_rpc():
    from linuxcnc_fleet.auth import AuthInterceptor
    
    mock_extractor = Mock(return_value=MockUser(sub="test-user"))
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = []
    mock_request.request_streaming = False
    mock_request.response_streaming = True
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    assert not mock_continuation.called
    assert callable(result.unary_stream)


def test_auth_interceptor_no_token_server_streaming():
    from linuxcnc_fleet.auth import AuthInterceptor
    
    mock_extractor = Mock(return_value=MockUser(sub="test-user"))
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = []
    mock_request.request_streaming = True
    mock_request.response_streaming = False
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    assert not mock_continuation.called
    assert callable(result.stream_unary)


def test_auth_interceptor_preserves_request_attributes():
    from linuxcnc_fleet.auth import AuthInterceptor
    
    mock_extractor = Mock(return_value=MockUser(sub="test"))
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = [("authorization", "Bearer token")]
    mock_request.some_attribute = "some_value"
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    enhanced_request = mock_continuation.call_args[0][0]
    assert enhanced_request.some_attribute == "some_value"


def test_auth_decorator_role_hierarchy():
    from linuxcnc_fleet.auth import AuthDecorator, AuthContext
    
    decorator = AuthDecorator("admin")
    
    # Admin should pass
    admin_ctx = AuthContext(sub="admin", role="admin")
    result = decorator._check_auth(admin_ctx)
    assert result is None
    
    # Maintainer should fail
    maintainer_ctx = AuthContext(sub="maintainer", role="maintainer")
    result = decorator._check_auth(maintainer_ctx)
    assert result is not None
    assert "insufficient" in result
    
    # Operator should fail
    operator_ctx = AuthContext(sub="operator", role="operator")
    result = decorator._check_auth(operator_ctx)
    assert result is not None


def test_auth_decorator_require_control():
    from linuxcnc_fleet.auth import AuthDecorator, AuthContext
    
    decorator = AuthDecorator("operator")
    
    # Operator should pass
    operator_ctx = AuthContext(sub="operator", role="operator")
    result = decorator._check_auth(operator_ctx)
    assert result is None
    
    # Viewer should fail
    viewer_ctx = AuthContext(sub="viewer", role="viewer")
    result = decorator._check_auth(viewer_ctx)
    assert result is not None


def test_auth_decorator_require_admin():
    from linuxcnc_fleet.auth import AuthDecorator, AuthContext
    
    decorator = AuthDecorator("admin")
    
    # Admin should pass
    admin_ctx = AuthContext(sub="admin", role="admin")
    result = decorator._check_auth(admin_ctx)
    assert result is None
    
    # Programmer should fail
    programmer_ctx = AuthContext(sub="programmer", role="programmer")
    result = decorator._check_auth(programmer_ctx)
    assert result is not None


def test_create_auth_interceptor():
    from linuxcnc_fleet.auth import create_auth_interceptor
    
    mock_extractor = Mock()
    interceptor = create_auth_interceptor(mock_extractor)
    
    assert isinstance(interceptor, type(object()))  # Just verify it's an object
    assert interceptor.user_extractor == mock_extractor


def test_auth_interceptor_handles_role_enum():
    from linuxcnc_fleet.auth import AuthInterceptor, AuthContext
    
    class RoleEnum:
        value = 4
    
    mock_user = Mock(
        sub="user123",
        name="Test User",
        email="test@example.com",
        role=RoleEnum(),
        facility=None,
        machine_tags=[],
    )
    
    mock_extractor = Mock(return_value=mock_user)
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = [("authorization", "Bearer token")]
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    enhanced_request = mock_continuation.call_args[0][0]
    assert enhanced_request.auth_context.role == "4"


def test_auth_interceptor_handles_missing_attributes():
    from linuxcnc_fleet.auth import AuthInterceptor, AuthContext
    
    class MinimalUser:
        sub = "user123"
    
    mock_extractor = Mock(return_value=MinimalUser())
    interceptor = AuthInterceptor(mock_extractor)
    
    mock_continuation = Mock()
    mock_request = Mock()
    mock_request.invocation_metadata = [("authorization", "Bearer token")]
    
    result = interceptor.intercept_service(mock_continuation, mock_request)
    
    enhanced_request = mock_continuation.call_args[0][0]
    assert enhanced_request.auth_context.sub == "user123"
    assert enhanced_request.auth_context.name == ""
    assert enhanced_request.auth_context.email == ""
    assert enhanced_request.auth_context.role == "viewer"
