"""Rehearse a fixed legacy memory snapshot import into a fresh CHA Final store."""

import argparse
import json
import os
from pathlib import Path

import psycopg

from custom_build import failures
from custom_build.memory.legacy_import import LegacyImportError, apply_snapshot
from custom_build.memory.legacy_source import LegacySourceError, read_snapshot


class _InvalidProjectMap(ValueError):
    """Reject a project map with an ambiguous or invalid shape."""


class _InvalidArguments(ValueError):
    """Signal an argument error without retaining command-line values."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _InvalidArguments


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidProjectMap
        result[key] = value
    return result


def _project_origins(path: Path) -> dict[str, str]:
    origins = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    if not isinstance(origins, dict) or any(not isinstance(origin, str) for origin in origins.values()):
        raise _InvalidProjectMap
    return origins


def _database_url(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        psycopg.conninfo.conninfo_to_dict(value)
    except psycopg.ProgrammingError:
        return None
    return value


def _print_error(code: str) -> int:
    print(json.dumps({"error": code}))
    return 1


def _failure(code: str) -> int:
    failures.record(None, code, "failed", source="legacy_import")
    return _print_error(code)


@failures.boundary(_print_error)
def main(argv=None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project-map", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write to a fresh disposable CHA Final database.")
    try:
        args = parser.parse_args(argv)
    except _InvalidArguments:
        return _failure("legacy.arguments_invalid")

    try:
        origins = _project_origins(args.project_map)
    except (OSError, UnicodeError, json.JSONDecodeError, _InvalidProjectMap):
        return _failure("legacy.mapping_invalid")

    source_url = _database_url("CHA_FINAL_LEGACY_DATABASE_URL")
    target_url = _database_url("CHA_FINAL_DATABASE_URL") if args.apply else None
    if source_url is None or (args.apply and target_url is None):
        return _failure("config.missing")

    try:
        with psycopg.connect(source_url, connect_timeout=5) as source:
            rows = read_snapshot(source, origins)
    except LegacySourceError:
        return _failure("legacy.source_invalid")
    except psycopg.OperationalError:
        return _failure("db.unreachable")

    imported = 0
    if target_url is not None:
        try:
            with psycopg.connect(target_url, connect_timeout=5) as target:
                imported = apply_snapshot(target, rows)
        except LegacyImportError:
            return _failure("legacy.target_conflict")
        except psycopg.OperationalError:
            return _failure("db.unreachable")

    print(json.dumps({"eligible": len(rows), "imported": imported}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
