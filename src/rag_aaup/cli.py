import argparse
import logging

import uvicorn

from rag_aaup.config import get_settings
from rag_aaup.indexing.database import Database
from rag_aaup.indexing.service import IndexingService


def main() -> None:
    parser = argparse.ArgumentParser(description="AAUP Smart Academic Advisor")
    parser.add_argument("command", choices=("serve", "index"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "index":
        settings = get_settings()
        report = IndexingService(settings, Database(settings.database_path)).sync()
        print(
            f"Added: {len(report.added)}, updated: {len(report.updated)}, "
            f"unchanged: {len(report.unchanged)}, failed: {len(report.failed)}"
        )
        for path, error in report.failed.items():
            print(f"FAILED {path}: {error}")
        return

    uvicorn.run("rag_aaup.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
