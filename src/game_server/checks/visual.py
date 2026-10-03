"""Visual checks: map the referee's report on the photo to check results.

The referee (on the Claude API) is consulted before the write transaction; these checks
only read its report from the context, so they are fast and pure.

The model's reason describes the scene, which is the answer to the clue: it goes into the
moderator-only `detail`. The player sees fixed text per check and outcome.
"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from game_server.checks.base import CheckResult, InTransaction, Rejection, SubmissionContext
from game_server.referee import RefereeJudgement, VisualCheckJudgement

UNCERTAIN_REASON = "The referee couldn't decide on this. A moderator will review your photo."
SKIPPED_REASON = "Not checked for this attempt."


def classify(
    judged: VisualCheckJudgement, min_confidence: float
) -> Literal["passed", "failed", "uncertain"]:
    """The model's ruling counts only at or above `min_confidence`; otherwise it's uncertain.

    Shared with the referee evals, so they grade exactly what production does.
    """
    if judged.confidence >= min_confidence and judged.verdict == "pass":
        return "passed"
    if judged.confidence >= min_confidence and judged.verdict == "fail":
        return "failed"
    return "uncertain"


@dataclass(frozen=True)
class _VisualCheck(InTransaction):
    """One of the referee's checks. The model's verdict counts only at `min_confidence`."""

    min_confidence: float
    name: ClassVar[str]
    passed_reason: ClassVar[str]
    rejection: ClassVar[Rejection]

    def __call__(self, ctx: SubmissionContext, /) -> CheckResult:
        """Map the referee's report to this check's result (see the table in docs/api.md)."""
        report = ctx.referee_report
        if ctx.checkpoint.challenge is None:
            return self._skipped("no visual challenge configured")
        if report is None:
            return self._skipped("referee not consulted: an earlier check failed")
        if report.status == "disabled":
            return self._skipped("referee disabled: no API key")
        if report.status == "error" or report.judgement is None:
            return CheckResult(
                self.name,
                "uncertain",
                0.0,
                UNCERTAIN_REASON,
                detail=f"referee error: {report.error_code}",
            )
        return self._from_judgement(self._judgement(report.judgement))

    def _judgement(self, judgement: RefereeJudgement) -> VisualCheckJudgement:
        result: VisualCheckJudgement = getattr(judgement, self.name)
        return result

    def _from_judgement(self, judged: VisualCheckJudgement) -> CheckResult:
        outcome = classify(judged, self.min_confidence)
        if outcome == "passed":
            return CheckResult(
                self.name, "passed", judged.confidence, self.passed_reason, detail=judged.reason
            )
        if outcome == "failed":
            return CheckResult(
                self.name,
                "failed",
                judged.confidence,
                self.rejection.message,
                self.rejection,
                detail=judged.reason,
            )
        return CheckResult(
            self.name, "uncertain", judged.confidence, UNCERTAIN_REASON, detail=judged.reason
        )

    def _skipped(self, why: str) -> CheckResult:
        return CheckResult(self.name, "skipped", 0.0, SKIPPED_REASON, detail=why)


@dataclass(frozen=True)
class SceneMatchesCheck(_VisualCheck):
    """The background is the checkpoint described in `challenge.scene`, photographed for real."""

    name: ClassVar[str] = "scene_matches"
    passed_reason: ClassVar[str] = "Your photo matches this checkpoint."
    rejection: ClassVar[Rejection] = Rejection(
        "scene_mismatch",
        "We couldn't match your photo to this checkpoint. Make sure the place is clearly "
        "visible behind you, then take a new photo.",
    )


@dataclass(frozen=True)
class PoseCorrectCheck(_VisualCheck):
    """One person is in the photo, posing as `challenge.pose` asks."""

    name: ClassVar[str] = "pose_correct"
    passed_reason: ClassVar[str] = "Your pose matches the challenge."
    rejection: ClassVar[Rejection] = Rejection(
        "pose_incorrect",
        "Your pose doesn't match the challenge. Check the instructions and take a new photo.",
    )
