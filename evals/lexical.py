"""Measure explicit-fact recall through the Hermes provider on disposable PostgreSQL."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).with_suffix(".toml")
DATABASE_URL = "CHA_FINAL_EVAL_DATABASE_URL"
NOTE_HEADER = "Recalled notes for reference only:"
FAILURE_PREFIX = "[Memory recall failed: "
ORIGIN = "https://git.example.invalid/eval/{}.git"


@contextmanager
def workspace(path, project):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd

    directory = path / (project or "unscoped")
    if not directory.exists():
        git_env = {"HOME": str(path), "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "--quiet", "--template=", str(directory)],
                       env=git_env, check=True, timeout=10)
        if project:
            subprocess.run(["git", "-C", str(directory), "remote", "add", "origin", ORIGIN.format(project)],
                           env=git_env, check=True, timeout=10)
    token = set_session_cwd(str(directory))
    try:
        yield
    finally:
        reset_session_cwd(token)


def seed(conn, facts):
    """Seed only empty fixture tables. The lock keeps the emptiness check and inserts atomic."""
    from agent.redact import redact_sensitive_text

    from custom_build.memory import project, store

    bodies = {fact["id"]: redact_sensitive_text(fact["body"], force=True) for fact in facts}
    if len(set(bodies.values())) != len(facts) or any(body.splitlines() != [body] for body in bodies.values()):
        raise ValueError("The eval fixture needs unique fact IDs and single-line bodies")
    with conn.transaction():
        conn.execute("lock table cha_final.facts, cha_final.project_facts, cha_final.events, "
                     "cha_final.legacy_import in share row exclusive mode")
        nonempty, = conn.execute(
            "select exists (select 1 from cha_final.facts) or exists (select 1 from cha_final.project_facts) "
            "or exists (select 1 from cha_final.events) or exists (select 1 from cha_final.legacy_import)"
        ).fetchone()
        if nonempty:
            raise SystemExit("The eval database must have an empty cha_final schema")
        for fact in facts:
            scope = project.key_for_origin(ORIGIN.format(fact["project"])) if "project" in fact else None
            store.save(conn, bodies[fact["id"]], project=scope)
    return bodies


def score_case(notes, bodies, expected):
    """Map returned text to IDs using the fixture's unique, single-line bodies."""
    if notes.startswith(FAILURE_PREFIX) and notes.endswith("]"):
        return {"status": "failed", "code": notes[len(FAILURE_PREFIX):-1], "fact_ids": []}

    fact_ids = []
    if notes:
        header, *lines = notes.splitlines()
        if header != NOTE_HEADER or not lines:
            raise ValueError("Unexpected recall note format")
        by_body = {body: fact_id for fact_id, body in bodies.items()}
        if any(line not in by_body for line in lines):
            raise ValueError("Recall returned a note outside the fixture")
        fact_ids = [by_body[line] for line in lines]

    if expected is None:
        return {"status": "unexpected_notes" if fact_ids else "abstained", "fact_ids": fact_ids}
    if expected in fact_ids:
        return {"status": "hit", "rank": fact_ids.index(expected) + 1, "fact_ids": fact_ids}
    return {"status": "miss", "fact_ids": fact_ids}


def evaluate(corpus, home, database_url, conn):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_cli.plugins import discover_plugins
    from plugins.memory import load_memory_provider

    shutil.copytree(ROOT / "custom_build", home / "plugins" / "custom_build",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(
        "memory:\n  provider: custom_build\nplugins:\n  enabled:\n    - custom_build\n", encoding="utf-8",
    )
    token = set_secret_scope({"CHA_FINAL_DATABASE_URL": database_url}, profile_home=str(home))
    try:
        discover_plugins()
        bodies = seed(conn, corpus["facts"])
        results = {}
        for case in corpus["cases"]:
            session_id = f"lexical-eval-{case['id']}"
            provider = load_memory_provider("custom_build")
            if provider is None:
                raise RuntimeError("Hermes did not load the custom_build provider")
            provider.initialize(session_id, hermes_home=str(home))
            with workspace(home, case.get("project")):
                notes = provider.prefetch(case["query"], session_id=session_id)
            results[case["id"]] = score_case(notes, bodies, case.get("expected"))
    finally:
        reset_secret_scope(token)
    return results


def summarize(corpus, results):
    answerable = [results[case["id"]] for case in corpus["cases"] if "expected" in case]
    unanswerable = [results[case["id"]] for case in corpus["cases"] if "expected" not in case]
    ranks = [result["rank"] for result in answerable if result["status"] == "hit"]
    return {
        "cases": results,
        "answerable": len(answerable),
        "recall_at_5": round(sum(rank <= 5 for rank in ranks) / len(answerable), 4),
        "mrr": round(sum(1 / rank for rank in ranks) / len(answerable), 4),
        "abstention": {"correct": sum(result["status"] == "abstained" for result in unanswerable),
                       "total": len(unanswerable)},
        "recall_failures": sum(result["status"] == "failed" for result in results.values()),
    }


def _database_url():
    value = os.environ.get(DATABASE_URL, "").strip()
    if not value:
        return None
    try:
        psycopg.conninfo.conninfo_to_dict(value)
    except psycopg.ProgrammingError:
        return None
    return value


def _failure(code):
    from custom_build import failures

    failures.record(None, code, "failed", source="lexical_eval")
    print(json.dumps({"error": code}))
    return 1


def main():
    source = CORPUS.read_bytes()
    corpus = tomllib.loads(source.decode("utf-8"))
    with tempfile.TemporaryDirectory(prefix="cha-lexical-eval-") as scratch:
        home = Path(scratch) / "hermes"
        os.environ.update(HOME=scratch, HERMES_HOME=str(home))
        from hermes_cli import __file__ as hermes_cli_file

        database_url = _database_url()
        if not database_url:
            return _failure("config.missing")
        revision = subprocess.run(
            ["git", "-C", str(Path(hermes_cli_file).resolve().parent), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=10,
            env={"HOME": scratch, "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1"},
        ).stdout.strip()
        try:
            conn = psycopg.connect(database_url, connect_timeout=3, options="-c statement_timeout=3000")
        except psycopg.OperationalError:
            return _failure("db.unreachable")
        with conn:
            results = evaluate(corpus, home, database_url, conn)

    summary = summarize(corpus, results)
    print(json.dumps({"hermes_commit": revision, "corpus_sha256": hashlib.sha256(source).hexdigest(),
                      **summary}, indent=2))
    return 1 if summary["recall_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
