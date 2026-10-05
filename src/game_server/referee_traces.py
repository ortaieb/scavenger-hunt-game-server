"""The referee's traces: one row per call, written in its submission's transaction.

A trace keeps what the referee was sent and what came back, so a verdict stays explainable
after the prompt changes, and what the call cost. The tables are defined in `schema.sql`.
Server-side only: a trace holds the scene (the answer to the clue) and the model's
description of the photo.
"""

from uuid import UUID

from psycopg import Connection, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from game_server.referee import RefereeReport


def record_trace(
    conn: Connection[TupleRow],
    submission_id: int,
    session: UUID,
    image_id: UUID,
    report: RefereeReport,
) -> None:
    """Record the call behind `report`, inside the caller's transaction.

    A disabled referee made no call, so its report has no trace. The system prompt is
    stored once, the first time it's used.
    """
    call = report.call
    if call is None:
        return
    _record_prompt(conn, call.prompt_sha256, call.system_prompt)
    image, judgement = call.image, report.judgement
    values: dict[str, object] = {
        "session": session,
        "submission_id": submission_id,
        "image_id": image_id,
        "image_sha256": image.sha256 if image else None,
        "image_width": image.width if image else None,
        "image_height": image.height if image else None,
        "reference_photos": Jsonb([]),  # the reference photos sent: none yet
        "prompt_sha256": call.prompt_sha256,
        "user_text": call.user_text,
        "model": report.model,
        "request_id": report.request_id,
        "status": report.status,
        "error_code": report.error_code,
        "stop_reason": call.stop_reason,
        "response_text": call.response_text,
        "judgement": Jsonb(judgement.model_dump(mode="json")) if judgement else None,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "cost_usd": report.cost_usd,
        "latency_ms": report.latency_ms,
    }
    conn.execute(
        sql.SQL("INSERT INTO referee_traces (created_at, {}) VALUES (now(), {})").format(
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(map(sql.Placeholder, values)),
        ),
        values,
    )


def _record_prompt(conn: Connection[TupleRow], sha256: str, text: str) -> None:
    """Store the prompt's text, unless it's already there."""
    conn.execute(
        "INSERT INTO referee_prompts (sha256, text, first_used_at) VALUES (%s, %s, now())"
        " ON CONFLICT (sha256) DO NOTHING",
        (sha256, text),
    )
