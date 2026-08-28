import json
import sqlite3
from pathlib import Path

import pytest

from codelux.errors import ValidationError
from codelux.sessions import CodexSessionManager

SESSION_ID = "12345678-1234-1234-1234-123456789abc"


def _fixture(tmp_path: Path) -> tuple[CodexSessionManager, Path, Path]:
    root = tmp_path / ".codex"
    sessions = root / "sessions" / "2026" / "08" / "08"
    sessions.mkdir(parents=True)
    session = sessions / f"rollout-2026-08-08T00-00-00-{SESSION_ID}.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "model_provider": "openai"},
            }
        )
        + "\n"
    )
    db = root / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table threads (id text primary key, model_provider text not null)")
        conn.execute("insert into threads values (?, 'openai')", (SESSION_ID,))
        conn.commit()
    return CodexSessionManager(tmp_path), session, db


def test_shared_session_prepare_commit_and_rollback(tmp_path: Path) -> None:
    manager, session, db = _fixture(tmp_path)
    session.chmod(0o640)
    db.chmod(0o660)
    change = manager.prepare({"openai"})
    assert change is not None
    manager.commit(change)
    assert json.loads(session.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"
    assert session.stat().st_mode & 0o777 == 0o640
    assert db.stat().st_mode & 0o777 == 0o660
    manager.rollback(change)
    assert json.loads(session.read_text())["payload"]["model_provider"] == "openai"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "openai"
    assert session.stat().st_mode & 0o777 == 0o640
    assert db.stat().st_mode & 0o777 == 0o660


def test_shared_session_ignores_already_shared_and_supports_noop(tmp_path: Path) -> None:
    manager, session, db = _fixture(tmp_path)
    session.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": SESSION_ID, "model_provider": "custom"}}
        )
        + "\n"
    )
    with sqlite3.connect(db) as conn:
        conn.execute("update threads set model_provider='custom'")
        conn.commit()
    assert manager.prepare({"openai"}) is None


def test_session_invalid_json_fails_closed(tmp_path: Path) -> None:
    manager, session, _ = _fixture(tmp_path)
    session.write_text("not-json\n")
    try:
        manager.prepare({"openai"})
    except Exception as exc:
        assert "JSONL" in str(exc)
    else:
        raise AssertionError("invalid session JSONL was accepted")


def test_merge_all_includes_unknown_and_removed_providers(tmp_path: Path) -> None:
    manager, session, db = _fixture(tmp_path)
    session.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": SESSION_ID, "model_provider": "removed"}}
        )
        + "\n"
    )
    with sqlite3.connect(db) as conn:
        conn.execute("update threads set model_provider='unknown'")
        conn.commit()

    change = manager.prepare()
    assert change is not None
    manager.commit(change)
    assert json.loads(session.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"


def test_merge_rewrites_only_metadata_matching_session_filename(tmp_path: Path) -> None:
    manager, session, _ = _fixture(tmp_path)
    embedded_id = "87654321-4321-4321-4321-cba987654321"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": SESSION_ID, "model_provider": "openai"},
                    }
                ),
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": embedded_id, "model_provider": "removed"},
                    }
                ),
            ]
        )
        + "\n"
    )

    change = manager.prepare()
    assert change is not None
    manager.commit(change)
    records = [json.loads(line) for line in session.read_text().splitlines()]
    assert records[0]["payload"]["model_provider"] == "custom"
    assert records[1]["payload"]["model_provider"] == "removed"


def test_merge_rejects_noncanonical_session_filename(tmp_path: Path) -> None:
    manager, session, _ = _fixture(tmp_path)
    session.rename(session.with_name("session.jsonl"))

    with pytest.raises(ValidationError, match="canonical UUID"):
        manager.prepare()


def test_merge_rejects_metadata_that_does_not_match_filename(tmp_path: Path) -> None:
    manager, session, _ = _fixture(tmp_path)
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "87654321-4321-4321-4321-cba987654321",
                    "model_provider": "openai",
                },
            }
        )
        + "\n"
    )

    with pytest.raises(ValidationError, match="does not match"):
        manager.prepare()
