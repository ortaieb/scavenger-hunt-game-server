# Game sessions and checkpoints

Back to the [README](../README.md).

A **game session** is one hunt. It has a region, a start and end time, and an ordered list of
**checkpoints**. Each checkpoint is a place participants must find from a clue and photograph.
Moderators write sessions by hand in a JSON file that the server loads at startup (see
`GAME_SERVER_SESSIONS_FILE`), and the hunt designer publishes them into the database while the
server runs ([Published sessions](#published-sessions)). Every route serves both the same way.

The file is a JSON list of sessions. Abridged from [`sessions.example.json`](../sessions.example.json):

```json
[
  {
    "id": "aeffe667-4f9f-4108-b5e2-56ae821fe413",
    "name": "Hyde Park Saturday Hunt",
    "location": "Hyde Park and Kensington Gardens, London",
    "start-time": "2026-10-03T10:00:00+01:00",
    "end-time": "2026-10-03T13:00:00+01:00",
    "checkpoints": [
      {
        "sequence": 1,
        "name": "Stone fountain",
        "clue": "Where a princess is remembered by water that never runs in a straight line.",
        "location": { "lat": 51.504873, "long": -0.169872 },
        "proximity": 40
      },
      {
        "sequence": 3,
        "name": "Speakers' Corner",
        "clue": "...",
        "location": { "lat": 51.512346, "long": -0.159203 },
        "proximity": 50,
        "window": {
          "opens-at": "2026-10-03T12:00:00+01:00",
          "closes-at": "2026-10-03T13:00:00+01:00"
        }
      }
    ]
  }
]
```

| Field                      | Rules                                                                |
|----------------------------|----------------------------------------------------------------------|
| `id`                       | UUID, unique across the file. Participants send it as `session`      |
| `name`                     | Non-empty                                                            |
| `location`                 | Non-empty description of the region (free text, not coordinates)    |
| `start-time`, `end-time`   | ISO 8601 with a UTC offset; `end-time` must be after `start-time`    |
| `checkpoints`              | At least one                                                         |
| `checkpoints[].sequence`   | Integer ≥ 1, unique within the session: the checkpoint's id           |
| `checkpoints[].name`, `clue` | Non-empty                                                          |
| `checkpoints[].location`   | `{lat, long}`: the answer to the clue (see *Secrecy* below)            |
| `checkpoints[].proximity`  | Integer > 0: how many metres from `location` counts as "arrived"     |
| `checkpoints[].window`     | Optional `{opens-at, closes-at}`, `opens-at` < `closes-at`, both within the session's start/end |
| `checkpoints[].challenge`  | Optional visual challenge for the referee, see below. Without one, the referee's visual checks for the checkpoint are `skipped` |
| `challenge.scene`          | 1–1 000 characters. **Server-only**: what should be visible in the photo's background, written for the referee, not the player |
| `challenge.pose`           | 1–200 characters. **Player-facing**: the pose or action the player must show in the photo |
| `checkpoints[].reference-photos` | Optional, default empty, at most 5. **Server-only**: the moderator's own photos of the place, which the referee compares the photo with; see below |
| `teams`                    | Optional, default empty. Without teams, nobody can join the session |
| `teams[].name`             | 1–40 characters, unique within the session ignoring case. Shown to the team |
| `teams[].join-code`        | 6–32 letters, digits or `-`; surrounding spaces are trimmed. Unique **across the whole file**, ignoring case, because joining finds the session by the code alone. A credential (see *Secrecy*) |
| `teams[].order`            | Every checkpoint `sequence` in the session, each exactly once: the order this team visits them. Position 1 is its first checkpoint |
| `moderator-code`           | Optional. Authorises the moderator's endpoints for this session: same format as `join-code`, unique across the file **and different from every join code**. A credential (see *Secrecy*). Without one, the session can't be moderated |

A checkpoint's **visual challenge** tells the referee what to look for in the photo:

```json
"challenge": {
  "scene": "The Diana Memorial Fountain: a wide oval ring of pale granite with shallow water running through it, set in open lawn with trees behind.",
  "pose": "Side profile, looking to your left, with the landmark behind you."
}
```

Writing a good challenge:

- **`scene`** describes what the camera should see behind the player. Be concrete: materials,
  shapes, colours, what surrounds it. It's the answer to the clue, so it is never shown to
  anyone (see *Secrecy*).
- **`pose`** is what the player must do in the photo. It's shown only when the team checks in
  at the checkpoint, by [`POST …/arrive`](api.md#post-sessionssessionparticipantsparticipantarrive),
  and the referee judges the photo against the pose given there.

**Reference photos.** The moderator's own photos of each checkpoint, taken while setting up
the hunt. The [referee](api.md#reference-photos) compares each player's photo with them, so
`scene_matches` is judged against the place itself, not only its written `scene` (a later
change will also show them to the moderator beside a `pending` photo):

```json
"reference-photos": ["reference/fountain-north.jpg", "reference/fountain-south.jpg"]
```

- Each entry is a **relative path to a JPEG, resolved against the sessions file's directory**,
  and must stay inside it: no absolute paths, and no `..` or symlink that leads out.
- At startup every listed photo must exist, be no bigger than `GAME_SERVER_MAX_IMAGE_BYTES`,
  and decode with the same safe decoding (and pixel cap) as player photos. **Otherwise the
  server refuses to start**, so a broken seed fails before the game, not during it.
- Errors give the entry's **position, never its path**, because a file name can describe the
  place: `[0].checkpoints[1].reference-photos[0]: file not found`.
- At startup only the resolved paths are kept, not the images. The referee prepares a
  checkpoint's photos (upright, EXIF stripped, smaller) the first time a photo there is
  judged, and keeps them in memory after that.
- **Order matters.** The referee sends the first `GAME_SERVER_REFEREE_MAX_REFERENCES` (default
  `2`) with each photo, in the order listed. `0` turns references off.
- **They're sent to the model provider** (Anthropic) with every photo judged at the
  checkpoint, like the player's photo.

`sessions.example.json` leaves them out: it can't ship real photos, and a listed photo that's
missing stops the server.

Guidance for moderators:

- **Take them yourself, with nobody in shot.** They're the organisers' photos, not players', so
  the player-photo purge doesn't apply to them, and the players' privacy notice doesn't need
  to cover them.
- **Show what a player's camera will see behind them**: the landmark from where a player
  would stand, in daylight. Put the clearest ones first. Different angles help; the referee
  doesn't expect the same angle, light or season.
- **Keep them out of version control**, like `sessions.json`: they show the answer to each clue.

**Teams.** Each team gets its own join code (the "hunt code") and visits the checkpoints in
its **own order**, so teams don't trail each other from one checkpoint to the next or crowd
one place at once:

```json
"teams": [
  { "name": "Red Foxes",   "join-code": "FOX-7Q2K",   "order": [1, 2, 3] },
  { "name": "Blue Herons", "join-code": "HERON-4MXP", "order": [2, 3, 1] }
]
```

A team plays as **one participant**: joining gives it a `participant` id, which every
endpoint already uses. In the single-player first iteration, a team is one player with one
phone. Join codes compare ignoring case and surrounding spaces, so `fox-7q2k ` and `FOX-7Q2K`
are the same code.

Guidance for moderators:

- **Pick codes nobody can guess**: random letters and digits like `FOX-7Q2K`, not `TEAM-1`.
  Send each team only its own code.
- **Don't rename a team or change its order once it has joined.** Its progress is stored
  under its name.

**Moderator code.** Each session can have a `moderator-code`, which the moderator presents
to the moderator-only endpoints (start and stop the session, the overview, the traces, rulings
on photos) as `Authorization: Bearer <code>`. It's a stopgap until real accounts exist.

- **Same format as a join code**: 6–32 letters, digits or `-`, surrounding spaces stripped,
  compared ignoring case.
- **Unique across the whole file, and different from every join code**, so no code can open
  two doors. A clash is reported at the later entry's path, saying what it clashes with:
  `[1].moderator-code: same as a join code`. The code itself is never shown.
- **A session without one can't be moderated**: every moderator call for it gets a `401`.
- On a moderator route, an unknown session is `404 {"detail": "unknown session"}`. A missing
  header, another scheme, a wrong code or a session without a code all get the same `401`
  with `WWW-Authenticate: Bearer`, so a caller can't tell why:
  `{"detail": "moderator code required", "code": "moderator_unauthorised"}`. A session's code
  works for that session only, and is compared in constant time.
- **Pick one nobody can guess and keep it to the moderators.** Like join codes, it's never
  returned by an endpoint or logged.

Unknown fields are rejected everywhere. If the file can't be read, isn't valid JSON, breaks any
rule above or repeats a session id, the server **refuses to start**. The error lists each
problem as `path: message`, e.g. `[0].checkpoints[1].proximity: Input should be greater than 0`.

**Effective window:** the session's run, from the moderator's start to their stop (open-ended
while it runs), narrowed by the checkpoint's `window` if it has one. Before the start there is
none. A `window` that closes before a late start is never open; one that opens after an early
stop never opens. The planned `start-time` and `end-time` play no part.

## Published sessions

A hunt approved in the [hunt designer](api.md#hunt-designer) is **published** straight into the
running server, where teams can join it at once. Published sessions live in the database beside
the file, in two tables:

| Table | Content |
|-------|---------|
| `published_sessions` | `id` (the session's UUID), `document` (the session in exactly the sessions-file shape), `draft` (the designer draft it came from, if any) and `published_at` |
| `session_codes` | Every published join and moderator `code`, normalised (upper case, no surrounding spaces), with its `session` and `kind` (`join` or `moderator`). The code is the primary key, so the database refuses a code used twice, even if two publishes race |

**What's checked.** Publishing holds a session to the same rules as the file: times, team
orders, code formats and unique team names. Also, its id must be unused, and every join code
and its moderator code must differ from each other and from every code in the file and every
published code. Each problem is reported at its path, as the file loader does, and never with a
value, e.g. `teams[1].join-code: already in use`; then nothing is stored.

**No reference photos.** There are no files to resolve, so a published session has none, and the
referee judges `scene_matches` from the written scene alone. A session that names reference
photos is refused (`checkpoints[0].reference-photos: …`). Uploading them is a follow-up.

**Final once published.** A published session can't be edited or deleted: teams' progress is
stored under their names.

**Looking one up.** File sessions stay in memory. A published session is read from the database
on first use (by its id, or by a code), then kept in memory: it never changes. An unknown id
costs one indexed query, and published sessions are found again after a restart.

**Resetting the database deletes published hunts too.** `make db-reset` drops every table,
`published_sessions` and `session_codes` included, so never run it during a hunt.

## Secrecy

A checkpoint's coordinates are the answer to its clue. **No endpoint may return checkpoint
coordinates, or distances to them**, not even in error messages. The one endpoint that uses
them without submitting, [`POST /checkpoint/proximity`](../docs/api.md#post-checkpointproximity), answers a
rate-limited yes/no and nothing more. Validation errors from the
sessions file never echo input values, so coordinates don't reach the logs either. Keep the
real sessions file out of version control: `sessions.json` is git-ignored.

A checkpoint's **`challenge.scene`** is secret in the same way: it describes what the place
looks like, which gives away the answer. **No endpoint may return the scene**, and validation
errors never echo it. Only `challenge.pose` is player-facing. A test calls every route (success
and error paths) with a sentinel scene and fails if any response contains it, or if a route
is added without being covered.

**Teams' orders and join codes** are secret too:

- **No endpoint returns a team's `order`.** A team only ever learns its current clue. With
  different orders, one team's later clue is another team's current one.
- **Join codes are credentials.** No endpoint returns one and none is logged; teams aren't
  even printed with their codes. Validation errors report a bad or duplicate code by its path,
  e.g. `[1].teams[0].join-code: duplicate join code`, never by its value. `POST /join` never
  returns or logs the code it was given. Validation errors (422) never echo submitted values on
  any endpoint: FastAPI's default would return the whole body, code included. The every-route
  secrecy test checks that no response, on success or error paths, echoes a sentinel join code.

**Moderator codes** are credentials like join codes: excluded from `repr`, never returned
by an endpoint, never logged, and never echoed by a validation error (the `Authorization`
header's value included). The every-route secrecy test checks that a sentinel moderator code
appears in no response and no log line.

**The one deliberate exception: publishing.** When the organiser publishes a designer draft,
[`POST /designer/drafts/{draft}/publish`](api.md#post-designerdraftsdraftpublish) returns the
new session's join codes and moderator code, and
[`GET …/publication`](api.md#get-designerdraftsdraftpublication) returns them again, so the
organiser can hand them out. These are the only responses with codes in them, and only for the
organiser's key; the codes are still never logged. The every-route secrecy test covers both and
checks they never contain the file session's sentinel codes.

**Published sessions** are as secret as the file's: no coordinates, scenes, orders or codes in
any response or log line, and the repository never puts a code in a `repr`, a log line or an
error. The every-route secrecy test runs with the sentinel session in each source: in the file,
and published to the database.

**Reference photos** show what the place looks like, so they're secret like `challenge.scene`.
No endpoint returns a reference photo's path, and no participant endpoint returns a reference
photo or says how many a checkpoint has. Only the moderator sees them: the
[traces](api.md#get-sessionssessiontraces) list the ones sent with each photo, by position and
hash, the [review queue](api.md#get-sessionssessionreview) says how many each checkpoint has,
and [`…/reference-photos/{position}`](api.md#get-sessionssessioncheckpointssequencereference-photosposition)
serves each one, with the moderator code. Startup errors and the referee's warnings name an
entry by its position, never its path. The every-route secrecy test gives a reference photo a
sentinel file name and checks no response mentions it.

FastAPI also serves interactive API docs at `/docs` (Swagger UI) and `/redoc`, and the OpenAPI
schema at `/openapi.json`.

