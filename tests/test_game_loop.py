"""Day 4's finish line: a whole hunt played through the API (with a fake referee)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from images import jpeg, scene

from game_server.app import create_app
from game_server.clock import get_clock
from game_server.config import Settings, get_settings
from game_server.referee import (
    RefereeJudgement,
    RefereeReport,
    VisualCheckJudgement,
    get_referee,
)
from game_server.sessions import VisualChallenge, get_session_repository, parse_sessions

SESSION = "aeffe667-4f9f-4108-b5e2-56ae821fe413"
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
END = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
PLACES = {1: (51.50, -0.10), 2: (51.51, -0.11), 3: (51.52, -0.12)}
ROUTES = {"FOX-7Q2K": [1, 2, 3], "HERON-4MXP": [2, 3, 1]}


def sessions_file() -> str:
    return json.dumps(
        [
            {
                "id": SESSION,
                "name": "Hunt",
                "location": "Here",
                "start-time": START.isoformat(),
                "end-time": END.isoformat(),
                "checkpoints": [
                    {
                        "sequence": n,
                        "name": f"Name {n}",
                        "clue": f"Clue {n}",
                        "location": {"lat": lat, "long": long},
                        "proximity": 50,
                        "challenge": {"scene": f"Scene {n}", "pose": f"Pose {n}"},
                    }
                    for n, (lat, long) in PLACES.items()
                ],
                "teams": [
                    {"name": "Red Foxes", "join-code": "FOX-7Q2K", "order": ROUTES["FOX-7Q2K"]},
                    {
                        "name": "Blue Herons",
                        "join-code": "HERON-4MXP",
                        "order": ROUTES["HERON-4MXP"],
                    },
                ],
            }
        ]
    )


class PassingReferee:
    """Judges every photo a confident pass."""

    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        self.calls.append(challenge.pose)
        check = VisualCheckJudgement(reason="fine", verdict="pass", confidence=0.95)
        return RefereeReport(
            status="ok",
            judgement=RefereeJudgement(scene_matches=check, pose_correct=check),
            model="fake",
        )


@pytest.fixture
def clock() -> list[datetime]:
    return [START + timedelta(minutes=5)]


@pytest.fixture
def client(tmp_path: Path, clock: list[datetime]) -> Iterator[TestClient]:
    app = create_app()
    settings = Settings(image_base_path=tmp_path / "images", db_path=tmp_path / "game.sqlite3")
    sessions = parse_sessions(sessions_file())
    referee = PassingReferee()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session_repository] = lambda: sessions
    app.dependency_overrides[get_clock] = lambda: lambda: clock[0]
    app.dependency_overrides[get_referee] = lambda: referee
    with TestClient(app) as test_client:
        yield test_client


class Team:
    """Plays through the API like the app would, keeping every clue it's ever shown."""

    photos = count(200)  # distinct seeds: no photo is a duplicate of another

    def __init__(self, client: TestClient, clock: list[datetime], code: str) -> None:
        self.client, self.clock, self.code = client, clock, code
        joined = client.post("/join", json={"code": code, "consent": True})
        assert joined.status_code == 201
        self.participant = joined.json()["participant"]
        self.clues_seen: list[str] = []

    def tick(self) -> None:
        self.clock[0] += timedelta(minutes=2)

    def state(self) -> dict[str, Any]:
        body: dict[str, Any] = self.client.get(
            f"/sessions/{SESSION}/participants/{self.participant}/state"
        ).json()
        if body["current"]:
            self.clues_seen.append(body["current"]["clue"])
        return body

    def arrive(self, checkpoint: int) -> dict[str, Any]:
        response = self.client.post(
            f"/sessions/{SESSION}/participants/{self.participant}/arrive",
            json={"checkpoint": checkpoint},
        )
        assert response.status_code in (200, 201), response.text
        body: dict[str, Any] = response.json()
        return body

    def photograph(self, checkpoint: int, *, far_away: bool = False) -> str:
        lat, long = PLACES[checkpoint]
        metadata = {
            "session": SESSION,
            "participant": self.participant,
            "checkpoint": checkpoint,
            "location": {"lat": lat + (0.05 if far_away else 0.0001), "long": long},
            "capture-time": self.clock[0].isoformat(),
        }
        response = self.client.post(
            "/challenge",
            files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "challenge-image": ("p.jpeg", jpeg(scene(next(self.photos))), "image/jpeg"),
            },
        )
        verdict: str = response.json()["verdict"]["checkpoint"]["verdict"]
        return verdict


def test_two_teams_play_a_whole_hunt(client: TestClient, clock: list[datetime]) -> None:
    foxes, herons = Team(client, clock, "FOX-7Q2K"), Team(client, clock, "HERON-4MXP")

    for step in range(3):
        for team in (foxes, herons):
            team.tick()
            state = team.state()
            current = state["current"]["sequence"]
            assert current == ROUTES[team.code][step]
            assert state["progress"] == {"completed": step, "total": 3}

            arrival = team.arrive(current)
            assert arrival["pose"] == f"Pose {current}"

            if team is foxes and step == 1:
                # A photo from far away fails: the team stays, and gets a fresh code.
                team.tick()
                assert team.photograph(current, far_away=True) == "failed"
                assert team.state()["current"]["sequence"] == current
                retry = team.arrive(current)
                assert retry["issued-at"] != arrival["issued-at"]  # a fresh arrival

            team.tick()
            assert team.photograph(current) == "pass"

    for team in (foxes, herons):
        final = team.state()
        assert (final["status"], final["progress"], final["current"]) == (
            "finished",
            {"completed": 3, "total": 3},
            None,
        )
        # Every clue the team was ever shown was its current one, in its own route's order.
        shown_in_order = list(dict.fromkeys(team.clues_seen))
        assert shown_in_order == [f"Clue {n}" for n in ROUTES[team.code]]
