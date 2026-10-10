"""The migration guard-rails CI runs on every PR, against throwaway git repositories."""

from pathlib import Path

import pytest
from check_migrations import LABEL, git, main, problems

V1 = "db/migrations/V1__baseline.sql"


def write(path: str, text: str = "SELECT 1;\n") -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def commit(message: str) -> None:
    git("add", "-A")
    git("commit", "-q", "-m", message)


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`main` with V1 and some code; the PR branch `pr` checked out from it."""
    monkeypatch.chdir(tmp_path)
    git("init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.com"),
        ("commit.gpgsign", "false"),
    ):
        git("config", key, value)
    write(V1, "CREATE TABLE t (n INTEGER);\n")
    write("src/app.py", "print('hi')\n")
    commit("V1")
    git("switch", "-q", "-c", "pr")
    return tmp_path


def on_main(change: str) -> None:
    """Something merged to main after the PR branched."""
    git("switch", "-q", "main")
    write(change)
    commit(change)
    git("switch", "-q", "pr")


# --- what's allowed -----------------------------------------------------------------------------


def test_adding_the_next_migration_is_fine() -> None:
    write("db/migrations/V2__add_column.sql")
    commit("V2")

    assert problems("main", allow_modified=False) == []


def test_several_new_migrations_are_fine() -> None:
    write("db/migrations/V2__add_column.sql")
    write("db/migrations/V3__backfill_column.sql")
    commit("V2, V3")

    assert problems("main", allow_modified=False) == []


def test_changes_outside_the_migrations_are_ignored() -> None:
    write("src/app.py", "print('changed')\n")
    write("README.md")
    commit("code")

    assert problems("main", allow_modified=False) == []


def test_a_repository_without_migrations_yet() -> None:
    git("rm", "-q", V1)
    git("commit", "-q", "-m", "no migrations")
    git("branch", "-q", "-f", "main", "HEAD")
    write(V1)
    commit("V1 again")

    assert problems("main", allow_modified=False) == []


# --- what's refused --------------------------------------------------------------------------


def test_modifying_a_merged_migration_is_refused() -> None:
    write(V1, "CREATE TABLE t (n BIGINT);\n")
    commit("edit V1")

    [problem] = problems("main", allow_modified=False)

    assert problem.startswith(f"{V1} is already on main and was modified.")
    assert "add a new one" in problem
    assert LABEL in problem


@pytest.mark.parametrize("allow_modified", [False, True], ids=["plain", "with-the-label"])
def test_deleting_a_merged_migration_is_refused(allow_modified: bool) -> None:
    git("rm", "-q", V1)
    git("commit", "-q", "-m", "delete V1")

    [problem] = problems("main", allow_modified=allow_modified)

    assert problem.startswith(f"{V1} is already on main and was deleted.")


@pytest.mark.parametrize("allow_modified", [False, True], ids=["plain", "with-the-label"])
def test_renaming_a_merged_migration_is_refused(allow_modified: bool) -> None:
    git("mv", V1, "db/migrations/V1__renamed.sql")
    commit("rename V1")

    [problem] = problems("main", allow_modified=allow_modified)

    assert problem.startswith(f"{V1} is already on main and was renamed.")


def test_a_version_that_isn_t_new_is_refused() -> None:
    write("db/migrations/V1__another_first.sql")
    commit("another V1")

    [problem] = problems("main", allow_modified=False)

    assert "version 1 isn't greater than V1, the highest on main" in problem
    assert "renumber it V2" in problem


def test_two_prs_adding_the_same_version_the_second_must_renumber() -> None:
    on_main("db/migrations/V2__from_another_pr.sql")
    write("db/migrations/V2__mine.sql")
    commit("my V2")

    [problem] = problems("main", allow_modified=False)

    assert problem.startswith("db/migrations/V2__mine.sql: version 2 isn't greater than V2")
    assert "Rebase on main and renumber it V3" in problem


@pytest.mark.parametrize(
    "name",
    ["V2_one_underscore.sql", "v2__lower_v.sql", "V2__Mixed_Case.sql", "V2__add.SQL", "notes.txt"],
)
def test_a_badly_named_file_is_refused(name: str) -> None:
    write(f"db/migrations/{name}")
    commit("bad name")

    [problem] = problems("main", allow_modified=False)

    assert problem.startswith(f"db/migrations/{name}: not a migration name.")


def test_every_problem_is_reported() -> None:
    write(V1, "-- edited\n")
    write("db/migrations/V1__again.sql")
    write("db/migrations/bad.sql")
    commit("three problems")

    assert len(problems("main", allow_modified=False)) == 3


# --- the escape hatch -----------------------------------------------------------------------


def test_the_label_allows_correcting_a_migration_that_failed_in_production() -> None:
    write(V1, "CREATE TABLE t (n INTEGER NOT NULL);\n")
    commit("fix V1, which failed in production")

    assert problems("main", allow_modified=True) == []


def test_the_label_doesn_t_excuse_an_old_version() -> None:
    write("db/migrations/V1__again.sql")
    commit("another V1")

    assert len(problems("main", allow_modified=True)) == 1


# --- the command ---------------------------------------------------------------------------


def test_main_passes_with_exit_code_0(capsys: pytest.CaptureFixture[str]) -> None:
    write("db/migrations/V2__ok.sql")
    commit("V2")

    assert main(["--base", "main"]) == 0
    assert capsys.readouterr().out == "Migrations OK against main.\n"


def test_main_fails_with_the_problems(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    write(V1, "-- edited\n")
    commit("edit V1")

    assert main(["--base", "main"]) == 1
    assert capsys.readouterr().out.startswith(f"error: {V1} is already on main and was modified.")


def test_in_github_actions_problems_are_annotations(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    write(V1, "-- edited\n")
    commit("edit V1")

    main(["--base", "main"])

    assert capsys.readouterr().out.startswith(f"::error::{V1} is already on main")


def test_main_with_the_label_flag(capsys: pytest.CaptureFixture[str]) -> None:
    write(V1, "-- corrected\n")
    commit("correct V1")

    assert main(["--base", "main", "--allow-modified"]) == 0
