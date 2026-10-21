"""Source-tree compatibility entry point for the installed backup command."""

from processwatch.backup import backup_database, main


if __name__ == "__main__":
    raise SystemExit(main())
