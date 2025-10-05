# Dahua Event Stream Timeout Debugging

## Problem
Event stream from `eventManager.cgi` endpoint disconnects with timeout errors every few minutes (observed: 1-5 minute intervals, NOT the expected 5-minute timeout pattern).

## Production Logs Analysis (Oct 4, 2025)
- Errors occurred at erratic intervals: 1-4 minutes (NOT a consistent 5-minute pattern)
- Most common interval: 2-3 minutes
- Pattern suggests multiple timeout sources racing to disconnect
- Errors continued sporadically throughout the day (14:07 - 22:37)

## Failed Attempts

### Attempt 1: Heartbeat Mechanism (Commit d68ed17)
**Approach:** Added application-level heartbeat polling
**Result:** Failed - errors persisted
**Reverted:** Yes

### Attempt 2: Disable Request Timeout (Commit b2c7aa4)
**Approach:** Set `timeout=aiohttp.ClientTimeout(total=None, sock_read=None)` on the request
**Result:** Failed - errors persisted and became more erratic
**Reverted:** Yes

### Attempt 3: Disable Session Timeout (Commit 029f44d)
**Approach:** Set `timeout=ClientTimeout(total=None)` on ClientSession
**Result:** Not tested - reverted before deployment
**Reverted:** Yes

## Root Cause Analysis

### Research: Scrypted Amcrest Plugin
Examined Scrypted's working implementation:
- Repository: https://github.com/koush/scrypted
- File: `plugins/amcrest/src/amcrest-api.ts`

**Key Finding:**
```typescript
stream.socket.setKeepAlive(true);
```

Scrypted enables **TCP keepalive at the socket level**, NOT application-level timeout manipulation.

### Why This Works
- TCP keepalive sends periodic probes at the OS/network layer
- Keeps connection "active" from the network stack perspective
- Prevents idle timeout from ever triggering
- No need to disable timeouts - they just never fire

## Final Solution (Commit 077b316)

### Implementation
Created socket factory with TCP keepalive enabled:

```python
def create_keepalive_socket(addr_info):
    """Create a socket with TCP keepalive enabled (matching Scrypted's approach)."""
    family, type_, proto, _, _ = addr_info
    sock = socket.socket(family=family, type=type_, proto=proto)

    # Enable TCP keepalive (equivalent to stream.socket.setKeepAlive(true) in Scrypted)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    # Configure keepalive parameters to prevent connection timeout
    if hasattr(socket, 'TCP_KEEPIDLE'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)  # Start after 60s idle
    if hasattr(socket, 'TCP_KEEPINTVL'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 60)  # Probe every 60s
    if hasattr(socket, 'TCP_KEEPCNT'):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)    # 5 failed probes

    return sock

# Applied to TCPConnector
connector = TCPConnector(
    enable_cleanup_closed=True,
    ssl=SSL_CONTEXT,
    socket_factory=create_keepalive_socket
)
```

### Why This Should Work
1. **Single change** - only TCP keepalive, no other modifications
2. **Proven approach** - directly matches Scrypted's working implementation
3. **OS-level solution** - works at network layer, not application layer
4. **No timeout manipulation** - keeps original timeout behavior, just prevents timeout from triggering

## Testing Plan
1. Deploy `__init__.py` and `client.py` (baseline) to production
2. Restart Home Assistant
3. Monitor logs for minimum 1 hour (preferably 4+ hours)
4. Success criteria: No timeout errors in event stream

## Notes
- Previous attempts tried to fix at wrong layer (application vs. network)
- Scrypted's success proves TCP keepalive is the correct approach
- All timeout-disabling changes were counterproductive and have been removed
