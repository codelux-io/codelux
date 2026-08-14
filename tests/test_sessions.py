import json
import sqlite3
from pathlib import Path

from codelux.sessions import CodexSessionManager


def _fixture(tmp_path: Path) -> tuple[CodexSessionManager, Path, Path]:
    root = tmp_path / ".codex"
    sessions = root / "sessions" / "2026" / "08" / "08"
    sessions.mkdir(parents=True)
    session = sessions / "session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "sid-1", "model_provider": "openai"},
            }
        )
        + "\n"
    )
    db = root / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("create table threads (id text primary key, model_provider text not null)")
        conn.execute("insert into threads values ('sid-1', 'openai')")
        conn.commit()
    return CodexSessionManager(tmp_path), session, db


def test_shared_session_prepare_commit_and_rollback(tmp_path: Path) -> None:
    manager, session, db = _fixture(tmp_path)
    change = manager.prepare({"openai"})
    assert change is not None
    manager.commit(change)
    assert json.loads(session.read_text())["payload"]["model_provider"] == "custom"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "custom"
    manager.rollback(change)
    assert json.loads(session.read_text())["payload"]["model_provider"] == "openai"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select model_provider from threads").fetchone()[0] == "openai"


def test_shared_session_ignores_already_shared_and_supports_noop(tmp_path: Path) -> None:
    manager, session, db = _fixture(tmp_path)
    session.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sid-1", "model_provider": "custom"}})
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
