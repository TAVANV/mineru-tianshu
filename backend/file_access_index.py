"""Indexed canonical task-file ownership, maintained atomically by SQLite triggers."""

from pathlib import Path


def init_schema(cursor):
    if not cursor.connection.in_transaction:
        cursor.execute("BEGIN IMMEDIATE")
    existing = cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_file_access'").fetchone()
    cursor.execute("""CREATE TABLE IF NOT EXISTS task_file_access (
        task_id TEXT NOT NULL, kind TEXT NOT NULL, raw_path TEXT NOT NULL,
        canonical_path TEXT, PRIMARY KEY(task_id, kind))""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_access_path ON task_file_access(kind,canonical_path)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_access_dirty ON task_file_access(canonical_path) WHERE canonical_path IS NULL"
    )
    for kind, column in [("upload", "file_path"), ("output", "result_path")]:
        for event in ["INSERT", f"UPDATE OF {column}"]:
            label = "insert" if event == "INSERT" else "update"
            cursor.execute(f"""CREATE TRIGGER IF NOT EXISTS file_access_{kind}_{label} AFTER {event} ON tasks
                BEGIN
                    DELETE FROM task_file_access WHERE task_id=NEW.task_id AND kind='{kind}';
                    INSERT INTO task_file_access(task_id,kind,raw_path)
                    SELECT NEW.task_id,'{kind}',NEW.{column}
                    WHERE NEW.{column} IS NOT NULL AND NEW.{column} != 'CLEARED';
                END""")
        if not existing:
            cursor.execute(f"""INSERT OR IGNORE INTO task_file_access(task_id,kind,raw_path)
            SELECT task_id,'{kind}',{column} FROM tasks WHERE {column} IS NOT NULL AND {column} != 'CLEARED' """)
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS file_access_delete AFTER DELETE ON tasks BEGIN
        DELETE FROM task_file_access WHERE task_id=OLD.task_id; END""")


def lookup(db, full_path, root, output=False):
    full_path, root = Path(full_path).resolve(), Path(root).resolve()
    if not full_path.is_relative_to(root):
        return None
    with db.get_cursor() as cursor:
        if cursor.execute("SELECT 1 FROM task_file_access WHERE canonical_path IS NULL LIMIT 1").fetchone():
            cursor.execute("BEGIN IMMEDIATE")
            for row in cursor.execute(
                "SELECT task_id,kind,raw_path FROM task_file_access WHERE canonical_path IS NULL"
            ).fetchall():
                cursor.execute(
                    "UPDATE task_file_access SET canonical_path=? WHERE task_id=? AND kind=?",
                    (str(Path(row["raw_path"]).resolve()), row["task_id"], row["kind"]),
                )
        candidates = [str(full_path)]
        if output:
            candidates += [str(p) for p in full_path.parents if p.is_relative_to(root)]
        placeholders = ",".join("?" for _ in candidates)
        row = cursor.execute(
            f"""SELECT t.* FROM task_file_access a JOIN tasks t ON t.task_id=a.task_id
            WHERE a.kind=? AND a.canonical_path IN ({placeholders})
            ORDER BY length(a.canonical_path) DESC LIMIT 1""",
            ["output" if output else "upload", *candidates],
        ).fetchone()
        if row:
            current = Path(row["result_path" if output else "file_path"]).resolve()
            if current.is_relative_to(root) and (
                (output and full_path.is_relative_to(current)) or full_path == current
            ):
                return dict(row)
        return None
