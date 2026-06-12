from pathlib import Path

from sqlmodel import Session, select

from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import AppMeta, MediaFile


def test_create_db_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")

    create_db_schema(engine)

    with Session(engine) as session:
        session.add(AppMeta(key="schema_version", value="test"))
        session.commit()

        value = session.exec(select(AppMeta).where(AppMeta.key == "schema_version")).one()

    assert value.value == "test"


def test_insert_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)

    with Session(engine) as session:
        media_file = MediaFile(
            path="/media/example.mkv",
            size_bytes=123,
            mtime_ns=456,
        )
        session.add(media_file)
        session.commit()

        stored = session.exec(select(MediaFile)).one()

    assert stored.path == "/media/example.mkv"
