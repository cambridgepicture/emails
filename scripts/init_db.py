from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_PATH
from services.db import connect_db, init_db


def main() -> None:
    conn = connect_db(SQLITE_PATH)
    init_db(conn)
    conn.close()
    print(f"Initialized {SQLITE_PATH}")


if __name__ == "__main__":
    main()
