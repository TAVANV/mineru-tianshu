"""Create a consistent SQLite snapshot, including committed WAL contents."""

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # mode=ro avoids silently creating an empty database for a mistyped source.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
            snapshot = Path(tmp.name)
        try:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst, pages=256)
            snapshot.replace(destination)
        finally:
            snapshot.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(
            os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent.parent / "data/db/mineru_tianshu.db"))
        ),
    )
    args = parser.parse_args()
    backup_database(args.source, args.destination)
    print(f"Database snapshot saved to {args.destination}")
