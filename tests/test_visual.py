from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import pytest

from game_server.arrivals import Arrival, LatestArrival
from game_server.checks.base import (
    CheckResult,
    InTransaction,
    SubmissionContext,
    decide_verdict,
)
from game_server.checks.visual import (
    SKIPPED_REASON,
    UNCERTAIN_REASON,
    PoseCorrectCheck,
    SceneMatchesCheck,
    classify,
)
from game_server.models import ChallengeMetadata, Location
from game_server.referee import RefereeJudgement, RefereeReport, VisualCheckJudgement
from game_server.sessions import Checkpoint, GameSession, VisualChallenge

THRESHOLD = 0.8
SCENE = SceneMatchesCheck(min_confidence=THRESHOLD)
POSE = PoseCorrectCheck(min_confidence=THRESHOLD)
SENTINEL = "SENTINEL-MODEL-REASON the granite fountain is visible"

Verdict = Literal["pass", "fail", "unsure"]
SKIPPED = ("skipped", 0.0, SKIPPED_REASON)


def make_ctx(
    report: RefereeReport | None, challenge: VisualChallenge | None = None
) -> SubmissionContext:
    checkpoint = Checkpoint(
        sequence=1,
        name="Spot",
        clue="Find it",
        location=Location(lat=0, long=0),
        proximity=10,
        challenge=challenge or VisualChallenge(scene="A fountain", pose="Wave"),
    )
    session = GameSession(
        id=UUID(int=1),
        name="Hunt",
        location="Here",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 2, tzinfo=UTC),
        checkpoints=(checkpoint,),
    )
    metadata = ChallengeMetadata(
        session=session.id,
        participant=UUID(int=2),
        checkpoint=1,
        location=Location(lat=0, long=0),
        capture_time=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    return SubmissionContext(
        metadata,
        datetime(2026, 1, 1, 12, tzinfo=UTC),
        session,
        checkpoint,
        b"img",
        0,
        referee_report=report,
    )


def judged(verdict: Verdict, confidence: float) -> RefereeReport:
    """A report where both checks got the same ruling."""
    check = VisualCheckJudgement(reason=SENTINEL, verdict=verdict, confidence=confidence)
    return RefereeReport(
        status="ok",
        judgement=RefereeJudgement(scene_matches=check, pose_correct=check),
        model="claude-haiku-4-5",
    )


def without_challenge(ctx: SubmissionContext) -> SubmissionContext:
    return replace(ctx, checkpoint=ctx.checkpoint.model_copy(update={"challenge": None}))


@pytest.mark.parametrize("check", [SCENE, POSE], ids=["scene", "pose"])
class TestMapping:
    def test_no_challenge_is_skipped(self, check: SceneMatchesCheck) -> None:
        result = check(without_challenge(make_ctx(judged("pass", 0.99))))

        assert (result.outcome, result.confidence, result.reason) == (
            "skipped",
            0.0,
            SKIPPED_REASON,
        )

    def test_referee_not_consulted_is_skipped(self, check: SceneMatchesCheck) -> None:
        result = check(make_ctx(None))

        assert (result.outcome, result.confidence, result.reason) == (
            "skipped",
            0.0,
            SKIPPED_REASON,
        )

    def test_referee_disabled_is_skipped(self, check: SceneMatchesCheck) -> None:
        result = check(make_ctx(RefereeReport(status="disabled")))

        assert (result.outcome, result.confidence, result.reason) == (
            "skipped",
            0.0,
            SKIPPED_REASON,
        )

    def test_referee_error_is_uncertain(self, check: SceneMatchesCheck) -> None:
        result = check(make_ctx(RefereeReport(status="error", error_code="timeout")))

        assert (result.outcome, result.confidence, result.reason) == (
            "uncertain",
            0.0,
            UNCERTAIN_REASON,
        )
        assert result.detail == "referee error: timeout"

    def test_confident_pass_is_passed(self, check: SceneMatchesCheck) -> None:
        result = check(make_ctx(judged("pass", 0.93)))

        assert (result.outcome, result.confidence, result.rejection) == ("passed", 0.93, None)

    def test_confident_fail_is_failed(self, check: SceneMatchesCheck) -> None:
        result = check(make_ctx(judged("fail", 0.93)))

        assert (result.outcome, result.confidence) == ("failed", 0.93)
        assert result.rejection == check.rejection
        assert result.reason == check.rejection.message

    @pytest.mark.parametrize(
        ("verdict", "confidence"),
        [("unsure", 0.99), ("pass", 0.79), ("fail", 0.5), ("unsure", 0.1)],
    )
    def test_unsure_or_below_threshold_is_uncertain(
        self, check: SceneMatchesCheck, verdict: Verdict, confidence: float
    ) -> None:
        result = check(make_ctx(judged(verdict, confidence)))

        assert (result.outcome, result.confidence, result.reason) == (
            "uncertain",
            confidence,
            UNCERTAIN_REASON,
        )

    @pytest.mark.parametrize(("verdict", "outcome"), [("pass", "passed"), ("fail", "failed")])
    def test_threshold_is_inclusive(
        self, check: SceneMatchesCheck, verdict: Verdict, outcome: str
    ) -> None:
        assert check(make_ctx(judged(verdict, THRESHOLD))).outcome == outcome

    @pytest.mark.parametrize("verdict", ["pass", "fail", "unsure"])
    def test_model_reason_goes_to_detail_never_reason(
        self, check: SceneMatchesCheck, verdict: Verdict
    ) -> None:
        result = check(make_ctx(judged(verdict, 0.95)))

        assert result.detail == SENTINEL
        assert SENTINEL not in result.reason


@pytest.mark.parametrize(
    ("check", "outcome", "reason", "code"),
    [
        (SCENE, "passed", "Your photo matches this checkpoint.", None),
        (
            SCENE,
            "failed",
            "We couldn't match your photo to this checkpoint. Make sure the place is clearly "
            "visible behind you, then take a new photo.",
            "scene_mismatch",
        ),
        (POSE, "passed", "Your pose matches the challenge.", None),
        (
            POSE,
            "failed",
            "Your pose doesn't match the challenge. Check the instructions and take a new photo.",
            "pose_incorrect",
        ),
    ],
)
def test_player_facing_text_and_codes(
    check: SceneMatchesCheck, outcome: str, reason: str, code: str | None
) -> None:
    result = check(make_ctx(judged("pass" if outcome == "passed" else "fail", 0.95)))

    assert (result.check, result.outcome, result.reason) == (check.name, outcome, reason)
    assert (result.rejection.code if result.rejection else None) == code


def every_result() -> list[CheckResult]:
    reports: list[RefereeReport | None] = [
        None,
        RefereeReport(status="disabled"),
        RefereeReport(status="error", error_code="api_error"),
        *(judged(v, c) for v in ("pass", "fail", "unsure") for c in (0.2, 0.95)),
    ]
    return [check(make_ctx(report)) for check in (SCENE, POSE) for report in reports]


def test_a_referee_past_its_deadline_leaves_the_verdict_pending() -> None:
    ctx = make_ctx(RefereeReport(status="error", error_code="deadline"))

    results = [SCENE(ctx), POSE(ctx)]

    assert [(result.outcome, result.detail) for result in results] == [
        ("uncertain", "referee error: deadline"),
        ("uncertain", "referee error: deadline"),
    ]
    assert decide_verdict(results) == "pending"


def test_every_outcome_is_reachable() -> None:
    assert {(r.check, r.outcome) for r in every_result()} == {
        (name, outcome)
        for name in ("scene_matches", "pose_correct")
        for outcome in ("passed", "failed", "uncertain", "skipped")
    }


@pytest.mark.parametrize("result", every_result(), ids=lambda r: f"{r.check}-{r.outcome}")
def test_player_reason_reveals_nothing(result: CheckResult) -> None:
    assert not any(char.isdigit() for char in result.reason)
    assert SENTINEL not in result.reason
    assert "fountain" not in result.reason.lower()


def test_visual_checks_run_in_the_transaction() -> None:
    assert isinstance(SCENE, InTransaction)
    assert isinstance(POSE, InTransaction)


@pytest.mark.parametrize(
    ("verdict", "confidence", "outcome"),
    [
        ("pass", 0.8, "passed"),
        ("pass", 0.79, "uncertain"),
        ("fail", 0.8, "failed"),
        ("fail", 0.1, "uncertain"),
        ("unsure", 1.0, "uncertain"),
    ],
)
def test_classify(verdict: Verdict, confidence: float, outcome: str) -> None:
    judged_check = VisualCheckJudgement(reason="r", verdict=verdict, confidence=confidence)

    assert classify(judged_check, THRESHOLD) == outcome


def test_no_pose_issued_at_check_in_is_skipped_with_its_reason() -> None:
    issued = datetime(2026, 1, 1, 11, 55, tzinfo=UTC)
    arrival = Arrival(3, 1, "1234", None, issued_at=issued, expires_at=issued + timedelta(hours=1))
    ctx = replace(make_ctx(None), arrival=LatestArrival(arrival, used=False))

    results = [check(ctx) for check in (SCENE, POSE)]

    assert {(r.outcome, r.confidence, r.reason) for r in results} == {SKIPPED}
    assert {r.detail for r in results} == {"referee not consulted: no pose was issued at check-in"}


def test_referee_not_consulted_after_a_failure_is_skipped() -> None:
    results = [check(make_ctx(None)) for check in (SCENE, POSE)]

    assert {r.detail for r in results} == {"referee not consulted: an earlier check failed"}
