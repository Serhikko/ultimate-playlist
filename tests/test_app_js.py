"""Functions of the web UI (static/app.js) run in Node with stubbed surroundings. Skipped when
Node is not installed (the CI runners have it)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "ultimate_playlist" / "server" / "static"
APP_JS = STATIC / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def _function_source(name: str) -> str:
    """The source of a top-level function inside app.js's closure (two-space indent)."""
    text = APP_JS.read_text(encoding="utf-8")
    start = text.index(f"\n  function {name}(") + 1
    end = text.index("\n  }\n", start) + len("\n  }\n")
    return text[start:end]


def _run(script: str, tmp_path: Path) -> object:
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [str(NODE), str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _note_job_transitions(polls: list[list[dict[str, object]]], tmp_path: Path) -> dict:
    """Feed successive /api/jobs polls to noteJobTransitions; how often it reloaded the
    library and what it toasted."""
    script = f"""
'use strict';
const state = {{ jobStatus: new Map(), firstJobsPoll: true }};
let reloads = 0;
const toasts = [];
function reloadTracks() {{ reloads += 1; }}
function toast(message) {{ toasts.push(message); }}
{_function_source("noteJobTransitions")}
const polls = {json.dumps(polls)};
const reloadsAfter = [];
for (const jobs of polls) {{
  noteJobTransitions(jobs);
  reloadsAfter.push(reloads);
}}
console.log(JSON.stringify({{ reloadsAfter, toasts }}));
"""
    result = _run(script, tmp_path)
    assert isinstance(result, dict)
    return result


def test_a_finished_download_reloads_the_library(tmp_path: Path) -> None:
    out = _note_job_transitions(
        [
            [{"id": "a", "status": "downloading"}],
            [{"id": "a", "status": "done", "track": {"id": "soundcloud:1"}}],
        ],
        tmp_path,
    )
    assert out["reloadsAfter"] == [0, 1]


def test_a_track_moved_into_the_jobs_folder_reloads_the_library(tmp_path: Path) -> None:
    """A link pasted again with a folder chosen: the track was in Unsorted and joined the
    folder, so the folder list and the track's folder changed though the job has no track."""
    out = _note_job_transitions(
        [
            [{"id": "a", "status": "queued"}, {"id": "b", "status": "queued"}],
            [
                {"id": "a", "status": "skipped", "message": "Already in library; moved to OST"},
                {"id": "b", "status": "skipped", "message": "Already in library (in Other)"},
            ],
        ],
        tmp_path,
    )
    assert out["reloadsAfter"] == [0, 1]
    assert out["toasts"] == []


@pytest.mark.parametrize(
    "message",
    [
        "Already in library",
        "Already in library (in Other)",
        "Already in library; not moved to OST: the file is in use",
        "Already in the queue",
        None,
    ],
)
def test_other_skips_leave_the_library_alone(message: str | None, tmp_path: Path) -> None:
    out = _note_job_transitions(
        [
            [{"id": "a", "status": "queued"}],
            [{"id": "a", "status": "skipped", "message": message}],
        ],
        tmp_path,
    )
    assert out["reloadsAfter"] == [0, 0]
