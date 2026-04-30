from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_PATH
from services.correction_detector import maybe_create_suggestion_for_correction
from services.db import connect_db, init_db, list_recent_corrections


def main() -> None:
    conn = connect_db(SQLITE_PATH)
    init_db(conn)
    corrections = list_recent_corrections(conn, limit=500)
    created = 0
    seen: set[tuple[str, str]] = set()
    for row in corrections:
        key = (row["from_folder"], row["to_folder"])
        if key in seen:
            continue
        seen.add(key)
        suggestion_id = maybe_create_suggestion_for_correction(conn, int(row["message_id"]), row["from_folder"], row["to_folder"])
        if suggestion_id is not None:
            created += 1
    conn.close()
    print(json.dumps({"suggestions_created": created}, indent=2))


if __name__ == "__main__":
    main()
