"""python -m app.bootstrap: exit 0 when the corpus is empty and needs a first ingest.

Used by scripts/start.sh so a fresh Railway volume fills itself without a
manual `python -m app.ingest`.
"""

from app.db import corpus_status, init_database, read_db
from app.settings import get_settings


def main() -> int:
    path = get_settings().database_path
    init_database(path)
    with read_db(path) as conn:
        _, documents, _ = corpus_status(conn)
    return 0 if documents == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
