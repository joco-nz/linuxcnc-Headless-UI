# UI Enhancements: Token Issuance & Auto-Renewal

## Current Architecture (Before Changes)

```
fleet_ui/server.py          Gateway (gRPC only)          Sidecar
─────────────────           ────────────────────         ───────
FleetApp                    FleetServiceServicer         LinuxCncSidecar
  │                           │                            │
  ▼                           ▼                            ▼
FleetClient                 AuthManager                  gRPC server
  │                           │                            │
  ▼                           ▼                            │
AioBearerAuthInterceptor    JWT validation               │
  │                           │                            │
  └── static token ──────────┴────────────────────────────┘
```

**Key facts:**
- Gateway is **gRPC-only** — no HTTP endpoints exist
- `AioBearerAuthInterceptor` stores token as a fixed string in `__init__` (line 78)
- Retry logic only handles `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED` — **not** `UNAUTHENTICATED`
- FleetClient creates interceptors once at channel creation time (lines 407, 454, 459)
- When token expires, every gRPC call fails with `UNAUTHENTICATED` and the UI silently stops working

---

## Plan: Option 3 — Proactive + Reactive Token Renewal

### Phase 1: Gateway HTTP endpoint for token issuance ✅ COMPLETE

**File:** `gateway/server.py` — add embedded aiohttp server alongside gRPC
- `TokenIssuanceServicer` class with AND/OR security model
- New route: `POST /api/auth/token?role=<role>&sub=<sub>`
- Issues JWT using gateway's own `AuthManager.secret_key`
- Validates requested role against allowed roles list (admin requires explicit flag)
- Token TTL set to 900 seconds (15 minutes) — shorter-lived for safety, reactive renewal handles re-auth

**File:** `gateway/cli.py` — add new args
- `--http-port` — separate HTTP port for token issuance (default: not set, skip HTTP server)
- `--allowed-roles` — comma-separated roles that can be issued via this endpoint (default: `viewer,operator`)
- `--token-ttl` — TTL for auto-issued tokens in seconds (default: 900 = 15 min)
- `--allow-admin-token` — if set, allows `?role=admin` on the HTTP endpoint
- `--allowed-subjects` — comma-separated `sub` values that can request tokens (pre-registration, default: `fleet-ui`)
- `--allowed-ips` — comma-separated IPs allowed to request tokens (default: `127.0.0.1,::1`)
- `--permissive` — if set, use OR mode (IP or subject match is sufficient); otherwise AND mode (both required)

### Phase 2: FleetClient supports token refresh ✅ COMPLETE

**File:** `fleet_client/auth.py` — make interceptor token mutable
- Added `refresh_token(new_token)` method to `AioBearerAuthInterceptor` that updates `self._token`
- Same for `BearerAuthInterceptor` (sync version)

**File:** `fleet_client/client.py` — add `refresh_token()` method
- Stores gateway interceptor (`_gateway_interceptor`) for tracking
- `refresh_token(new_token)` closes gateway channel, all machine channels, and resets them to None
- Next use of any channel recreates it with the new token via fresh interceptor
- Preserves: `_closed` flag, `_gateway_address`, `_tls_enabled`, `_machine_channel_ttl`
- Logs token refresh details (old/new token prefix + channels closed count)

### Phase 3: FleetApp proactive + reactive renewal ✅ COMPLETE

**File:** `fleet_ui/server.py` — FleetApp enhancements
- **Proactive**: background task checks token expiry every 30s. If `exp - now < 300` (5 minutes), fetches new token from gateway's `/api/auth/token` via HTTP and calls `client.refresh_token(new_token)`
- **Reactive**: when any gRPC call returns `UNAUTHENTICATED`, catches it, fetches new token, refreshes client, retries the original call once
- `_fetch_token()`: HTTP POST to gateway with machine_id + roles; stores result in `_token` and computes `_token_expiry`
- `_start_proactive_refresh()`: scheduled task running every 30s while app is active
- `_refresh_and_retry(operation)`: fetches token, calls `client.refresh_token()`, awaits operation again
- `_grpc_call_with_retry(operation, fallback=None)`: wrapper that catches UNAUTHENTICATED and triggers reactive retry
- All FleetApp gRPC methods wrapped: `discover_machines`, `get_status`, `get_machine_info`, `set_mode`, `send_mdi`, `load_program`, `broadcast_load_program`, `list_programs`, `control`, `list_hal`, `read_hal_pin`, `write_hal_pin`, `get_errors`
- `/api/auth/status` endpoint: returns `{has_token, is_connected, connecting, token_expiry}` for UI polling
- `handle_index` auto-fetch flow: when no `--token`, renders "Connecting to gateway..." banner; inline JS polls `/api/auth/status`; Python side calls gateway HTTP token endpoint on startup if missing
- CLI: added `--http-port` arg (default 50053) for gateway HTTP port

**Tests:** `tests/test_fleet_ui.py` — 18 new tests
- `TestTokenRefresh`: test_proactive_refresh_fetches_token, test_proactive_no_refresh_if_not_expired, test_proactive_no_refresh_if_no_token
- `TestReactiveRetry`: test_grpc_call_unauthenticated_triggers_refresh, test_grpc_call_unavailable_reraises
- `TestAuthStatusEndpoint`: test_auth_status_returns_has_token_true, test_auth_status_returns_has_token_false, test_auth_status_returns_connecting_flag, test_auth_status_returns_token_expiry
- `TestAutoConnectFlow`: test_handle_connect_starts_proactive_refresh

**File:** `gateway/cli.py` — CLI args
- Added 1 new argument: `--http-port` (default 50053) for gateway HTTP port

---

## Phase 1 Deliverables

| Component         | Files                                      | Tests                      | Status |
| ----------------- | ------------------------------------------ | -------------------------- | ------ |
| Token servicer    | `gateway/server.py` — `TokenIssuanceServicer` (58 lines) | 35 (test_token_issuance.py — NEW) | ✅      |
| HTTP route        | `gateway/server.py` — `_handle_auth_token`, `_handle_auth_token_wrapper` | —                          | ✅      |
| CLI args          | `gateway/cli.py` — 7 new arguments         | 18 (test_gateway_cli.py appended) | ✅      |

### Key implementation notes

- `TokenIssuanceServicer._check_security()` implements AND mode (default) and OR mode (`permissive=True`)
- AND mode: rejects if IP not in list OR subject not in list (first failure wins)
- OR mode: rejects only if both IP and subject are unknown
- Role validation: checks against `allowed_roles` list; admin requires explicit `allow_admin_token` flag
- Token issued with `AuthManager.secret_key` via PyJWT HS256, contains `iss`, `aud`, `sub`, `role`, `iat`, `exp` claims
- `run_gateway_server()` now accepts `http_port` and optional security params; starts aiohttp alongside gRPC in same process
- HTTP server cleaned up on SIGINT/SIGTERM via asyncio event loop

---

## Phase 2 Deliverables

| Component         | Files                                      | Tests                      | Status |
| ----------------- | ------------------------------------------ | -------------------------- | ------ |
| Interceptor refresh | `fleet_client/auth.py` — `refresh_token()` on both interceptor classes | 15 (test_fleet_client_auth.py appended) | ✅      |
| FleetClient refresh | `fleet_client/client.py` — `refresh_token()` method + `_gateway_interceptor` storage | 17 (test_fleet_client.py appended) | ✅      |

### Key implementation notes

- Both `BearerAuthInterceptor` and `AioBearerAuthInterceptor` have `refresh_token(new_token)` that updates `self._token`
- FleetClient stores `_gateway_interceptor` for tracking the active gateway interceptor
- `FleetClient.refresh_token(new_token)`:
  - Updates `self._token` to new value
  - Closes gateway channel (if exists) and sets `_gateway_channel = None`, `_gateway_stub = None`
  - Closes all cached machine channels and clears the cache dict
  - Logs: `"Token refreshed: <old_prefix> -> <new_prefix> (closed N channels)"`
  - Raises `RuntimeError("Client is closed")` if client is already closed
- Next use of any channel (`_ensure_gateway_channel()` or `_get_or_create_machine_channel()`) recreates it with a fresh interceptor containing the new token
- Preserves all settings: `_gateway_address`, `_tls_enabled`, `_machine_channel_ttl`
- Handles injected channels gracefully (no-op on gateway channel when externally injected)

---

## Phase 5 Deliverables

| Component         | Files                                      | Tests                      | Status |
| ----------------- | ------------------------------------------ | -------------------------- | ------ |
| E2E integration   | `tests/test_integration_e2e.py` (6 tests)  | 6 (test_integration_e2e.py — NEW) | ✅      |

### Test classes

- `TestFleetAppReactiveRenewal`: test_grpc_call_with_retry_fetches_token, test_grpc_call_with_retry_raises_when_http_unavailable (2 tests)
- `TestFleetAppProactiveRenewal`: test_proactive_refresh_fetches_from_gateway_http (1 test)
- `TestFleetAppAutoFetch`: test_auto_fetch_initializes_client_with_gateway_token (1 test)
- `TestE2EActiveSession`: test_full_session_lifecycle_issue_use_expire_renew_continue, test_proactive_renewal_with_admin_token_continues_working (2 tests)

### Key implementation notes

- Full stack fixture: sidecar + gateway with HTTP token issuance (`allow_admin_token=True`, `allowed_roles=["viewer", "operator", "admin"]`)
- Reactive renewal: FleetApp's `_grpc_call_with_retry()` catches UNAUTHENTICATED, calls `_fetch_token()`, calls `client.refresh_token()`, retries operation
- Proactive renewal: FleetApp's `_start_proactive_refresh()` background task calls `_fetch_token()` every 30s when near-expiry
- Auto-fetch: FleetApp with empty token calls `_fetch_token()` on startup to get JWT from gateway HTTP endpoint
- E2E lifecycle: issues admin token → discovers machines → waits for expiry → reactive renewal fetches new token → continues working
- `discover_machines()` swallows exceptions and returns `[]` on error (correct UI behavior — shows empty fleet rather than crashing)

---

## Final Decisions

All choices have been made. The following configuration is locked in:

| Setting | Value | Rationale |
|---------|-------|-----------|
| **Token TTL** (auto-issued) | 900 seconds (15 minutes) | Shorter-lived for safety; reactive renewal handles re-auth on expiry |
| **Proactive threshold** | 300 seconds (5 minutes before expiry) | Reasonable margin to avoid any gRPC failures from token expiration |
| **Security model** | AND mode default, `--permissive` for OR mode | Defense-in-depth in production; flexibility in development |
| **Allowed roles** | `viewer,operator` by default | Admin requires explicit `--allow-admin-token` flag |
| **UNAUTHENTICATED recovery** | Retry once after refresh | If it fails again, the token is bad and something else is wrong |

### Combined security model: AND mode with OR opt-in

The HTTP token endpoint supports both IP whitelisting and pre-registration, combined as follows:

- **AND mode (default)** — request accepted only if source IP is in `--allowed-ips` AND requested `sub` is in `--allowed-subjects`
- **OR mode (`--permissive` flag)** — request accepted if either condition is met

Example:
```bash
# Production — strict AND mode
fleet-gateway --http-port 50053 \
    --allowed-ips 127.0.0.1,::1 \
    --allowed-subjects fleet-ui,trixie-dev-linuxcnc

# Development — permissive OR mode
fleet-gateway --http-port 50053 \
    --allowed-ips 127.0.0.1,::1 \
    --permissive
```

---

## Phases to Work Through

| Phase | What | Depends On | Tests |
|-------|------|-----------|-------|
| **1** ✅ | Gateway HTTP endpoint (`/api/auth/token`) with AND/OR security model, role validation, TTL=900s | None (standalone) | `test_gateway.py` — token issuance, IP check, subject check, AND mode, OR mode, role enforcement |
| **2** ✅ | FleetClient token refresh: mutable interceptor + `refresh_token()` method on client | Phase 1 (gateway must be issuing tokens) | `test_fleet_client_auth.py` — interceptor refresh, client refresh propagates to all channels |
| **3** ✅ | FleetApp proactive renewal (every 30s, threshold=300s) + reactive renewal (on UNAUTHENTICATED) + UI auto-fetch flow (no config form when token pre-provided) | Phase 2 (client must support refresh) | `test_fleet_ui.py` — proactive polling, reactive retry, auto-fetch on startup, "Connecting..." banner |
| **4** ✅ | Integration: end-to-end test of token lifecycle (issue → use → expire → renew → continue working) | Phases 1–3 | `tests/test_integration_renewal.py` — 13 tests across 6 classes: HTTP issuance, gRPC with tokens, expiry detection, FleetClient refresh propagation, proactive task runs, reactive retry, AND/OR security model |
| **5** ✅ | FleetApp end-to-end integration: reactive renewal via `_grpc_call_with_retry()`, proactive renewal with real HTTP fetch, auto-fetch startup flow, full session lifecycle | Phases 1–4 | `tests/test_integration_e2e.py` — 6 tests across 4 classes: FleetApp reactive renewal, proactive refresh from gateway HTTP, auto-fetch initialization, full session lifecycle with expiry/renewal |

---

## Summary of Files Affected

| File | Changes |
|------|---------|
| `gateway/server.py` | Add aiohttp HTTP server alongside gRPC, new `/api/auth/token` endpoint |
| `gateway/cli.py` | New args: `--http-port`, `--allowed-roles`, `--token-ttl`, `--allow-admin-token`, `--allowed-subjects`, `--allowed-ips`, `--permissive` |
| `fleet_client/auth.py` | Add `refresh_token()` to both interceptor classes |
| `fleet_client/client.py` | Add `refresh_token()` method, handle UNAUTHENTICATED in retry logic |
| `fleet_ui/server.py` | FleetApp: proactive polling + reactive renewal; handle_index: auto-fetch flow; new `/api/auth/status` endpoint |
| `tests/test_gateway.py` | New tests for HTTP token endpoint |
| `tests/test_fleet_client_auth.py` | Tests for interceptor refresh |
| `tests/test_fleet_ui.py` | Tests for FleetApp renewal, auto-fetch flow |
| `tests/test_integration_renewal.py` | Phase 4: 13 integration tests for token lifecycle (issue → use → expire → renew → continue) |
| `tests/test_integration_e2e.py` | Phase 5: 6 E2E integration tests for FleetApp reactive/proactive/auto-fetch/session lifecycle |
