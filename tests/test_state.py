import queue
from unittest.mock import patch

import app
import state as state_module


def test_full_sse_client_is_closed_and_removed():
    q = queue.Queue(maxsize=1)
    q.put_nowait('stale')
    old_clients = state_module.state.sse_clients
    state_module.state.sse_clients = [q]
    try:
        state_module.sse_broadcast('status', {'ok': True})
        assert state_module.state.sse_clients == []
        assert q.get_nowait() is state_module.SSE_CLOSE
    finally:
        state_module.state.sse_clients = old_clients


def test_sse_connection_limit_preserves_api_threads():
    q = queue.Queue()
    old_clients = app.state.sse_clients
    app.state.sse_clients = [q]
    try:
        with patch.dict('os.environ', {'USDB_MAX_SSE_CLIENTS': '1'}, clear=False):
            response = app.app.test_client().get('/stream')
        assert response.status_code == 503
    finally:
        app.state.sse_clients = old_clients
