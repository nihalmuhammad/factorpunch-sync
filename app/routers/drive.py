"""Send the daily attendance CSV to Google Drive.

Any signed-in operator (admin or viewer) can press Send; only admins can sign
in to Google, change the folder or disconnect. See app/services/drive.py for
why sign-in is a device flow and why the app owns its folder.
"""

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import audit, config
from app.database import get_db
from app.deps import require_admin, require_auth
from app.models import DriveIntegration, User
from app.net import client_ip
from app.routers.attendance import build_attendance_csv
from app.services import drive
from app.services.drive import DriveError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["drive"], dependencies=[Depends(require_auth)])

# One send at a time, so two staff pressing together can't both miss the
# existing file and create a duplicate.
_send_lock = threading.Lock()

# The operational day starts at 03:00 (docs/ATTENDANCE_POLICY.md), so at
# 01:00 "today" still means yesterday's trading day.
_DAY_START_HOURS = 3

MSG_NOT_SET_UP = "Google Drive isn't set up yet. Ask an admin to connect it in Settings."


class DriveConfigUpdate(BaseModel):
    client_id: Optional[str] = Field(None, max_length=300)
    client_secret: Optional[str] = Field(None, max_length=300)
    folder_name: Optional[str] = Field(None, min_length=1, max_length=200)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def operational_today() -> date:
    local = datetime.now(ZoneInfo(config.DEFAULT_DEVICE_TIMEZONE))
    return (local - timedelta(hours=_DAY_START_HOURS)).date()


def _get_or_create(db: Session) -> DriveIntegration:
    row = db.query(DriveIntegration).filter_by(id=1).first()
    if not row:
        row = DriveIntegration(id=1, folder_name="FactorPunch Attendance")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _pending(row: DriveIntegration) -> Optional[dict]:
    if not row.pending_device_code or not row.pending_expires_at:
        return None
    if row.pending_expires_at <= _now():
        return None
    return {
        "user_code": row.pending_user_code,
        "verification_url": row.pending_verification_url,
        "expires_at": row.pending_expires_at,
        "interval": row.pending_interval or 5,
    }


def _clear_pending(row: DriveIntegration) -> None:
    row.pending_device_code = None
    row.pending_user_code = None
    row.pending_verification_url = None
    row.pending_expires_at = None
    row.pending_interval = None


def _serialize(row: DriveIntegration, user: User) -> dict:
    data = {
        "connected": bool(row.refresh_token),
        "folder_name": row.folder_name,
        "today": operational_today().isoformat(),
        "last_send_at": row.last_send_at,
        "last_send_ok": row.last_send_ok,
        "last_send_file": row.last_send_file,
        "last_send_by": row.last_send_by,
        "last_error": row.last_error,
    }
    if user.role == "admin":
        data.update({
            "client_id": row.client_id,
            "client_secret_set": bool(row.client_secret),
            "connected_at": row.connected_at,
            "connected_by": row.connected_by,
            "pending": _pending(row),
        })
    return data


@router.get("")
def get_status(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    return _serialize(_get_or_create(db), user)


@router.put("")
def update_config(payload: DriveConfigUpdate, request: Request,
                  db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    updates = payload.model_dump(exclude_unset=True)
    # Write-only secret: blank means "leave it", never "erase it".
    if not updates.get("client_secret"):
        updates.pop("client_secret", None)
    if "client_id" in updates:
        updates["client_id"] = (updates["client_id"] or "").strip() or None
    if "folder_name" in updates:
        updates["folder_name"] = updates["folder_name"].strip()
        if not updates["folder_name"]:
            raise HTTPException(status_code=400, detail="Folder name cannot be empty")
        if updates["folder_name"] != row.folder_name:
            row.folder_id = None  # next send creates the newly named folder
    credentials_changed = ("client_id" in updates and updates["client_id"] != row.client_id) \
        or "client_secret" in updates
    for field, value in updates.items():
        setattr(row, field, value)
    if credentials_changed and row.refresh_token:
        # A grant belongs to one OAuth client; it can't be reused with another.
        row.refresh_token = None
        row.folder_id = None
        row.connected_at = None
        row.connected_by = None
    db.commit()
    db.refresh(row)
    if updates:
        secret = "client_secret" in updates
        audit.record(db, admin.username, "drive_config_change", ip=client_ip(request),
                     detail=f"fields changed: {', '.join(sorted(updates))}"
                     + (" (secret value not logged)" if secret else ""))
    return _serialize(row, admin)


@router.post("/connect")
def start_connect(request: Request, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    if not row.client_id or not row.client_secret:
        raise HTTPException(status_code=400,
                            detail="Save the OAuth client ID and secret before connecting.")
    try:
        with drive.make_client() as client:
            flow = drive.start_device_flow(client, row.client_id)
    except DriveError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    row.pending_device_code = flow["device_code"]
    row.pending_user_code = flow["user_code"]
    row.pending_verification_url = flow.get("verification_url") or flow.get("verification_uri")
    row.pending_expires_at = _now() + timedelta(seconds=int(flow.get("expires_in", 1800)))
    row.pending_interval = int(flow.get("interval", 5))
    db.commit()
    db.refresh(row)
    return _serialize(row, admin)


@router.post("/connect/poll")
def poll_connect(request: Request, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    if not _pending(row):
        _clear_pending(row)
        db.commit()
        raise HTTPException(status_code=400,
                            detail="No sign-in in progress, or the code expired. Start again.")
    try:
        with drive.make_client() as client:
            tokens = drive.poll_device_flow(client, row.client_id, row.client_secret,
                                            row.pending_device_code)
    except DriveError as exc:
        _clear_pending(row)
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    if tokens is None:
        return {**_serialize(row, admin), "status": "pending"}
    if not tokens.get("refresh_token"):
        _clear_pending(row)
        db.commit()
        raise HTTPException(status_code=400,
                            detail="Google did not return a long-lived sign-in. Start again.")
    row.refresh_token = tokens["refresh_token"]
    row.connected_at = _now()
    row.connected_by = admin.username
    row.folder_id = None
    row.last_error = None
    _clear_pending(row)
    db.commit()
    db.refresh(row)
    audit.record(db, admin.username, "drive_connect", ip=client_ip(request),
                 detail="Google Drive signed in")
    return {**_serialize(row, admin), "status": "connected"}


@router.post("/disconnect")
def disconnect(request: Request, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    token = row.refresh_token
    row.refresh_token = None
    row.folder_id = None
    row.connected_at = None
    row.connected_by = None
    _clear_pending(row)
    db.commit()
    db.refresh(row)
    if token:
        with drive.make_client() as client:
            drive.revoke(client, token)
        audit.record(db, admin.username, "drive_disconnect", ip=client_ip(request),
                     detail="Google Drive signed out")
    return _serialize(row, admin)


@router.post("/send")
def send(request: Request, day: Optional[date] = Query(None),
         db: Session = Depends(get_db), user: User = Depends(require_auth)):
    """Build attendance-<day>.csv and upload it, replacing a same-day file."""
    today = operational_today()
    day = day or today
    if day > today:
        raise HTTPException(status_code=400, detail="You can't send a day that hasn't happened yet.")
    filename = f"attendance-{day.isoformat()}.csv"
    ip = client_ip(request)

    if not _send_lock.acquire(timeout=60):
        raise HTTPException(status_code=409, detail="Another send is in progress. Try again in a minute.")
    try:
        row = _get_or_create(db)
        if not (row.refresh_token and row.client_id and row.client_secret):
            raise HTTPException(status_code=400, detail=MSG_NOT_SET_UP)

        content = build_attendance_csv(db, day, day).encode("utf-8")
        try:
            with drive.make_client() as client:
                token = drive.access_token(client, row.client_id, row.client_secret,
                                           row.refresh_token)
                row.folder_id = drive.ensure_folder(client, token, row.folder_id, row.folder_name)
                result = drive.upload_csv(client, token, row.folder_id, filename, content)
        except DriveError as exc:
            message = str(exc)
            row.last_send_at = _now()
            row.last_send_ok = False
            row.last_send_file = filename
            row.last_send_by = user.username
            row.last_error = message
            db.commit()
            audit.record(db, user.username, "drive_send", target=filename, ip=ip,
                         detail=f"failed: {message}")
            raise HTTPException(status_code=502, detail=message)

        row.last_send_at = _now()
        row.last_send_ok = True
        row.last_send_file = filename
        row.last_send_by = user.username
        row.last_error = None
        db.commit()
        db.refresh(row)
        action = "replaced" if result["replaced"] else "created"
        audit.record(db, user.username, "drive_send", target=filename, ip=ip,
                     detail=f"{action} in Drive folder '{row.folder_name}'")
        return {**_serialize(row, user), "replaced": result["replaced"]}
    finally:
        _send_lock.release()
