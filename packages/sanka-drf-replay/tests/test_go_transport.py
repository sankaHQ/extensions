# SPDX-License-Identifier: Apache-2.0
"""Loopback response timeout classification without processes or network waits."""

from unittest.mock import MagicMock, Mock

import pytest

from sanka_drf_replay import go
from sanka_drf_replay.replay import ReplayError


@pytest.mark.parametrize(
    "phase,error,expired,category",
    [
        ("send", TimeoutError(), False, "infrastructure_failure"),
        ("headers", TimeoutError(), False, "candidate_failure"),
        ("body", TimeoutError(), False, "candidate_failure"),
        ("headers", OSError("local transport unavailable"), False, "infrastructure_failure"),
        ("body", TimeoutError(), True, "infrastructure_failure"),
    ],
)
def test_go_response_timeout_is_repairable(tmp_path, monkeypatch, phase, error, expired, category):
    process = Mock(pid=12345)
    process.poll.return_value = None
    monkeypatch.setattr(go.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(go.os, "killpg", Mock())
    monkeypatch.setattr(go.signal, "signal", Mock())
    monkeypatch.setattr(go.threading, "Timer", Mock())
    monkeypatch.setattr(go.threading, "Event", lambda: Mock(is_set=lambda: expired))
    reservation = MagicMock()
    reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
    monkeypatch.setattr(go.socket, "socket", Mock(return_value=reservation))
    monkeypatch.setattr(go.socket, "create_connection", Mock(return_value=MagicMock()))
    connection = Mock()
    response = connection.getresponse.return_value
    response.info.return_value.get_all.return_value = []
    response.read.return_value = b"{}"
    target = {"send": connection.request, "headers": connection.getresponse, "body": response.read}
    target[phase].side_effect = error
    monkeypatch.setattr(go.http.client, "HTTPConnection", Mock(return_value=connection))

    with pytest.raises(ReplayError) as caught:
        go.run_candidate(
            tmp_path,
            {"method": "GET", "path": "/items/"},
            [],
            str(tmp_path / "db.sqlite3"),
            "REPLAY_DB",
            tmp_path,
        )
    assert caught.value.category == category
    assert caught.value.__cause__ is error
