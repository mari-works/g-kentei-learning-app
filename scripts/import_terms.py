import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_ROOT.parent))

from config import Config
from models import init_db, load_keywords_from_csv


def main():
    db_path = Config.DATABASE
    csv_path = Path(__file__).resolve().parent.parent / "terms_2024.csv"

    print("Initializing database if needed...")
    init_db(db_path)

    print(f"Loading keywords from CSV: {csv_path}")
    load_keywords_from_csv(db_path, csv_path)
    print("CSV import complete.")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)
