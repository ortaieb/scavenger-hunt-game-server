"""The referee: judges a photo's visual challenge on the Claude API with structured outputs.

The deterministic checks can only rule a submission out. The referee looks at the photo
itself and answers, for each visual check, pass / fail / unsure with a confidence and a
reason. `judge` never raises: every failure becomes a report with `status="error"`.

All Anthropic SDK use goes through `_create_structured_message`; tests mock that.
"""

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cache, lru_cache
from importlib.resources import files
from io import BytesIO
from typing import Annotated, Literal, Protocol

import anthropic
from anthropic.types import ImageBlockParam, JSONOutputFormatParam, TextBlockParam
from fastapi import Depends
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

from game_server.config import Settings, get_settings
from game_server.imaging import UndecodableImageError, open_upright
from game_server.pricing import cost_usd
from game_server.sessions import VisualChallenge

logger = logging.getLogger(__name__)

# A comfortable ceiling for two short reasons plus the JSON around them.
MAX_TOKENS = 1024
JPEG_QUALITY = 85


class VisualCheckJudgement(BaseModel):
    """The referee's ruling on one visual check."""

    # First, so the model describes what it sees before it rules (output follows schema order).
    reason: str
    verdict: Literal["pass", "fail", "unsure"]
    # Structured outputs don't enforce min/max in the grammar; pydantic enforces them here.
    confidence: float = Field(ge=0, le=1)


class RefereeJudgement(BaseModel):
    """One named field per check, so the schema forces each to appear exactly once."""

    scene_matches: VisualCheckJudgement
    pose_correct: VisualCheckJudgement


RefereeStatus = Literal["ok", "disabled", "error"]
RefereeErrorCode = Literal[
    "timeout", "api_error", "refusal", "max_tokens", "invalid_output", "invalid_image"
]


@dataclass(frozen=True)
class SentImage:
    """The prepared JPEG the model saw, identified by its hash and size: no second copy."""

    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class RefereeCall:
    """What one call sent and got back: the record its trace keeps.

    Server-side only and never logged: `user_text` holds the scene (the answer to the
    clue), and the response describes the photo. Both are kept out of `repr`, so neither
    reaches a log line by accident.
    """

    system_prompt: str = field(repr=False)
    user_text: str = field(repr=False)
    # None when the photo couldn't be prepared, so nothing was sent.
    image: SentImage | None = None
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


class Referee(Protocol):
    """Judges a photo against a checkpoint's visual challenge. Never raises."""

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport: ...


class DisabledReferee:
    """Used when no API key is configured: makes no network call."""

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        """Report `disabled` without looking at the photo."""
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
) -> ModelReply:
    """The one call into the Anthropic SDK: a Messages request constrained to our schema.

    Uses `messages.create` with the same `output_config.format` that `messages.parse`
    sends, so the stop reason can be checked before the output is validated (`parse`
    validates first, which would hide a refusal or truncation behind a parse error).
    """
    message = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"format": _output_format()},
    )
    return ModelReply(
        stop_reason=message.stop_reason,
        text="".join(block.text for block in message.content if block.type == "text"),
        model=message.model,
        request_id=message._request_id,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )


@dataclass(frozen=True)
class PreparedImage:
    """The photo as the model receives it."""

    jpeg: bytes = field(repr=False)
    width: int
    height: int

    def sent(self) -> SentImage:
        """How the trace identifies this JPEG."""
        return SentImage(hashlib.sha256(self.jpeg).hexdigest(), self.width, self.height)


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


def user_text(challenge: VisualChallenge) -> str:
    """The text part of the user turn: the scene and pose inside their delimiting tags."""
    return (
        f"<scene>\n{challenge.scene}\n</scene>\n\n<pose>\n{challenge.pose}\n</pose>\n\n"
        "Judge scene_matches and pose_correct for this photo."
    )


def build_content(prepared_jpeg: bytes, text: str) -> list[ImageBlockParam | TextBlockParam]:
    """The user turn: the image, then the text."""
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(prepared_jpeg).decode("ascii"),
            },
        },
        {"type": "text", "text": text},
    ]


class ClaudeReferee:
    """Judges with a Claude model, via structured outputs."""

    def __init__(self, client: anthropic.Anthropic, model: str, max_image_edge: int) -> None:
        self._client = client
        self.model = model
        self.max_image_edge = max_image_edge

    def judge(self, image: bytes, challenge: VisualChallenge) -> RefereeReport:
        """Ask the model for a judgement. Never raises: failures become `status="error"`."""
        started = time.monotonic()
        report = self._judge(image, challenge, started)
        _log(report)
        return report

    def _judge(self, image: bytes, challenge: VisualChallenge, started: float) -> RefereeReport:
        system, text = system_prompt(), user_text(challenge)
        try:
            prepared = prepare_image(image, self.max_image_edge)
        except UndecodableImageError:
            return self._error("invalid_image", started, RefereeCall(system, text))
        call = RefereeCall(system, text, prepared.sent())
        try:
            reply = _create_structured_message(
                self._client,
                model=self.model,
                system=system,
                content=build_content(prepared.jpeg, text),
            )
        except anthropic.APITimeoutError:  # a subclass of APIConnectionError: check it first
            return self._error("timeout", started, call)
        except anthropic.APIError:  # status errors and connection errors, after SDK retries
            return self._error("api_error", started, call)
        return self._interpret(reply, started, call)

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
                latency_ms=_elapsed_ms(started),
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
            latency_ms=_elapsed_ms(started),
            call=call,
        )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


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
def build_referee(
    api_key: str | None, model: str, timeout_seconds: float, max_retries: int, max_edge: int
) -> Referee:
    """The process-wide referee for this configuration. No key: `DisabledReferee`."""
    if not api_key:
        return DisabledReferee()
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=max_retries)
    return ClaudeReferee(client, model, max_edge)


def get_referee(settings: Annotated[Settings, Depends(get_settings)]) -> Referee:
    """Dependency providing the referee built from settings (overridable in tests)."""
    key = settings.anthropic_api_key
    return build_referee(
        key.get_secret_value() if key else None,
        settings.referee_model,
        settings.referee_timeout_seconds,
        settings.referee_max_retries,
        settings.referee_max_image_edge,
    )
