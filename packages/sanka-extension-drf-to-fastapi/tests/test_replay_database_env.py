# SPDX-License-Identifier: Apache-2.0
"""Direct HTTP replay-side isolation; no scan/plan/apply migration lifecycle."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from sanka_drf_replay.replay import _replay_one


@pytest.mark.parametrize("candidate_env", ["CANDIDATE_DB", "SOURCE_DB"])
def test_source_and_candidate_use_independent_database_variable_names(tmp_path, candidate_env):
    project, candidate = tmp_path / "source", tmp_path / "candidate"
    project.mkdir()
    candidate.mkdir()
    (project / "settings.py").write_text(
        'import os\nSECRET_KEY="sample"\nROOT_URLCONF="urls"\n'
        'ALLOWED_HOSTS=["testserver"]\nINSTALLED_APPS=[]\n'
        'DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3",'
        '"NAME":os.environ["SOURCE_DB"]}}\n'
    )
    (project / "urls.py").write_text(
        "import os, sqlite3\nfrom django.http import JsonResponse\n"
        "from django.urls import path\n"
        "def create(request):\n"
        '    with sqlite3.connect(os.environ["SOURCE_DB"]) as db:\n'
        '        pk=db.execute("INSERT INTO records(value) VALUES (?)", ("sample",)).lastrowid\n'
        '    return JsonResponse({"id":pk},status=201)\n'
        'urlpatterns=[path("records/",create)]\n'
    )
    candidate_app = (
        "import os, sqlite3\nfrom fastapi import FastAPI\n"
        'app=FastAPI()\n@app.post("/records/", status_code=201)\n'
        "def create():\n"
        '    with sqlite3.connect(os.environ["CANDIDATE_DB"]) as db:\n'
        '        pk=db.execute("INSERT INTO records(value) VALUES (?)", ("sample",)).lastrowid\n'
        '    return {"id":pk}\n'
    )
    (candidate / "app.py").write_text(candidate_app.replace("CANDIDATE_DB", candidate_env))
    base = tmp_path / "base.sqlite3"
    untouched = tmp_path / "captured.sqlite3"
    for database in (base, untouched):
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
    media = tmp_path / "media"
    media.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    environment.update(PYTHONDONTWRITEBYTECODE="1")
    environment[candidate_env] = str(untouched)
    for index in range(2):
        report = _replay_one(
            {
                "id": str(index),
                "method": "POST",
                "path": "/records/",
                "expected_source_status": 201,
            },
            index=index,
            target="fastapi",
            project=project,
            candidate=candidate,
            entrypoint="app.py",
            settings_module="settings",
            db_env="SOURCE_DB",
            candidate_db_env=candidate_env,
            base_db=base,
            base_media=media,
            temp=tmp_path,
            ignored=(),
            all_headers=False,
            source_python=Path(sys.executable),
            target_python=Path(sys.executable),
            environment=environment,
        )
        assert report["match"] is True
        assert report["native"]["compliant"] is True
        for side in ("source", "candidate"):
            assert report[side]["status"] == 201
            assert report[side]["database_before"] == {"records": 0}
            assert report[side]["database_after"] == {"records": 1}
        with sqlite3.connect(untouched) as db:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
