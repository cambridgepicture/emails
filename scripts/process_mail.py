from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_PATH
from services.classifier import upsert_and_classify_message
from services.db import connect_db, init_db


SAMPLE_MESSAGE = {
    "graph_message_id": "sample-001",
    "internet_message_id": "<sample-001@example.local>",
    "conversation_id": "conv-001",
    "sender_email": "billing@example.com",
    "sender_name": "Billing Team",
    "subject": "Invoice for April services",
    "body_excerpt": "Please see the attached invoice and payment instructions. Amount due is listed below.",
    "received_at": "2026-04-30T12:00:00+00:00",
    "importance": "high",
    "to_recipients": ["accounts@example.local"],
    "cc_recipients": [],
    "attachment_names": ["invoice-april.pdf"],
    "initial_folder": "Inbox",
    "current_folder": "Inbox",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a mock email message into the emails database.")
    parser.add_argument("--json", dest="json_path", help="Path to a JSON file containing one message")
    args = parser.parse_args()

    payload = SAMPLE_MESSAGE
    if args.json_path:
        payload = json.loads(Path(args.json_path).read_text(encoding="utf-8"))

    conn = connect_db(SQLITE_PATH)
    init_db(conn)
    result = upsert_and_classify_message(conn, payload)
    conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
