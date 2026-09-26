"""Bulk import from another tracker: plan, apply, undo.

`plan_import` reads the ledger and decides row by row what an import would do,
and never writes. `apply_import` does exactly what a plan says, in one
transaction, and leaves an `ImportBatch` receipt so `undo_import` can take the
rows back out. `POST /users/{id}/imports` and the MCP tool both call these;
neither parses spreadsheets — rows arrive already in the ledger vocabulary
(`EventCreate`), mapped by whoever read the file.

The rules mirror the tracker's own per-day invariants (`withEntry` and
`PROPHYLAXIS_SUPERSEDES` in `src/lib/tracker-entries.ts`, `saveCountedUse` in
`src/pages/Tracker.tsx`), which the API never enforced on its own:

- Only rows on the same `occurred_on` interact.
- A refill collides with another refill and nothing else; so does a count.
- The four factor uses — prophylaxis, on-demand, follow-up, makeup — collide
  with each other: one use per day.
- A missed dose is nothing to import. It is derived from the routine (a
  planned day with no use on it), so a sheet's "missed" rows are dropped by
  whoever maps the file; what does come across is the routine that makes the
  gap visible, and a `makeup` row for a dose that was taken late.
- A row identical to one already in the ledger, or earlier in the batch, is
  skipped as already present, so re-running the same sheet is idempotent.
- A collision with the ledger is skipped, or replaces the ledger's rows when
  the caller asks. A collision inside the batch is an error whatever the
  caller asks: two uses on one day have to be merged by whoever mapped the
  sheet, not silently dropped.
- A row dated after today in Singapore is an error; the tracker cannot log
  the future.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, col, select

from app import services
from app.models import EventKind, ImportBatch, TrackingEvent
from app.schemas import (
    EventCreate,
    ImportAction,
    ImportBatchRead,
    ImportReason,
    ImportReport,
    ImportRowResult,
    OnConflict,
    StatusRead,
    to_event,
)

#: The kinds that take factor out of the cupboard: one per day.
USE_KINDS = frozenset(
    {
        EventKind.prophylaxis.value,
        EventKind.on_demand.value,
        EventKind.follow_up.value,
        EventKind.makeup.value,
    }
)
#: Columns that do not make a row the same row.
IDENTITY_EXCLUDE = {"id", "user_id", "created_at", "updated_at"}


def collides(a: str, b: str) -> bool:
    """Whether two kinds cannot share a day."""
    if a == b:
        return True
    return a in USE_KINDS and b in USE_KINDS


def already_used_on_missed_day(
    row: TrackingEvent,
    ledger_by_day: dict[date, list[TrackingEvent]],
    pending_by_day: dict[date, list["PlannedRow"]],
) -> list[int] | None:
    """The uses already logged on the day a makeup row claims to make up for.

    None when the row is not a makeup, or when that day is genuinely empty. An
    empty list means the clash is with an earlier row of the same batch, which
    has no id yet. A day with a use on it was not missed, and charging both
    that use and the makeup would take the same dose out of the cupboard twice.
    """
    if row.kind != EventKind.makeup.value or row.missed_on is None:
        return None
    logged = [
        event.id
        for event in ledger_by_day.get(row.missed_on, [])
        if event.kind in USE_KINDS and event.id is not None
    ]
    if logged:
        return logged
    if any(planned.row.kind in USE_KINDS for planned in pending_by_day.get(row.missed_on, [])):
        return []
    return None


def identity(row: TrackingEvent) -> dict:
    """What makes two rows the same entry: every column the user could set."""
    return row.model_dump(exclude=IDENTITY_EXCLUDE)


@dataclass
class PlannedRow:
    index: int
    payload: EventCreate
    #: Unsaved, built by `to_event`; written as-is by `apply_import`.
    row: TrackingEvent
    action: ImportAction
    reason: ImportReason | None = None
    existing_ids: list[int] = field(default_factory=list)
    conflicts_with_index: int | None = None
    message: str = ""
    event_id: int | None = None

    @property
    def writes(self) -> bool:
        return self.action in (ImportAction.create, ImportAction.replace)


@dataclass
class ImportPlan:
    user_id: int
    on_conflict: OnConflict
    rows: list[PlannedRow]

    @property
    def ok(self) -> bool:
        return all(row.action is not ImportAction.error for row in self.rows)

    @property
    def replaced_ids(self) -> list[int]:
        return [
            event_id
            for row in self.rows
            if row.action is ImportAction.replace
            for event_id in row.existing_ids
        ]


def plan_import(
    session: Session,
    user_id: int,
    events: Sequence[EventCreate],
    on_conflict: OnConflict,
    today: date | None = None,
) -> ImportPlan:
    """Decide what each row would do against the ledger as it is now.

    Rows keep the caller's order, so `index` matches the list they sent. The
    ledger is read once; at household scale that is cheaper than a query per
    row and keeps the decision consistent within the batch.
    """
    today = today or services.today_sgt()
    ledger = session.exec(
        select(TrackingEvent)
        .where(TrackingEvent.user_id == user_id)
        .order_by(TrackingEvent.occurred_on, TrackingEvent.id)
    ).all()
    by_day: dict[date, list[TrackingEvent]] = {}
    for event in ledger:
        by_day.setdefault(event.occurred_on, []).append(event)
    # Rows of this batch that will be written, by day, for the in-batch checks.
    pending: dict[date, list[PlannedRow]] = {}
    rows: list[PlannedRow] = []

    for index, payload in enumerate(events):
        row = to_event(payload, user_id)
        planned = PlannedRow(index=index, payload=payload, row=row, action=ImportAction.create)
        day = row.occurred_on
        label = f"{row.kind} on {day.isoformat()}"
        key = identity(row)
        same_day_ledger = by_day.get(day, [])
        same_day_batch = pending.get(day, [])

        if day > today:
            planned.action = ImportAction.error
            planned.reason = ImportReason.future_date
            planned.message = f"{label} is after today ({today.isoformat()})"
        elif twin := next((p for p in same_day_batch if identity(p.row) == key), None):
            planned.action = ImportAction.skip
            planned.reason = ImportReason.already_present
            planned.conflicts_with_index = twin.index
            planned.message = f"identical to row {twin.index}"
        elif twin := next((e for e in same_day_ledger if identity(e) == key), None):
            planned.action = ImportAction.skip
            planned.reason = ImportReason.already_present
            planned.existing_ids = [twin.id] if twin.id is not None else []
            planned.message = f"already in the ledger as event {twin.id}"
        elif clash := next((p for p in same_day_batch if collides(p.row.kind, row.kind)), None):
            planned.action = ImportAction.error
            planned.reason = ImportReason.in_batch_conflict
            planned.conflicts_with_index = clash.index
            planned.message = (
                f"row {clash.index} already logs a {clash.row.kind} on {day.isoformat()}; "
                "one factor use per day — merge them"
            )
        elif (claimed := already_used_on_missed_day(row, by_day, pending)) is not None:
            planned.action = ImportAction.error
            planned.reason = ImportReason.not_missed
            planned.existing_ids = claimed
            planned.message = (
                f"{label} makes up for {row.missed_on.isoformat() if row.missed_on else '?'}, "
                "but a factor use is already logged on that day — it was not missed"
            )
        elif clashes := [e for e in same_day_ledger if collides(e.kind, row.kind)]:
            planned.existing_ids = [e.id for e in clashes if e.id is not None]
            planned.reason = ImportReason.conflict
            kinds = ", ".join(sorted({e.kind for e in clashes}))
            if on_conflict == "replace":
                planned.action = ImportAction.replace
                planned.message = (
                    f"replaces the {kinds} already logged on {day.isoformat()} "
                    f"(event ids {planned.existing_ids})"
                )
            else:
                planned.action = ImportAction.skip
                planned.message = f"a {kinds} is already logged on {day.isoformat()}"
        else:
            planned.message = label

        if planned.writes:
            pending.setdefault(day, []).append(planned)
        rows.append(planned)

    return ImportPlan(user_id=user_id, on_conflict=on_conflict, rows=rows)


def apply_import(session: Session, plan: ImportPlan, source: str) -> ImportBatch:
    """Write the plan in one transaction and return its receipt.

    Refuses a plan with any row in error: the caller previews first, and a
    partial write would need a partial undo.
    """
    if not plan.ok:
        raise ValueError("a plan with rows in error cannot be applied")
    for event_id in plan.replaced_ids:
        existing = session.get(TrackingEvent, event_id)
        if existing is not None and existing.user_id == plan.user_id:
            session.delete(existing)
    written = [row for row in plan.rows if row.writes]
    for row in written:
        session.add(row.row)
    session.flush()
    for row in written:
        row.event_id = row.row.id
    batch = ImportBatch(
        user_id=plan.user_id,
        source=source,
        event_ids_json=json.dumps([row.event_id for row in written]),
        rows_received=len(plan.rows),
        rows_created=sum(row.action is ImportAction.create for row in plan.rows),
        rows_replaced=sum(row.action is ImportAction.replace for row in plan.rows),
        rows_skipped=sum(row.action is ImportAction.skip for row in plan.rows),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def undo_import(session: Session, batch: ImportBatch) -> int:
    """Delete what the import created — rows it replaced are gone for good —
    and forget the batch. Returns how many events were removed."""
    removed = 0
    for event_id in json.loads(batch.event_ids_json):
        event = session.get(TrackingEvent, event_id)
        if event is not None and event.user_id == batch.user_id:
            session.delete(event)
            removed += 1
    session.delete(batch)
    session.commit()
    return removed


def list_batches(session: Session, user_id: int) -> list[ImportBatch]:
    """Newest first."""
    return list(
        session.exec(
            select(ImportBatch)
            .where(ImportBatch.user_id == user_id)
            .order_by(col(ImportBatch.created_at).desc(), col(ImportBatch.id).desc())
        ).all()
    )


def batch_read(session: Session, batch: ImportBatch) -> ImportBatchRead:
    event_ids: list[int] = json.loads(batch.event_ids_json)
    remaining = 0
    if event_ids:
        remaining = len(
            session.exec(
                select(TrackingEvent.id).where(
                    TrackingEvent.user_id == batch.user_id,
                    col(TrackingEvent.id).in_(event_ids),
                )
            ).all()
        )
    return ImportBatchRead(
        id=batch.id,
        source=batch.source,
        created_at=batch.created_at,
        rows_received=batch.rows_received,
        rows_created=batch.rows_created,
        rows_replaced=batch.rows_replaced,
        rows_skipped=batch.rows_skipped,
        event_ids=event_ids,
        events_remaining=remaining,
    )


def report(
    plan: ImportPlan,
    *,
    dry_run: bool,
    source: str,
    batch: ImportBatch | None = None,
    status: StatusRead | None = None,
) -> ImportReport:
    """The plan as the API and the MCP tool report it. `written` is simply
    whether a batch exists: a dry run and a refused import look the same."""
    return ImportReport(
        dry_run=dry_run,
        written=batch is not None,
        batch_id=batch.id if batch is not None else None,
        source=source,
        on_conflict=plan.on_conflict,
        rows_received=len(plan.rows),
        rows_created=sum(row.action is ImportAction.create for row in plan.rows),
        rows_replaced=sum(row.action is ImportAction.replace for row in plan.rows),
        rows_skipped=sum(row.action is ImportAction.skip for row in plan.rows),
        rows_errored=sum(row.action is ImportAction.error for row in plan.rows),
        rows=[
            ImportRowResult(
                index=row.index,
                kind=EventKind(row.row.kind),
                occurred_on=row.row.occurred_on,
                action=row.action,
                reason=row.reason,
                existing_ids=row.existing_ids,
                conflicts_with_index=row.conflicts_with_index,
                event_id=row.event_id,
                message=row.message,
            )
            for row in plan.rows
        ],
        status=status,
    )
