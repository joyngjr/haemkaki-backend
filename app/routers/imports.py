"""Imports from another tracker: batches with a dry run and an undo.

One request carries up to 500 rows in the ledger vocabulary. `app/importing.py`
decides what to do with each and writes them in one transaction with a
receipt, so the whole batch can be taken back out. The MCP server offers the
same operation as a tool; this route is the plain-HTTP way in, and how the
importer is exercised without an MCP client.

No authentication, for the same reason as `users.py`.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app import importing
from app.db import get_session
from app.models import ImportBatch
from app.routers.status import status_read
from app.routers.users import _get_or_404
from app.schemas import ImportBatchRead, ImportReport, ImportRequest

router = APIRouter(prefix="/users/{user_id}/imports", tags=["imports"])


def _batch_or_404(session: Session, user_id: int, batch_id: int) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found")
    return batch


@router.post("", response_model=ImportReport)
def import_events(
    user_id: int, payload: ImportRequest, session: Session = Depends(get_session)
) -> ImportReport:
    """Plan the batch, and write it unless this is a dry run.

    Always 200 for a plan that could be written — a dry run creates nothing,
    so 201 would be a lie. A real import with any row in error writes nothing
    and answers 422 with the report as `detail`, so the caller sees every
    row's verdict. Skipped rows are not errors: they are what `on_conflict`
    asked for.
    """
    user = _get_or_404(session, user_id)
    plan = importing.plan_import(session, user_id, payload.events, payload.on_conflict)
    if payload.dry_run:
        return importing.report(plan, dry_run=True, source=payload.source)
    if not plan.ok:
        refused = importing.report(plan, dry_run=False, source=payload.source)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, refused.model_dump(mode="json"))
    batch = importing.apply_import(session, plan, payload.source)
    return importing.report(
        plan, dry_run=False, source=payload.source, batch=batch, status=status_read(session, user)
    )


@router.get("", response_model=list[ImportBatchRead])
def list_imports(user_id: int, session: Session = Depends(get_session)) -> list[ImportBatchRead]:
    """Newest first, each with how many of its events still exist."""
    _get_or_404(session, user_id)
    return [
        importing.batch_read(session, batch) for batch in importing.list_batches(session, user_id)
    ]


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
def undo_import(user_id: int, batch_id: int, session: Session = Depends(get_session)) -> None:
    """Removes the events the import created. Rows it replaced are not restored."""
    importing.undo_import(session, _batch_or_404(session, user_id, batch_id))
