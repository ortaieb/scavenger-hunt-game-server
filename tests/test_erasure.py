"""Erasure (the right to be forgotten): the job, its triggers, replays after a restore, and the
data export."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from clerk_backend_api.models import ClerkBaseError
from clerk_fakes import FakeClerk
from conftest import SignIn
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from pytest_mock import MockerFixture
from test_join import SESSION, sessions_text
from test_ruling import POSE_UNSURE, TIMEOUT

from game_server.app import create_app
from game_server.checks.base import CheckResult, Rejection
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.database import Database
from game_server.directory.__main__ import (
    list_erasures,
    read_pids,
    replay_erasures,
)
from game_server.directory.__main__ import (
    main as directory_cli,
)
from game_server.directory.clerk import ClerkApi, ClerkError, get_clerk_users
from game_server.directory.erasures import ErasureRecords, suppression_hmac
from game_server.directory.repository import DirectoryRepository, directory_database
from game_server.erasure import ErasureJob, GameRecords, erasure_job
from game_server.moderators import SessionModerators
from game_server.sessions import SessionRepository, get_session_repository
from game_server.storage import ImageStore
from game_server.submissions import NewSubmission, SubmissionStore

T0 = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
ADA, BOB = "user_ada", "user_bob"
ADA_EMAIL = "user_ada@example.com"  # what `sign_in` gives her
SUPPRESSION_KEY = "test-suppression-key-0123456789"
SCENE_REASON = "SENTINEL-REASON: a fountain, one person in a red coat"
DETAIL = "SENTINEL-DETAIL: arm half raised"
NOTE = "SENTINEL-NOTE: the coat is clearly visible"
LAT, LONG = 51.50721, -0.12758


@pytest.fixture
def clerk() -> FakeClerk:
    return FakeClerk()


@pytest.fixture
def settings(sign_in: SignIn, tmp_path: Path) -> Settings:
    """Accounts on, with the test key `sign_in` trusts, and a suppression key."""
    return Settings(
        auth_mode="optional",
        image_base_path=tmp_path / "images",
        suppression_key=SUPPRESSION_KEY,
    )


@pytest.fixture
def client(
    settings: Settings,
    clerk: FakeClerk,
    load_sessions: Callable[[str], SessionRepository],
) -> Iterator[TestClient]:
    app = create_app()
    sessions = load_sessions(sessions_text())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: T0
    app.dependency_overrides[get_clerk_users] = lambda: clerk
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def directory_db(settings: Settings) -> Database:
    return directory_database(settings)


@pytest.fixture
def people(directory_db: Database) -> DirectoryRepository:
    return DirectoryRepository(directory_db)


@pytest.fixture
def records(directory_db: Database) -> ErasureRecords:
    return ErasureRecords(directory_db)


@pytest.fixture
def job(settings: Settings, clerk: FakeClerk) -> ErasureJob:
    return erasure_job(settings, lambda: T0, clerk)


@pytest.fixture
def images(settings: Settings) -> ImageStore:
    return ImageStore(settings.image_base_path)


def pid_of(people: DirectoryRepository, user: str) -> UUID:
    found = people.find_pid(user)
    assert found is not None
    return found


def photo(
    store: SubmissionStore,
    images: ImageStore,
    participant: UUID,
    pid: UUID | None,
    checkpoint: int = 1,
) -> tuple[int, UUID]:
    """A photo the person sent: its file, checks with reasons, and a referee trace."""
    image_id, _ = images.save(b"\xff\xd8 a photo \xff\xd9")
    scene_failed = CheckResult(
        "scene_matches",
        "failed",
        0.9,
        SCENE_REASON,
        Rejection("scene_mismatch", SCENE_REASON),
        detail=DETAIL,
    )
    recorded = store.record(
        NewSubmission(
            session=UUID(SESSION),
            participant=participant,
            checkpoint=checkpoint,
            received_at=T0,
            capture_time=T0,
            lat=LAT,
            long=LONG,
            image_id=image_id,
            verdict="failed",
            checks=(POSE_UNSURE, scene_failed),
            distance_m=3.5,
            phash=0x1234_5678_9ABC_DEF0,
            processing_ms=40,
            referee=TIMEOUT,
            pid=pid,
        )
    )
    return recorded.id, image_id


@pytest.fixture
def ada(
    client: TestClient,
    sign_in: SignIn,
    people: DirectoryRepository,
    store: SubmissionStore,
    database: Database,
    images: ImageStore,
    clerk: FakeClerk,
) -> dict[str, Any]:
    """Ada, an organiser who plays too: profile, consent, team, check-in, photo, ruling and a
    moderator row. Her teammate Bob plays alongside her."""
    headers = sign_in("organiser", ADA)
    pid = pid_of(people, ADA)
    clerk.emails[ADA] = ADA_EMAIL
    client.patch("/me", json={"displayName": "Ada Lovelace", "homeArea": "W4"}, headers=headers)
    client.put(
        "/me/consents/upcoming-hunts",
        json={"granted": True, "wordingVersion": "2026-10-16"},
        headers=headers,
    )
    participant = store.join_team(UUID(SESSION), "Red Foxes", T0, pid).participant
    store.arrive(
        UUID(SESSION),
        participant,
        1,
        pose="Wave",
        now=T0,
        ttl=timedelta(minutes=10),
        new_code=lambda: "CODE-1",
        pid=pid,
    )
    submission, image_id = photo(store, images, participant, pid)
    store.rule(UUID(SESSION), submission, "reject", NOTE, T0, pid=pid)
    SessionModerators(database).add(UUID(SESSION), pid, None, T0)
    bob_headers = sign_in("player", BOB)
    bob = pid_of(people, BOB)
    store.join_team(UUID(SESSION), "Red Foxes", T0, bob)
    bob_submission, bob_image = photo(store, images, participant, bob, checkpoint=2)
    return {
        "headers": headers,
        "pid": pid,
        "participant": participant,
        "submission": submission,
        "image": image_id,
        "bob": bob,
        "bob_headers": bob_headers,
        "bob_submission": bob_submission,
        "bob_image": bob_image,
    }


def count(db: psycopg.Connection[DictRow], query: str, *params: object) -> int:
    row = db.execute(query, params).fetchone()
    assert row is not None
    return int(next(iter(row.values())))


def submission_row(db: psycopg.Connection[DictRow], submission: int) -> DictRow:
    row = db.execute("SELECT * FROM submissions WHERE id = %s", (submission,)).fetchone()
    assert row is not None
    return row


def every_value(db: psycopg.Connection[DictRow]) -> str:
    """Every row of every table in the game's and the directory's schemas, as text, in a fixed
    order (tables by name, rows by their text), so two snapshots compare equal."""
    tables = db.execute(
        "SELECT table_schema, table_name FROM information_schema.tables"
        " WHERE table_schema IN ('public', 'directory') AND table_type = 'BASE TABLE'"
        " AND table_name <> 'flyway_schema_history'"
        " ORDER BY table_schema, table_name"
    ).fetchall()
    dumped = []
    for table in tables:
        name = f'"{table["table_schema"]}"."{table["table_name"]}"'
        # The table names come from the catalogue, never from input.
        query = f"SELECT t::text AS row FROM {name} t ORDER BY 1"  # noqa: S608
        rows = db.execute(query).fetchall()
        dumped += [f"{table['table_schema']}.{table['table_name']}: {row['row']}" for row in rows]
    return "\n".join(dumped)


# --- the job -------------------------------------------------------------------------------------


def test_erasing_removes_the_person_and_keeps_the_teams_results(
    client: TestClient,
    ada: dict[str, Any],
    people: DirectoryRepository,
    images: ImageStore,
    clerk: FakeClerk,
    db: psycopg.Connection[DictRow],
) -> None:
    response = client.request("DELETE", "/me", headers=ada["headers"])

    assert response.status_code == 202
    assert response.json() == {"pid": str(ada["pid"]), "status": "erasing"}
    pid = ada["pid"]
    # The directory: the person, their profile, consents and role.
    assert people.find_pid(ADA) is None
    for table in ("profiles", "contact_consents", "role_grants", "role_changes", "clerk_outbox"):
        query = f"SELECT count(*) FROM directory.{table} WHERE pid = %s"  # noqa: S608 - fixed names
        assert count(db, query, pid) == 0
    # Clerk: sessions revoked at once, then the user deleted.
    assert clerk.revoked == [ADA]
    assert clerk.deleted == [ADA]
    # The game: the photo file, the membership and the moderator row.
    assert not images.path_for(ada["image"]).exists()
    assert count(db, "SELECT count(*) FROM team_members WHERE pid = %s", pid) == 0
    assert count(db, "SELECT count(*) FROM session_moderators WHERE pid = %s", pid) == 0
    # The team's result stays, with no person and nothing describing them.
    row = submission_row(db, ada["submission"])
    assert (row["participant"], row["checkpoint"], row["verdict"], row["received_at"]) == (
        ada["participant"],
        1,
        "failed",
        T0,
    )
    assert (row["pid"], row["lat"], row["long"], row["distance_m"], row["phash"]) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert [(c["check"], c["outcome"], c["reason"], c["detail"]) for c in row["checks"]] == [
        ("pose_correct", "uncertain", "", None),
        ("scene_matches", "failed", "", None),
    ]
    assert row["rejections"] == [{"code": "scene_mismatch", "message": ""}]
    assert count(db, "SELECT count(*) FROM referee_traces WHERE submission_id = %s", row["id"]) == 0
    ruling = db.execute("SELECT ruling, note, pid FROM rulings").fetchone()
    assert ruling == {"ruling": "reject", "note": None, "pid": None}
    assert db.execute("SELECT pid FROM arrivals").fetchall() == [{"pid": None}]


def test_the_erasure_is_recorded_with_each_step(
    client: TestClient, ada: dict[str, Any], records: ErasureRecords
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])

    erasure = records.get(ada["pid"])
    assert erasure is not None
    assert erasure.source == "self"
    assert erasure.do_not_contact is False
    assert (
        erasure.game_done_at,
        erasure.clerk_done_at,
        erasure.directory_done_at,
        erasure.completed_at,
    ) == (T0, T0, T0, T0)


def test_a_teammate_is_left_as_they_were(
    client: TestClient,
    ada: dict[str, Any],
    people: DirectoryRepository,
    images: ImageStore,
    db: psycopg.Connection[DictRow],
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])

    assert people.find_pid(BOB) == ada["bob"]
    assert images.path_for(ada["bob_image"]).exists()
    row = submission_row(db, ada["bob_submission"])
    assert (row["pid"], row["lat"], row["phash"]) == (ada["bob"], LAT, "123456789abcdef0")
    assert row["checks"][1]["reason"] == SCENE_REASON
    assert count(db, "SELECT count(*) FROM referee_traces") == 1
    assert count(db, "SELECT count(*) FROM team_members WHERE pid = %s", ada["bob"]) == 1


def test_nothing_identifying_is_left_anywhere(
    client: TestClient, ada: dict[str, Any], db: psycopg.Connection[DictRow]
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])

    everything = every_value(db)
    for identity in (ADA, ADA_EMAIL, "Ada Lovelace", NOTE):
        assert identity not in everything


def test_the_pid_remains_only_as_evidence_in_the_erasures(
    client: TestClient, ada: dict[str, Any], db: psycopg.Connection[DictRow]
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])

    lines = [line for line in every_value(db).splitlines() if str(ada["pid"]) in line]
    assert len(lines) == 1
    assert lines[0].startswith(f"directory.erasures: ({ada['pid']},")


def test_running_twice_gives_the_same_result(
    ada: dict[str, Any], job: ErasureJob, clerk: FakeClerk, db: psycopg.Connection[DictRow]
) -> None:
    job.request(ada["pid"], "admin", do_not_contact=False)
    assert job.run(ada["pid"])
    first = every_value(db)

    assert job.run(ada["pid"])
    assert job.replay(ada["pid"])

    assert every_value(db) == first
    assert clerk.deleted == [ADA]  # the replay found the Clerk user already gone in Clerk


def test_an_interrupted_erasure_finishes_on_the_next_run(
    ada: dict[str, Any],
    job: ErasureJob,
    clerk: FakeClerk,
    people: DirectoryRepository,
    records: ErasureRecords,
    images: ImageStore,
) -> None:
    job.request(ada["pid"], "admin", do_not_contact=False)
    clerk.fail_writes = True

    assert not job.run(ada["pid"])

    half = records.get(ada["pid"])
    assert half is not None
    assert half.game_done_at == T0
    assert (half.clerk_done_at, half.directory_done_at, half.completed_at) == (None, None, None)
    assert not images.path_for(ada["image"]).exists()
    found = people.find_person(ADA)
    assert found is not None
    assert found.status == "erasing"  # kept until Clerk is done: it holds the Clerk user id

    clerk.fail_writes = False
    assert job.run_unfinished() == 1

    assert people.find_pid(ADA) is None
    assert clerk.deleted == [ADA]
    done = records.get(ada["pid"])
    assert done is not None
    assert done.completed_at == T0


def test_without_a_clerk_key_the_erasure_waits(
    ada: dict[str, Any], settings: Settings, people: DirectoryRepository
) -> None:
    job = erasure_job(settings, lambda: T0, None)
    job.request(ada["pid"], "admin", do_not_contact=False)

    assert not job.run(ada["pid"])
    assert people.find_pid(ADA) == ada["pid"]


def test_an_erasure_clerk_started_doesnt_call_clerk(
    ada: dict[str, Any], job: ErasureJob, clerk: FakeClerk, people: DirectoryRepository
) -> None:
    job.request(ada["pid"], "clerk", do_not_contact=False)

    assert job.run(ada["pid"])
    assert (clerk.revoked, clerk.deleted) == ([], [])
    assert people.find_pid(ADA) is None


def test_two_runs_at_once_never_overlap(
    ada: dict[str, Any], job: ErasureJob, records: ErasureRecords
) -> None:
    job.request(ada["pid"], "admin", do_not_contact=False)

    with records.lock(ada["pid"]) as held:
        assert held
        assert not job.run(ada["pid"])  # someone else holds it: left for them

    assert job.run(ada["pid"])


# --- suppression ---------------------------------------------------------------------------------


def test_asking_not_to_be_contacted_keeps_only_a_keyed_hash(
    client: TestClient, ada: dict[str, Any], db: psycopg.Connection[DictRow]
) -> None:
    client.request("DELETE", "/me", json={"doNotContact": True}, headers=ada["headers"])

    rows = db.execute("SELECT email_hmac, reason FROM directory.suppressions").fetchall()
    assert rows == [
        {"email_hmac": suppression_hmac(SUPPRESSION_KEY, ADA_EMAIL), "reason": "erasure"}
    ]
    assert ADA_EMAIL not in every_value(db)


def test_the_hash_ignores_case_and_spaces() -> None:
    assert suppression_hmac("k", " Ada@Example.COM ") == suppression_hmac("k", "ada@example.com")


def test_without_asking_there_is_no_suppression(
    client: TestClient, ada: dict[str, Any], db: psycopg.Connection[DictRow]
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])

    assert count(db, "SELECT count(*) FROM directory.suppressions") == 0


def test_without_a_suppression_key_the_erasure_waits_for_one(
    ada: dict[str, Any], settings: Settings, clerk: FakeClerk, people: DirectoryRepository
) -> None:
    keyless = settings.model_copy(update={"suppression_key": None})
    job = erasure_job(keyless, lambda: T0, clerk)
    job.request(ada["pid"], "admin", do_not_contact=True)

    assert not job.run(ada["pid"])
    assert people.find_pid(ADA) == ada["pid"]

    assert erasure_job(settings, lambda: T0, clerk).run(ada["pid"])
    assert people.find_pid(ADA) is None


def test_asking_not_to_be_contacted_is_never_undone(
    ada: dict[str, Any], records: ErasureRecords
) -> None:
    records.request(ada["pid"], "self", do_not_contact=True, now=T0)

    erasure = records.request(ada["pid"], "admin", do_not_contact=False, now=T0)

    assert (erasure.source, erasure.do_not_contact) == ("self", True)


# --- the duplicate check -------------------------------------------------------------------------


def test_an_erased_fingerprint_is_left_out_of_the_duplicate_check(
    ada: dict[str, Any], job: ErasureJob, store: SubmissionStore
) -> None:
    job.request(ada["pid"], "admin", do_not_contact=False)
    job.run(ada["pid"])

    with store.transaction(UUID(SESSION)) as transaction:
        accepted = transaction.accepted_photos(UUID(SESSION))

    assert ada["submission"] not in {photo.submission_id for photo in accepted}


# --- triggers ------------------------------------------------------------------------------------


def test_erasing_needs_a_token(client: TestClient) -> None:
    assert client.request("DELETE", "/me").status_code == 401


def test_delete_me_refuses_unknown_options(client: TestClient, ada: dict[str, Any]) -> None:
    response = client.request("DELETE", "/me", json={"everything": True}, headers=ada["headers"])

    assert response.status_code == 422


def test_an_admin_erases_a_person(
    client: TestClient,
    ada: dict[str, Any],
    sign_in: SignIn,
    people: DirectoryRepository,
    records: ErasureRecords,
) -> None:
    admin = sign_in("admin", "user_admin")

    response = client.post(
        f"/directory/people/{ada['bob']}/erase", json={"doNotContact": True}, headers=admin
    )

    assert response.status_code == 202
    assert people.find_pid(BOB) is None
    erasure = records.get(ada["bob"])
    assert erasure is not None
    assert (erasure.source, erasure.do_not_contact) == ("admin", True)


def test_only_an_admin_erases_someone_else(client: TestClient, ada: dict[str, Any]) -> None:
    response = client.post(f"/directory/people/{ada['bob']}/erase", headers=ada["headers"])

    assert response.status_code == 403


def test_erasing_an_unknown_person_is_404(client: TestClient, sign_in: SignIn) -> None:
    response = client.post(f"/directory/people/{uuid4()}/erase", headers=sign_in("admin"))

    assert response.status_code == 404
    assert response.json()["code"] == "unknown_person"


def test_the_command_line_erases_a_person(
    ada: dict[str, Any],
    settings: Settings,
    clerk: FakeClerk,
    people: DirectoryRepository,
    mocker: MockerFixture,
) -> None:
    mocker.patch("game_server.directory.__main__.Settings", return_value=settings)
    mocker.patch("game_server.directory.__main__.get_clerk_users", return_value=clerk)
    mocker.patch("game_server.directory.__main__.close_databases")

    assert directory_cli(["erase", "--pid", str(ada["pid"])]) == 0
    assert people.find_pid(ADA) is None


# --- restores ------------------------------------------------------------------------------------


def restore(db: psycopg.Connection[DictRow], ada: dict[str, Any]) -> None:
    """Bring Ada back, as a restore from a backup taken before her erasure would."""
    db.execute(
        "INSERT INTO directory.people (pid, clerk_user_id) VALUES (%s, %s)", (ada["pid"], ADA)
    )
    db.execute(
        "INSERT INTO directory.profiles (pid, email, display_name) VALUES (%s, %s, 'Ada Lovelace')",
        (ada["pid"], ADA_EMAIL),
    )
    db.execute(
        "INSERT INTO team_members (session, participant, pid, joined_at, photo_consent_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (UUID(SESSION), ada["participant"], ada["pid"], T0, T0),
    )
    db.execute(
        "UPDATE submissions SET pid = %s, lat = %s, long = %s WHERE id = %s",
        (ada["pid"], LAT, LONG, ada["submission"]),
    )


def test_a_replay_erases_a_restored_person_again(
    client: TestClient,
    ada: dict[str, Any],
    settings: Settings,
    clerk: FakeClerk,
    people: DirectoryRepository,
    db: psycopg.Connection[DictRow],
    mocker: MockerFixture,
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])
    restore(db, ada)
    mocker.patch("game_server.directory.__main__.get_clerk_users", return_value=clerk)

    assert replay_erasures(settings, None) == 0

    assert people.find_pid(ADA) is None
    assert count(db, "SELECT count(*) FROM team_members WHERE pid = %s", ada["pid"]) == 0
    row = submission_row(db, ada["submission"])
    assert (row["pid"], row["lat"]) == (None, None)
    assert ADA_EMAIL not in every_value(db)


def test_a_replay_from_a_saved_list_covers_erasures_the_restore_lost(
    client: TestClient,
    ada: dict[str, Any],
    settings: Settings,
    clerk: FakeClerk,
    people: DirectoryRepository,
    db: psycopg.Connection[DictRow],
    mocker: MockerFixture,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.request("DELETE", "/me", json={"doNotContact": True}, headers=ada["headers"])
    assert list_erasures(settings) == 0
    saved = tmp_path / "erasures.txt"
    saved.write_text(capsys.readouterr().out)
    db.execute("TRUNCATE directory.erasures, directory.suppressions")  # the restore lost them
    restore(db, ada)
    mocker.patch("game_server.directory.__main__.get_clerk_users", return_value=clerk)

    assert replay_erasures(settings, saved) == 0

    assert people.find_pid(ADA) is None
    assert count(db, "SELECT count(*) FROM directory.suppressions") == 1
    assert count(db, "SELECT count(*) FROM directory.erasures WHERE source = 'replay'") == 1


def test_a_replay_takes_do_not_contact_from_the_saved_list(
    client: TestClient,
    ada: dict[str, Any],
    job: ErasureJob,
    db: psycopg.Connection[DictRow],
) -> None:
    client.request("DELETE", "/me", headers=ada["headers"])
    restore(db, ada)

    assert job.replay(ada["pid"], do_not_contact=True)

    assert count(db, "SELECT count(*) FROM directory.suppressions") == 1


def test_list_erasures_prints_pids_only(
    client: TestClient,
    ada: dict[str, Any],
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client.request("DELETE", "/me", json={"doNotContact": True}, headers=ada["headers"])

    assert list_erasures(settings) == 0

    assert capsys.readouterr().out == f"{ada['pid']} do-not-contact\n"


@pytest.mark.parametrize(
    "line", ["not-a-pid", "5b1f5d3e-8f0e-4f64-9d57-2f6d4a3e7c10 maybe", "a b c"]
)
def test_a_bad_saved_list_is_refused(tmp_path: Path, line: str) -> None:
    saved = tmp_path / "erasures.txt"
    saved.write_text(f"{line}\n")

    with pytest.raises(ValueError):
        read_pids(saved)


def test_a_saved_list_keeps_do_not_contact(tmp_path: Path) -> None:
    pid = uuid4()
    saved = tmp_path / "erasures.txt"
    saved.write_text(f"{pid}\n\n{pid} do-not-contact\n")

    assert read_pids(saved) == {pid: True}


# --- export --------------------------------------------------------------------------------------


def test_the_export_holds_the_profile_consents_role_and_play_history(
    client: TestClient, ada: dict[str, Any]
) -> None:
    response = client.get("/me/export", headers=ada["headers"])

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["pid"] == str(ada["pid"])
    assert body["profile"]["email"] == ADA_EMAIL
    assert body["profile"]["displayName"] == "Ada Lovelace"
    assert body["role"] == "organiser"
    [consent] = body["consents"]
    assert (consent["purpose"], consent["source"], consent["withdrawnAt"]) == (
        "upcoming_hunts",
        "profile",
        None,
    )
    assert consent["wording"]
    [team] = body["teams"]
    assert (team["team"], team["hunt"]) == ("Red Foxes", "Hyde Park Saturday Hunt")
    [photo_row] = body["photos"]
    assert (photo_row["checkpoint"], photo_row["lat"], photo_row["long"]) == (1, LAT, LONG)
    assert all("detail" not in check for check in photo_row["checks"])
    assert [c["checkpoint"] for c in body["checkIns"]] == [1]
    assert [r["note"] for r in body["rulings"]] == [NOTE]
    assert [m["session"] for m in body["moderating"]] == [SESSION]
    assert DETAIL not in response.text
    assert "image" not in response.text  # never a photo, nor where one is


def test_the_export_holds_only_the_callers_data(client: TestClient, ada: dict[str, Any]) -> None:
    body = client.get("/me/export", headers=ada["bob_headers"]).json()

    assert body["pid"] == str(ada["bob"])
    assert [p["checkpoint"] for p in body["photos"]] == [2]
    assert body["rulings"] == []
    assert body["moderating"] == []
    assert ADA_EMAIL not in json.dumps(body)


def test_the_export_needs_a_token(client: TestClient) -> None:
    assert client.get("/me/export").status_code == 401


# --- Clerk ---------------------------------------------------------------------------------------


def clerk_failure(status: int) -> ClerkBaseError:
    return ClerkBaseError(
        "failed", httpx.Response(status, request=httpx.Request("DELETE", "https://x"))
    )


def test_deleting_a_clerk_user_already_gone_is_fine(mocker: MockerFixture) -> None:
    api = ClerkApi("sk_test_x")
    mocker.patch.object(api._clerk.users, "delete", side_effect=clerk_failure(404))

    api.delete_user("user_gone")


def test_another_clerk_failure_is_a_clerk_error_naming_only_its_kind(
    mocker: MockerFixture,
) -> None:
    api = ClerkApi("sk_test_x")
    mocker.patch.object(api._clerk.users, "delete", side_effect=clerk_failure(500))

    with pytest.raises(ClerkError, match=r"^ClerkBaseError$"):
        api.delete_user("user_a")


def test_erasing_someone_the_game_never_saw_does_nothing(
    ada: dict[str, Any], database: Database, db: psycopg.Connection[DictRow]
) -> None:
    before = every_value(db)

    GameRecords(database).erase(uuid4())

    assert every_value(db) == before
