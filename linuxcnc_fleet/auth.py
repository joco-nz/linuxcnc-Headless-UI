"""OIDC authentication interceptor for FleetService gRPC server."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Optional, Sequence, Tuple

import grpc

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class AuthContext:
    """Extracted user context from OIDC token."""
    sub: str
    name: str = ""
    email: str = ""
    role: str = "viewer"
    facility: Optional[str] = None
    machine_tags: list[str] = dataclasses.field(default_factory=list)
    authenticated: bool = True


def _extract_token(metadata: Sequence[Tuple[str, str]]) -> Optional[str]:
    """Extract Bearer token from gRPC metadata."""
    for key, value in metadata:
        if key == "authorization":
            if value.startswith("Bearer "):
                return value[7:]
            return value
    return None


def _get_metadata(metadata: Sequence[Tuple[str, str]], key: str) -> Optional[str]:
    """Get a single metadata value by key."""
    for k, v in metadata:
        if k == key:
            return v
    return None


class AuthInterceptor(grpc.ServerInterceptor):
    """gRPC server interceptor that validates OIDC tokens from metadata."""

    def __init__(self, user_extractor: Callable[[dict], Any]) -> None:
        """Initialize with a user extraction callable.

        Args:
            user_extractor: Callable that takes metadata dict and returns user object
                           with sub, name, email, role, facility, machine_tags attributes
        """
        self.user_extractor = user_extractor

    def intercept_service(self, continuation, handler_request):
        """Intercept the service call to extract and validate user context."""
        token = _extract_token(handler_request.invocation_metadata)

        if not token:
            log.warning("No authorization token found in request — rejecting")
            return self._make_fail_handler("No authorization token provided", handler_request)

        try:
            user = self.user_extractor({"authorization": f"Bearer {token}"})
            role = getattr(user, 'role', 'viewer')
            if hasattr(role, 'value'):
                role = str(role.value)
            else:
                role = str(role)
            auth_ctx = AuthContext(
                sub=getattr(user, 'sub', 'unknown'),
                name=getattr(user, 'name', ''),
                email=getattr(user, 'email', ''),
                role=role,
                facility=getattr(user, 'facility', None),
                machine_tags=list(getattr(user, 'machine_tags', [])),
            )
        except Exception as e:
            log.warning("Token validation failed — rejecting request: %s", e)
            return self._make_fail_handler(f"Token validation failed: {e}", handler_request)

        original_handler_request = handler_request
        
        class AuthEnhancedRequest:
            def __init__(self, wrapped, auth_context):
                self._wrapped = wrapped
                self.auth_context = auth_context
            
            def __getattr__(self, name):
                return getattr(self._wrapped, name)
        
        enhanced_request = AuthEnhancedRequest(original_handler_request, auth_ctx)
        
        return continuation(enhanced_request)

    @staticmethod
    def _make_fail_handler(message: str, handler_request) -> Any:
        """Return a stub handler that aborts with UNAUTHENTICATED for all RPC types."""
        if not handler_request.request_streaming and not handler_request.response_streaming:
            def _fail(request, context):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
            return grpc.unary_unary_rpc_method_handler(_fail)
        elif not handler_request.request_streaming and handler_request.response_streaming:
            def _fail_stream(request, context):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
            return grpc.unary_stream_rpc_method_handler(_fail_stream)
        elif handler_request.request_streaming and not handler_request.response_streaming:
            async def _fail_unary(req_iter, context):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
            return grpc.stream_unary_rpc_method_handler(_fail_unary)
        else:
            async def _fail_streaming(req_iter, context):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, message)
            return grpc.stream_stream_rpc_method_handler(_fail_streaming)


class AuthDecorator:
    """Decorator to add auth checks to specific RPC methods."""

    def __init__(self, required_role: str = "admin") -> None:
        """Initialize with minimum required role.

        Args:
            required_role: Minimum role required for the decorated method
        """
        self.required_role = required_role
        self.role_hierarchy = {
            "viewer": 0,
            "operator": 1,
            "programmer": 2,
            "maintainer": 3,
            "admin": 4,
        }

    def _check_auth(self, auth_ctx: AuthContext) -> Optional[str]:
        """Check if auth context meets required role.

        Returns error message if access denied, None if allowed.
        """
        user_level = self.role_hierarchy.get(auth_ctx.role, 0)
        required_level = self.role_hierarchy.get(self.required_role, 0)
        
        if user_level < required_level:
            return f"Role '{auth_ctx.role}' insufficient, requires '{self.required_role}'"
        
        return None

    def require_control(self):
        """Decorator for control operations requiring at least operator role."""
        return AuthDecorator("operator")

    def require_admin(self):
        """Decorator for admin operations requiring admin role."""
        return AuthDecorator("admin")


def create_auth_interceptor(user_extractor: Callable[[dict], Any]) -> AuthInterceptor:
    """Create an authentication interceptor.

    Args:
        user_extractor: Callable that extracts user from metadata dict

    Returns:
        Configured AuthInterceptor instance
    """
    return AuthInterceptor(user_extractor)
