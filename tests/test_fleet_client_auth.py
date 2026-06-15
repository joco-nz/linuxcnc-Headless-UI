"""Tests for fleet_client.auth — OIDC bearer token gRPC client interceptor."""

from unittest.mock import MagicMock, Mock, patch

import grpc
import pytest

from fleet_client.auth import (
    AioBearerAuthInterceptor,
    BearerAuthInterceptor,
    _ClientCallDetails,
    create_auth_interceptor,
    create_aio_auth_interceptor,
)


class TestBearerAuthInterceptorInit:

    def test_stores_token(self):
        interceptor = BearerAuthInterceptor("my-token")
        assert interceptor._token == "my-token"

    def test_empty_token(self):
        interceptor = BearerAuthInterceptor("")
        assert interceptor._token == ""


class TestCreateAuthInterceptor:

    def test_returns_bearer_interceptor(self):
        result = create_auth_interceptor("some-token")
        assert isinstance(result, BearerAuthInterceptor)

    def test_forwards_token(self):
        result = create_auth_interceptor("my-token")
        assert result._token == "my-token"


class TestClientCallDetails:

    def test_stores_all_fields(self):
        details = _ClientCallDetails(
            method="/foo.Bar",
            timeout=30.0,
            metadata=[("key", "val")],
            credentials=None,
            wait_for_ready=True,
            compression=grpc.Compression.Gzip,
        )
        assert details.method == "/foo.Bar"
        assert details.timeout == 30.0
        assert details.metadata == [("key", "val")]
        assert details.credentials is None
        assert details.wait_for_ready is True
        assert details.compression == grpc.Compression.Gzip

    def test_none_timeout(self):
        details = _ClientCallDetails(
            method="/test", timeout=None, metadata=[],
            credentials=None, wait_for_ready=False,
            compression=grpc.Compression.NoCompression,
        )
        assert details.timeout is None

    def test_none_metadata(self):
        details = _ClientCallDetails(
            method="/test", timeout=None, metadata=None,
            credentials=None, wait_for_ready=False,
            compression=grpc.Compression.NoCompression,
        )
        assert details.metadata is None


class _FakeClientCallDetails:
    """Minimal grpc.ClientCallDetails implementation for testing."""

    def __init__(self, method, timeout, metadata, credentials, wait_for_ready, compression):
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials
        self.wait_for_ready = wait_for_ready
        self.compression = compression


class TestInterceptUnaryUnary:

    def _make_details(self, metadata=None):
        return _FakeClientCallDetails(
            method="/test.Method", timeout=10.0,
            metadata=metadata, credentials=None,
            wait_for_ready=False, compression=grpc.Compression.NoCompression,
        )

    def test_adds_bearer_metadata(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        # Verify continuation was called with new details
        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        assert isinstance(new_details, _ClientCallDetails)
        metadata = new_details.metadata
        assert ("authorization", "Bearer token123") in metadata

    def test_preserves_existing_metadata(self):
        interceptor = BearerAuthInterceptor("token123")
        existing_meta = [("x-custom", "value")]
        details = self._make_details(metadata=existing_meta)
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert ("x-custom", "value") in metadata
        assert ("authorization", "Bearer token123") in metadata
        assert len(metadata) == 2

    def test_no_existing_metadata(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details(metadata=None)
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert len(metadata) == 1
        assert ("authorization", "Bearer token123") in metadata

    def test_forwards_request(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=MagicMock())
        expected_req = object()

        interceptor.intercept_unary_unary(mock_continuation, details, expected_req)

        call_args = mock_continuation.call_args
        assert call_args[0][1] is expected_req

    def test_returns_continuation_result(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details()
        expected_response = Mock(spec=grpc.Call)
        mock_continuation = MagicMock(return_value=expected_response)

        result = interceptor.intercept_unary_unary(mock_continuation, details, "req")

        assert result is expected_response

    def test_empty_token_produces_bearer_space(self):
        interceptor = BearerAuthInterceptor("")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert ("authorization", "Bearer ") in metadata

    def test_passes_through_timeout(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        assert new_details.timeout == 10.0

    def test_passes_through_method(self):
        interceptor = BearerAuthInterceptor("token123")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=MagicMock())

        interceptor.intercept_unary_unary(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        assert new_details.method == "/test.Method"


class TestInterceptUnaryStream:

    def _make_details(self, metadata=None):
        return _FakeClientCallDetails(
            method="/test.Stream", timeout=30.0,
            metadata=metadata, credentials=None,
            wait_for_ready=False, compression=grpc.Compression.NoCompression,
        )

    def test_adds_bearer_metadata_streaming(self):
        interceptor = BearerAuthInterceptor("stream-token")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=iter([]))

        result = interceptor.intercept_unary_stream(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert ("authorization", "Bearer stream-token") in metadata

    def test_preserves_existing_metadata_streaming(self):
        interceptor = BearerAuthInterceptor("stream-token")
        existing_meta = [("x-trace", "abc")]
        details = self._make_details(metadata=existing_meta)
        mock_continuation = MagicMock(return_value=iter([]))

        interceptor.intercept_unary_stream(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert ("x-trace", "abc") in metadata
        assert ("authorization", "Bearer stream-token") in metadata
        assert len(metadata) == 2

    def test_returns_continuation_result_streaming(self):
        interceptor = BearerAuthInterceptor("stream-token")
        details = self._make_details()
        expected_iter = iter([Mock(), Mock()])
        mock_continuation = MagicMock(return_value=expected_iter)

        result = interceptor.intercept_unary_stream(mock_continuation, details, "req")

        assert result is expected_iter

    def test_passes_through_timeout_streaming(self):
        interceptor = BearerAuthInterceptor("stream-token")
        details = self._make_details()
        mock_continuation = MagicMock(return_value=iter([]))

        interceptor.intercept_unary_stream(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        assert new_details.timeout == 30.0

    def test_no_existing_metadata_streaming(self):
        interceptor = BearerAuthInterceptor("stream-token")
        details = self._make_details(metadata=None)
        mock_continuation = MagicMock(return_value=iter([]))

        interceptor.intercept_unary_stream(mock_continuation, details, "req")

        call_args = mock_continuation.call_args
        new_details = call_args[0][0]
        metadata = new_details.metadata
        assert len(metadata) == 1
        assert ("authorization", "Bearer stream-token") in metadata
