"""The referee: judges a photo's visual challenge on the Claude API with structured outputs.

The deterministic checks can only rule a submission out. The referee looks at the photo
itself and answers, for each visual check, pass / fail / unsure with a confidence and a
reason. `judge` never raises: every failure becomes a report with `status="error"`.

The whole step, retries included, ends by a deadline, so a slow model can't keep a player
waiting: past it, the report is an error and the verdict goes to a moderator.

All Anthropic SDK use goes through `_create_structured_message`; tests mock that.
"""

import base64
import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cache, lru_cache
from importlib.resources import files
from io import BytesIO
from typing import Annotated, Literal, Protocol, Self

import anthropic
from anthropic.types import ImageBlockParam, JSONOutputFormatParam, TextBlockParam
from fastapi import Depends
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

from game_server.clock import Timer
from game_server.config import (
    DEFAULT_REFEREE_DEADLINE_SECONDS,
    DEFAULT_REFEREE_MAX_RETRIES,
    DEFAULT_REFEREE_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from game_server.imaging import UndecodableImageError, open_upright
from game_server.pricing import cost_usd
from game_server.sessions import VisualChallenge

logger = logging.getLogger(__name__)

# A comfortable ceiling for two short reasons plus the JSON around them.
MAX_TOKENS = 1024
JPEG_QUALITY = 85
# The text blocks that say which image is which, when reference photos are sent.
REFERENCE_LABEL = (
    "Reference photo {number} of {count}: the checkpoint, photographed by the organiser"
)
PLAYER_LABEL = "The player's photo"
# No retry starts with less time than this left before the deadline: it couldn't answer.
MIN_RETRY_SECONDS = 1.0
# The pause before each retry doubles from the first, up to the longest.
FIRST_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 2.0

Sleep = Callable[[float], None]


class VisualCheckJudgement(BaseModel):
    """The referee's ruling on one visual check."""

    # First, so the model describes what it sees before it rules (output follows schema order).
    # The description goes out in the schema, so the privacy rule sits where each reason is
    # written.
    reason: str = Field(
        description=(
            "What you see, in one or two short sentences. Describe the pose only by body "
            'position (arms, hands, head direction, stance) and call the subject "the person", '
            "never he or she. Never mention age, gender, ethnicity, skin, hair, facial hair, "
            "build, clothing or accessories."
        )
    )
    verdict: Literal["pass", "fail", "unsure"]
    # Structured outputs don't enforce min/max in the grammar; pydantic enforces them here.
    confidence: float = Field(ge=0, le=1)


class RefereeJudgement(BaseModel):
    """One named field per check, so the schema forces each to appear exactly once."""

    scene_matches: VisualCheckJudgement
    pose_correct: VisualCheckJudgement


RefereeStatus = Literal["ok", "disabled", "error"]
RefereeErrorCode = Literal[
    "deadline", "timeout", "api_error", "refusal", "max_tokens", "invalid_output", "invalid_image"
]


@dataclass(frozen=True)
class CallLimits:
    """How long the referee may take: the whole step, each attempt, and how many retries."""

    deadline_seconds: float = DEFAULT_REFEREE_DEADLINE_SECONDS
    timeout_seconds: float = DEFAULT_REFEREE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_REFEREE_MAX_RETRIES

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The configured limits, as production and the evals both use them."""
        return cls(
            settings.referee_deadline_seconds,
            settings.referee_timeout_seconds,
            settings.referee_max_retries,
        )


DEFAULT_LIMITS = CallLimits()


@dataclass(frozen=True)
class SentImage:
    """The prepared JPEG the model saw, identified by its hash and size: no second copy."""

    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class SentReference:
    """A reference photo the model saw: its index in the checkpoint's list, and its hash.

    Never its path, which can describe the place.
    """

    position: int
    sha256: str


@dataclass(frozen=True)
class RefereeCall:
    """What one call sent and got back: the record its trace keeps.

    Server-side only and never logged: `user_text` holds the scene (the answer to the
    clue), and the response describes the photo. Both are kept out of `repr`, so neither
    reaches a log line by accident.
    """

    system_prompt: str = field(repr=False)
    user_text: str = field(repr=False)
    # None when nothing was sent: the photo couldn't be prepared, or the deadline came first.
    image: SentImage | None = None
    # The checkpoint's reference photos sent before it, in order; () when none were sent.
    references: tuple[SentReference, ...] = ()
    stop_reason: str | None = None
    # The model's output as received; None when no reply came back.
    response_text: str | None = field(default=None, repr=False)

    @property
    def prompt_sha256(self) -> str:
        """The system prompt's identity in the trace."""
        return prompt_sha256(self.system_prompt)


@dataclass(frozen=True)
class RefereeReport:
    """What the referee concluded, plus what the call cost, for moderator audit."""

    status: RefereeStatus
    # The reasons describe the photo: kept out of `repr`, like the call's texts.
    judgement: RefereeJudgement | None = field(default=None, repr=False)
    error_code: RefereeErrorCode | None = None
    model: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    # At list price (`game_server.pricing`); None without a reply, or for an unknown model.
    cost_usd: Decimal | None = None
    # What was sent and received; None when the referee is disabled and made no call.
    call: RefereeCall | None = None


@dataclass(frozen=True)
class PreparedImage:
    """The photo as the model receives it."""

    jpeg: bytes = field(repr=False)
    width: int
    height: int

    def sent(self) -> SentImage:
        """How the trace identifies this JPEG."""
        return SentImage(hashlib.sha256(self.jpeg).hexdigest(), self.width, self.height)


@dataclass(frozen=True)
class PreparedReference:
    """A checkpoint's reference photo as the model receives it, and its index in the list."""

    position: int
    image: PreparedImage

    def sent(self) -> SentReference:
        """How the trace identifies this reference: its position and its JPEG's hash."""
        return SentReference(self.position, self.image.sent().sha256)


class Referee(Protocol):
    """Judges a photo against a checkpoint's visual challenge. Never raises.

    `references` are the checkpoint's prepared reference photos, to compare the place with.
    """

    def judge(
        self,
        image: bytes,
        challenge: VisualChallenge,
        references: Sequence[PreparedReference] = (),
    ) -> RefereeReport: ...


class DisabledReferee:
    """Used when no API key is configured: makes no network call."""

    def judge(
        self,
        image: bytes,
        challenge: VisualChallenge,
        references: Sequence[PreparedReference] = (),
    ) -> RefereeReport:
        """Report `disabled` without looking at the photo or the references."""
        return RefereeReport(status="disabled")


@dataclass(frozen=True)
class ModelReply:
    """The parts of a Messages API response the referee uses."""

    stop_reason: str | None
    text: str
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int


@cache
def _output_format() -> JSONOutputFormatParam:
    """The JSON-schema output format for `RefereeJudgement`, as `messages.parse` builds it."""
    schema = RefereeJudgement.model_json_schema()
    return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}


def _create_structured_message(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    content: list[ImageBlockParam | TextBlockParam],
    timeout: float,
) -> ModelReply:
    """The one call into the Anthropic SDK: a Messages request constrained to our schema.

    Uses `messages.create` with the same `output_config.format` that `messages.parse`
    sends, so the stop reason can be checked before the output is validated (`parse`
    validates first, which would hide a refusal or truncation behind a parse error).
    `timeout` bounds this one attempt: the client makes no retries of its own, since it
    can't see the deadline, so `ClaudeReferee` retries instead.
    """
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"format": _output_format()},
        timeout=timeout,
    )
    return ModelReply(
        stop_reason=message.stop_reason,
        text="".join(block.text for block in message.content if block.type == "text"),
        model=message.model,
        request_id=message._request_id,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )


def prepare_image(image: bytes, max_edge: int) -> PreparedImage:
    """Re-encode the photo for the referee: upright, long edge <= `max_edge`, no metadata.

    Re-encoding from pixels drops all EXIF, GPS included, so the provider receives pixels
    only; the smaller image also costs fewer tokens. Raises `UndecodableImageError`.
    """
    with open_upright(image, "RGB", max_edge) as upright:
        rgb = upright.convert("RGB")
        rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        rgb.save(buffer, "JPEG", quality=JPEG_QUALITY, exif=b"")
        return PreparedImage(buffer.getvalue(), *rgb.size)


@cache
def system_prompt() -> str:
    """The referee's instructions, kept in their own file for review and evaluation."""
    return files("game_server").joinpath("referee_prompt.md").read_text(encoding="utf-8")


def prompt_sha256(prompt: str) -> str:
    """A system prompt's identity in traces and eval reports: the SHA-256 of its UTF-8."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def user_text(challenge: VisualChallenge, with_references: bool = False) -> str:
    """The text part of the user turn: the scene and pose inside their delimiting tags.

    With reference photos there are several images, so it names the one to judge.
    """
    photo = "the player's photo" if with_references else "this photo"
    return (
        f"<scene>\n{challenge.scene}\n</scene>\n\n<pose>\n{challenge.pose}\n</pose>\n\n"
        f"Judge scene_matches and pose_correct for {photo}."
    )


def _image_block(jpeg: bytes) -> ImageBlockParam:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def _text_block(text: str) -> TextBlockParam:
    return {"type": "text", "text": text}


def build_content(
    prepared_jpeg: bytes, text: str, references: Sequence[PreparedReference] = ()
) -> list[ImageBlockParam | TextBlockParam]:
    """The user turn: the image, then the text.

    With references, each reference photo and then the player's photo come after a label
    saying which is which; the text still comes last.
    """
    if not references:
        return [_image_block(prepared_jpeg), _text_block(text)]
    content: list[ImageBlockParam | TextBlockParam] = []
    for number, reference in enumerate(references, start=1):
        label = REFERENCE_LABEL.format(number=number, count=len(references))
        content += [_text_block(label), _image_block(reference.image.jpeg)]
    return [*content, _text_block(PLAYER_LABEL), _image_block(prepared_jpeg), _text_block(text)]


def _retryable(error: anthropic.APIError) -> bool:
    """Connection errors (timeouts included), 429 and 5xx: another attempt may succeed."""
    if isinstance(error, anthropic.APIConnectionError):
        return True
    return isinstance(error, anthropic.APIStatusError) and (
        error.status_code == 429 or error.status_code >= 500
    )


def _error_code(error: anthropic.APIError) -> RefereeErrorCode:
    # APITimeoutError is a subclass of APIConnectionError: it gets its own code.
    return "timeout" if isinstance(error, anthropic.APITimeoutError) else "api_error"


def _backoff_seconds(retry: int) -> float:
    """The pause before retry number `retry` (from 0): short, since the deadline is near."""
    return min(FIRST_BACKOFF_SECONDS * 2.0**retry, MAX_BACKOFF_SECONDS)


class ClaudeReferee:
    """Judges with a Claude model, via structured outputs, within `limits`.

    `timer` and `sleep` measure and wait out the deadline; tests pass a fake clock.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        max_image_edge: int,
        limits: CallLimits = DEFAULT_LIMITS,
        timer: Timer = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._client = client
        self.model = model
        self.max_image_edge = max_image_edge
        self.limits = limits
        self._timer = timer
        self._sleep = sleep

    def judge(
        self,
        image: bytes,
        challenge: VisualChallenge,
        references: Sequence[PreparedReference] = (),
    ) -> RefereeReport:
        """Ask the model for a judgement. Never raises: failures become `status="error"`.

        `references` are sent as given, before the photo: the caller picks and prepares them.
        """
        started = self._timer()
        report = self._judge(image, challenge, references, started)
        _log(report)
        return report

    def _judge(
        self,
        image: bytes,
        challenge: VisualChallenge,
        references: Sequence[PreparedReference],
        started: float,
    ) -> RefereeReport:
        system, text = system_prompt(), user_text(challenge, with_references=bool(references))
        try:
            prepared = prepare_image(image, self.max_image_edge)
        except UndecodableImageError:  # nothing is sent, the references included
            return self._error("invalid_image", started, RefereeCall(system, text))
        deadline = started + self.limits.deadline_seconds
        if self._timer() >= deadline:  # preparing the photo took all the time: nothing is sent
            return self._error("deadline", started, RefereeCall(system, text))
        sent = tuple(reference.sent() for reference in references)
        call = RefereeCall(system, text, prepared.sent(), sent)
        content = build_content(prepared.jpeg, text, references)
        reply = self._ask(system, content, deadline)
        if isinstance(reply, str):  # no reply: why not
            return self._error(reply, started, call)
        return self._interpret(reply, started, call)

    def _ask(
        self, system: str, content: list[ImageBlockParam | TextBlockParam], deadline: float
    ) -> ModelReply | RefereeErrorCode:
        """The model's reply, retrying while there's time; or why there's no reply.

        Each attempt waits at most the timeout, or the time left if that's less. A
        retryable error is retried after a short pause, unless that would leave less than
        `MIN_RETRY_SECONDS`. A reply is always used, even one that arrives late.
        """
        retry = 0
        while True:
            left = deadline - self._timer()
            if left <= 0:
                return "deadline"
            try:
                return _create_structured_message(
                    self._client,
                    model=self.model,
                    system=system,
                    content=content,
                    timeout=min(self.limits.timeout_seconds, left),
                )
            except anthropic.APIError as error:
                if not _retryable(error):
                    return _error_code(error)
                if self._timer() >= deadline:  # e.g. the attempt waited out the time left
                    return "deadline"
                if retry == self.limits.max_retries:
                    return _error_code(error)
                pause = _backoff_seconds(retry)
                if deadline - self._timer() - pause < MIN_RETRY_SECONDS:
                    return "deadline"
                retry += 1
                self._log_retry(retry, error, deadline)
                self._sleep(pause)

    def _interpret(self, reply: ModelReply, started: float, call: RefereeCall) -> RefereeReport:
        def report(
            status: RefereeStatus,
            judgement: RefereeJudgement | None = None,
            error_code: RefereeErrorCode | None = None,
        ) -> RefereeReport:
            return RefereeReport(
                status=status,
                judgement=judgement,
                error_code=error_code,
                model=reply.model,
                request_id=reply.request_id,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                latency_ms=self._elapsed_ms(started),
                cost_usd=_cost_usd(reply),
                call=replace(call, stop_reason=reply.stop_reason, response_text=reply.text),
            )

        # On these the output may be cut short or empty: it can't be trusted to fit the schema.
        if reply.stop_reason == "refusal":
            return report("error", error_code="refusal")
        if reply.stop_reason == "max_tokens":
            return report("error", error_code="max_tokens")
        try:
            judgement = RefereeJudgement.model_validate_json(reply.text)
        except ValidationError:
            return report("error", error_code="invalid_output")
        return report("ok", judgement=judgement)

    def _error(self, code: RefereeErrorCode, started: float, call: RefereeCall) -> RefereeReport:
        return RefereeReport(
            status="error",
            error_code=code,
            model=self.model,
            latency_ms=self._elapsed_ms(started),
            call=call,
        )

    def _elapsed_ms(self, started: float) -> int:
        return round((self._timer() - started) * 1000)

    def _log_retry(self, retry: int, error: anthropic.APIError, deadline: float) -> None:
        """One line per retry: what failed, and how much time is left. Never the request."""
        status = error.status_code if isinstance(error, anthropic.APIStatusError) else "-"
        logger.info(
            "referee model=%s retry=%d/%d after error=%s status=%s left_ms=%d",
            self.model,
            retry,
            self.limits.max_retries,
            _error_code(error),
            status,
            round((deadline - self._timer()) * 1000),
        )


def _cost_usd(reply: ModelReply) -> Decimal | None:
    """What the call cost at list price; None, with a warning, for a model without a price."""
    cost = cost_usd(reply.model, reply.input_tokens, reply.output_tokens)
    if cost is None:
        logger.warning("referee model=%s has no price in game_server.pricing", reply.model)
    return cost


def _log(report: RefereeReport) -> None:
    """One line per call. Never the image, the scene or the reasons."""
    parts = [
        f"referee model={report.model}",
        f"status={report.status}",
        f"references={len(report.call.references) if report.call else 0}",
        f"latency_ms={report.latency_ms}",
        f"tokens={report.input_tokens}/{report.output_tokens}",
        f"cost_usd={report.cost_usd}",
        f"request_id={report.request_id}",
    ]
    if report.error_code:
        parts.append(f"error={report.error_code}")
    if report.judgement:
        for name in RefereeJudgement.model_fields:
            check: VisualCheckJudgement = getattr(report.judgement, name)
            parts.append(f"{name}={check.verdict}({check.confidence:.2f})")
    logger.info(" ".join(parts))


@lru_cache
def build_referee(api_key: str | None, model: str, max_edge: int, limits: CallLimits) -> Referee:
    """The process-wide referee for this configuration. No key: `DisabledReferee`.

    The client makes no retries of its own: the referee retries, within its deadline.
    """
    if not api_key:
        return DisabledReferee()
    client = anthropic.Anthropic(api_key=api_key, timeout=limits.timeout_seconds, max_retries=0)
    return ClaudeReferee(client, model, max_edge, limits)


def get_referee(settings: Annotated[Settings, Depends(get_settings)]) -> Referee:
    """Dependency providing the referee built from settings (overridable in tests)."""
    key = settings.anthropic_api_key
    return build_referee(
        key.get_secret_value() if key else None,
        settings.referee_model,
        settings.referee_max_image_edge,
        CallLimits.from_settings(settings),
    )
