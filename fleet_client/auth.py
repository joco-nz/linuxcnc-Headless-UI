"""OIDC auth interceptor for gRPC client — attaches bearer token to every RPC call."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import grpc

log = logging.getLogger(__name__)


class BearerAuthInterceptor(grpc.UnaryUnaryClientInterceptor):
    """gRPC client interceptor that adds OIDC bearer token to all unary calls.
    
    This interceptor automatically attaches the authorization header
    (Bearer <token>) to every outgoing gRPC call's metadata.
    """

    def __init__(self, token: str) -> None:
        """Initialize with an OIDC access token.
        
        Args:
            token: OIDC bearer token string
        """
        self._token = token

    def intercept_unary_unary(self, continuation, client_call_details, request):
        """Intercept unary-unary calls to add authorization metadata."""
        metadata = list(client_call_details.metadata or [])
        metadata.append(("authorization", f"Bearer {self._token}"))

        new_details = _ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=metadata,
            credentials=None,
            wait_for_ready=False,
            compression=grpc.Compression.NoCompression,
        )

        return continuation(new_details, request)

    def intercept_unary_stream(self, continuation, client_call_details, request):
        """Intercept unary-server-streaming calls to add authorization metadata."""
        metadata = list(client_call_details.metadata or [])
        metadata.append(("authorization", f"Bearer {self._token}"))

        new_details = _ClientCallDetails(
            method=client_call_details.method,
            timeout=client_call_details.timeout,
            metadata=metadata,
            credentials=None,
            wait_for_ready=False,
            compression=grpc.Compression.NoCompression,
        )

        return continuation(new_details, request)


class _ClientCallDetails(
    grpc.ClientCallDetails
):
    """Implementation of ClientCallDetails for interceptor use."""

    def __init__(
        self,
        method: str,
        timeout: Optional[float],
        metadata: Optional[list[tuple[str, str]]],
        credentials: Optional[grpc.CallCredentials],
        wait_for_ready: bool,
        compression: grpc.Compression,
    ) -> None:
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials
        self.wait_for_ready = wait_for_ready
        self.compression = compression


def create_auth_interceptor(token: str) -> BearerAuthInterceptor:
    """Create a bearer auth interceptor.
    
    Args:
        token: OIDC bearer token string
        
    Returns:
        Configured BearerAuthInterceptor instance
    """
    return BearerAuthInterceptor(token)
