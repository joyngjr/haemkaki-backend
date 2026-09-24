"""HaemKaki as tools for an assistant: the MCP server.

Served at `/mcp` by `main.py` over Streamable HTTP. A user connects Claude.ai,
Claude Desktop or Claude Code to it, hands the assistant a spreadsheet, and
the assistant maps the rows and calls `import_events`; nothing here parses a
file. No authentication, like the REST API: the URL is the key.

Every tool is a plain `def` the SDK runs on a worker thread, opening its own
session. Tools never call the routers' `_get_or_404` — an HTTPException means
nothing to an MCP client — and raise `ToolError` instead, whose message the
assistant reads. Otherwise they reuse the routers' own helpers and
`app/importing.py`, so every ledger rule is enforced in one place.

The descriptions below are the assistant's whole user interface. Keep them
plain and exact.
"""

from datetime import date
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from app import importing, services
from app.db import engine
from app.models import ImportBatch, TrackingEvent, User
from app.routers.events import LIMIT_MAX
from app.routers.schedules import replace_series
from app.routers.status import status_read
from app.routers.users import _profile_response
from app.schemas import (
    IMPORT_ROWS_MAX,
    IMPORT_SOURCE_MAX,
    EventCreate,
    ImportBatchRead,
    ImportReport,
    OnConflict,
    ProfileRead,
    ScheduleCreate,
    ScheduleRead,
    StatusRead,
    TrackingEventRead,
    event_read,
    schedule_read,
)

INSTRUCTIONS = (
    "HaemKaki tracks factor supply for people with haemophilia: refills in, doses out, a "
    "prophylaxis routine, and the vials on hand derived from that ledger.\n\n"
    "Rules for writing:\n"
    "- Amounts are whole VIALS, never IU. If a source records IU, ask the user how many IU "
    "one vial holds and convert; never guess.\n"
    "- Dates are calendar days in Singapore time, written YYYY-MM-DD. There is no time of "
    "day. `status.as_of` from get_profile is today.\n"
    "- Nothing is authenticated. Confirm with the user which profile you are writing into.\n"
    "- Five kinds of row: refill (vials that arrived); prophylaxis (the routine dose — pass "
    "`vials` only when the source says how big it was, otherwise the routine sizes it, and "
    "with no routine a routine-sized dose charges nothing); on-demand (a dose given for a "
    "bleed — this is how the app marks a bleed; add `bleed_nature`: spontaneous or traumatic "
    "only when the source says which); follow-up (a later dose for that bleed); "
    "makeup (a planned dose taken late: occurred_on is the day taken, missed_on the planned "
    "day it was owed for, amount {source: custom, vials: N} or {source: routine}).\n"
    "- One refill and one factor use per day.\n"
    "- A missed dose is not a row and cannot be written: it is derived from the routine as a "
    "planned day already past with no factor use on it, and `status.missed_doses` lists the "
    "recent ones. To record one, import the routine for that period and leave the day empty; "
    "to say it was taken late, import a makeup dose naming it.\n\n"
    "Importing another tracker's history: list_profiles; get_profile to see whether a "
    "routine exists (set_routine, or pass explicit vials, when the history predates it); "
    "import_events with dry_run=true; show the user what would be created, replaced, "
    "skipped and rejected; only after they confirm, import_events with dry_run=false. Then "
    "report vials on hand and days of cover, and say the Tracker shows the rows the next "
    "time it opens. undo_import reverses a batch."
)

mcp = MCPServer("HaemKaki", version="0.1.0", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(destructive_hint=True, idempotent_hint=False, open_world_hint=False)
WRITES_IDEMPOTENT = ToolAnnotations(
    destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


# A bare list return is wrapped as {"result": [...]}; a model keeps the key meaningful.
class ProfilesRead(BaseModel):
    profiles: list[ProfileRead]


class ProfileStatusRead(BaseModel):
    profile: ProfileRead
    status: StatusRead


class EventsRead(BaseModel):
    events: list[TrackingEventRead]


class ImportsRead(BaseModel):
    imports: list[ImportBatchRead]


class UndoRead(BaseModel):
    batch_id: int
    events_removed: int
    status: StatusRead


def _user(session: Session, profile_id: int) -> User:
    user = session.get(User, profile_id)
    if user is None:
        raise ToolError(f"No profile with id {profile_id}. Call list_profiles to see who exists.")
    return user


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
        for error in exc.errors()
    )


@mcp.tool(
    name="list_profiles",
    title="List profiles",
    description=(
        "Every profile on this server, oldest first, with id, name, factor type, vials on "
        "hand, days of cover and the clinical profile (its prophylactic medication carries "
        "the usual dose in vials, which helps when a source only says a dose was taken). Call "
        "this first: every other tool takes a profile_id."
    ),
    annotations=READ_ONLY,
)
def list_profiles() -> ProfilesRead:
    with Session(engine) as session:
        users = session.exec(select(User).order_by(User.created_at, User.id)).all()
        return ProfilesRead(
            profiles=[ProfileRead.model_validate(_profile_response(session, u)) for u in users]
        )


@mcp.tool(
    name="get_profile",
    title="Get a profile and its status",
    description=(
        "One profile with its folded status: vials on hand, days of cover, the routine in "
        "force, the next planned dose, the last bleed and the five most recent events. Use it "
        "before an import to learn whether a routine exists, and after one to report the new "
        "balance."
    ),
    annotations=READ_ONLY,
)
def get_profile(profile_id: int) -> ProfileStatusRead:
    with Session(engine) as session:
        user = _user(session, profile_id)
        return ProfileStatusRead(
            profile=ProfileRead.model_validate(_profile_response(session, user)),
            status=status_read(session, user),
        )


@mcp.tool(
    name="list_events",
    title="List ledger events",
    description=(
        "The ledger, oldest first, up to 500 rows, optionally between since and until "
        "(YYYY-MM-DD, inclusive). Each row carries applied_vials: the vials the supply count "
        "charged for it (positive for a refill, negative for a dose, zero when the amount is "
        "unknown). Check what is already logged before importing; verify afterwards."
    ),
    annotations=READ_ONLY,
)
def list_events(
    profile_id: int,
    since: Annotated[date | None, Field(description="Earliest occurred_on to include.")] = None,
    until: Annotated[date | None, Field(description="Latest occurred_on to include.")] = None,
) -> EventsRead:
    with Session(engine) as session:
        _user(session, profile_id)
        statement = select(TrackingEvent).where(TrackingEvent.user_id == profile_id)
        if since is not None:
            statement = statement.where(TrackingEvent.occurred_on >= since)
        if until is not None:
            statement = statement.where(TrackingEvent.occurred_on <= until)
        statement = statement.order_by(TrackingEvent.occurred_on, TrackingEvent.id).limit(LIMIT_MAX)
        events = list(session.exec(statement).all())
        applied = services.applied_vials(
            events,
            services.load_schedules(session, profile_id),
            services.load_plans(session, profile_id),
        )
        return EventsRead(events=[event_read(e, applied.get(e.id or 0, 0)) for e in events])


@mcp.tool(
    name="set_routine",
    title="Set the prophylaxis routine",
    description=(
        "Set the prophylaxis routine: from start_on, either every interval_days days or on "
        "fixed weekdays (0 = Sunday … 6 = Saturday), vials per dose. Replaces the existing "
        "routine — the tracker keeps one — and leaves logged doses alone. Set it before "
        "importing prophylaxis rows that carry no vials of their own."
    ),
    annotations=WRITES_IDEMPOTENT,
)
def set_routine(
    profile_id: int,
    start_on: Annotated[date, Field(description="First day of the routine, YYYY-MM-DD.")],
    vials: Annotated[int, Field(description="Vials per dose.")],
    interval_days: Annotated[
        int | None, Field(description="Every N days, or give weekdays.")
    ] = None,
    weekdays: Annotated[list[int] | None, Field(description="Fixed days, 0 = Sunday.")] = None,
) -> ScheduleRead:
    with Session(engine) as session:
        _user(session, profile_id)
        try:
            payload = ScheduleCreate(
                start_on=start_on,
                vials=vials,
                interval_days=interval_days,
                weekdays=weekdays,
                replace=True,
            )
        except ValidationError as exc:
            raise ToolError(_validation_message(exc)) from exc
        return schedule_read(replace_series(session, profile_id, payload))


@mcp.tool(
    name="import_events",
    title="Import ledger rows",
    description=(
        "Write rows from another tracker into the ledger, up to 500 per call, in the ledger "
        "vocabulary (see the server instructions for the five kinds). Defaults to "
        "dry_run=true, which validates and reports what would happen without writing: show "
        "that to the user, and call again with dry_run=false only after they confirm. Rules: "
        "one refill and one factor use (prophylaxis, on-demand, follow-up, makeup) per day; "
        "days after today are rejected; a row identical to one already logged is skipped as "
        "already present; a missed dose is derived, not a row, so a sheet's missed entries "
        "are dropped and the routine for that period is what makes them show. on_conflict "
        "governs collisions with rows already in the ledger: skip keeps the ledger's row, "
        "replace deletes it and writes yours. Two rows in one batch that collide are "
        "an error, and a real import with any error writes nothing — fix the batch and call "
        "again. The report lists every row with its action and reason, and after a real "
        "import the batch_id for undo_import and the profile's new status."
    ),
    annotations=WRITES,
)
def import_events(
    profile_id: int,
    events: Annotated[
        list[EventCreate],
        Field(min_length=1, max_length=IMPORT_ROWS_MAX, description="Rows to import."),
    ],
    source: Annotated[
        str,
        Field(max_length=IMPORT_SOURCE_MAX, description="The file name or app the rows came from."),
    ] = "assistant",
    on_conflict: OnConflict = "skip",
    dry_run: bool = True,
) -> ImportReport:
    source = source.strip() or "assistant"
    with Session(engine) as session:
        user = _user(session, profile_id)
        plan = importing.plan_import(session, profile_id, events, on_conflict)
        if dry_run or not plan.ok:
            return importing.report(plan, dry_run=dry_run, source=source)
        batch = importing.apply_import(session, plan, source)
        return importing.report(
            plan, dry_run=False, source=source, batch=batch, status=status_read(session, user)
        )


@mcp.tool(
    name="list_imports",
    title="List imports",
    description=(
        "The imports made for a profile, newest first, with how many rows each created and "
        "how many of those events still exist."
    ),
    annotations=READ_ONLY,
)
def list_imports(profile_id: int) -> ImportsRead:
    with Session(engine) as session:
        _user(session, profile_id)
        batches = importing.list_batches(session, profile_id)
        return ImportsRead(imports=[importing.batch_read(session, b) for b in batches])


@mcp.tool(
    name="undo_import",
    title="Undo an import",
    description=(
        "Delete the events an import created (rows it replaced are not restored) and forget "
        "the batch. Returns how many events were removed and the profile's status after."
    ),
    annotations=WRITES_IDEMPOTENT,
)
def undo_import(profile_id: int, batch_id: int) -> UndoRead:
    with Session(engine) as session:
        user = _user(session, profile_id)
        batch = session.get(ImportBatch, batch_id)
        if batch is None or batch.user_id != profile_id:
            raise ToolError(
                f"No import with id {batch_id} for profile {profile_id}. Call list_imports."
            )
        removed = importing.undo_import(session, batch)
        return UndoRead(
            batch_id=batch_id, events_removed=removed, status=status_read(session, user)
        )


@mcp.prompt(
    name="import_tracker",
    title="Import from another tracker",
    description="The procedure for moving a spreadsheet or another app's export into HaemKaki.",
)
def import_tracker(
    profile_id: Annotated[str, Field(description="The profile id to import into, if known.")] = "",
) -> str:
    target = f"profile {profile_id}" if profile_id else "the profile the user names (list_profiles)"
    return (
        f"Import the user's tracker history into HaemKaki for {target}.\n\n"
        "1. Ask for the file or the pasted rows if you do not have them yet.\n"
        "2. Work out which columns hold the date, the type of entry, the amount and any "
        "notes. Say what you found and what you are unsure about.\n"
        "3. Map each row to one kind: refill, prophylaxis, on-demand (a bleed treated), "
        "follow-up or makeup (a dose taken late, naming the day it was owed for). A row "
        "that only says a dose was missed has no kind — say so, and set the routine "
        "instead, which is what makes the empty day read as missed. Amounts are whole "
        "vials: if the sheet records IU, ask how many IU one vial holds before converting, "
        "and show the conversion.\n"
        "4. Dates are Singapore calendar days as YYYY-MM-DD; drop the time of day.\n"
        "5. Call get_profile. If the history predates the routine, either set_routine first "
        "or give each prophylaxis row its vials.\n"
        "6. Call import_events with dry_run=true and show the user a table of what would be "
        "created, replaced, skipped and rejected. Fix or drop rejected rows.\n"
        "7. Only after the user confirms, call import_events with dry_run=false.\n"
        "8. Report the new vials on hand and days of cover, mention the batch id for "
        "undo_import, and say that the Tracker shows the rows the next time it opens."
    )
