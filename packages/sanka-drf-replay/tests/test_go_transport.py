# SPDX-License-Identifier: Apache-2.0
"""Loopback response timeout classification without processes or network waits."""

import ctypes
import http.client
import sys
from unittest.mock import MagicMock, Mock

import pytest

from sanka_drf_replay import go
from sanka_drf_replay.replay import ReplayError


@pytest.mark.parametrize(
    "phase,error,expired,category,exit_status",
    [
        ("send", TimeoutError(), False, "infrastructure_failure", None),
        ("headers", TimeoutError(), False, "candidate_failure", None),
        ("body", TimeoutError(), False, "candidate_failure", None),
        ("headers", OSError("local transport unavailable"), False, "infrastructure_failure", None),
        ("body", TimeoutError(), True, "infrastructure_failure", None),
        ("headers", http.client.RemoteDisconnected(), False, "candidate_failure", None),
        ("headers", http.client.RemoteDisconnected(), True, "infrastructure_failure", None),
        ("send", http.client.RemoteDisconnected(), False, "infrastructure_failure", None),
        ("headers", http.client.RemoteDisconnected(), False, "infrastructure_failure", -9),
    ],
)
def test_go_response_timeout_is_repairable(
    tmp_path, monkeypatch, phase, error, expired, category, exit_status
):
    process = Mock(pid=12345)
    process.poll.side_effect = [None, exit_status]
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


@pytest.mark.parametrize(
    "platform,members,query_errno,exception",
    [
        ("darwin", 0, 0, ReplayError),
        ("darwin", 1, 0, PermissionError),
        ("darwin", 0, 1, PermissionError),
        ("linux", 0, 0, PermissionError),
    ],
)
def test_exited_go_server_cleanup_preserves_failure_only_when_group_is_gone(
    tmp_path, monkeypatch, platform, members, query_errno, exception
):
    monkeypatch.setattr(sys, "platform", platform)
    process = Mock(pid=12345)
    process.poll.return_value = 1
    monkeypatch.setattr(go.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(go.os, "killpg", Mock(side_effect=PermissionError(1, "denied")))

    def query(*args):
        ctypes.set_errno(query_errno)
        return members

    library = Mock()
    library.proc_listpgrppids.side_effect = query
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=library))
    monkeypatch.setattr(go.signal, "signal", Mock())
    monkeypatch.setattr(go.threading, "Timer", Mock())
    reservation = MagicMock()
    reservation.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 12345)
    monkeypatch.setattr(go.socket, "socket", Mock(return_value=reservation))
    with pytest.raises(exception) as caught:
        go.run_candidate(
            tmp_path,
            {"method": "GET", "path": "/"},
            [],
            str(tmp_path / "db.sqlite3"),
            "DATABASE_URL",
            tmp_path,
        )
    if exception is ReplayError:
        assert caught.value.category == "candidate_failure"
