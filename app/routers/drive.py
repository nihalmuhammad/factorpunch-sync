"""Send the daily attendance CSV to the restaurant's Google shared drive.

Any signed-in operator (admin or viewer) can press Send; only admins can
upload the service-account key, set the shared-drive folder or run a test.
See app/services/drive.py for why this is a service account and a shared drive.
"""

import json
import logging
import re
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

MSG_NOT_SET_UP = "Google Drive isn't set up yet. Ask an admin to finish it in Settings."

_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


class DriveConfigUpdate(BaseModel):
    # A shared drive ID, a folder ID inside it, or a pasted Drive link.
    folder: str = Field(..., max_length=500)


class DriveKeyUpload(BaseModel):
    # The service-account key file's text. Real keys are ~2.4 KB.
    key_json: str = Field(..., max_length=20000)


def parse_folder(value: str) -> Optional[str]:
    """Accept a raw ID or a Drive link (…/folders/<id>, …/drive/u/0/folders/<id>)."""
    value = (value or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    candidate = match.group(1) if match else value
    return candidate if _DRIVE_ID.match(candidate) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def operational_today() -> date:
    local = datetime.now(ZoneInfo(config.DEFAULT_DEVICE_TIMEZONE))
    return (local - timedelta(hours=_DAY_START_HOURS)).date()


def _get_or_create(db: Session) -> DriveIntegration:
    row = db.query(DriveIntegration).filter_by(id=1).first()
    if not row:
        row = DriveIntegration(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _ready(row: DriveIntegration) -> bool:
    return bool(row.sa_key and row.folder_id)


def _serialize(row: DriveIntegration, user: User) -> dict:
    data = {
        "ready": _ready(row),
        "folder_title": row.folder_title,
        "today": operational_today().isoformat(),
        "last_send_at": row.last_send_at,
        "last_send_ok": row.last_send_ok,
        "last_send_file": row.last_send_file,
        "last_send_by": row.last_send_by,
        "last_error": row.last_error,
    }
    if user.role == "admin":
        data.update({
            "key_set": bool(row.sa_key),
            "sa_email": row.sa_email,
            "key_uploaded_at": row.key_uploaded_at,
            "key_uploaded_by": row.key_uploaded_by,
            "folder_id": row.folder_id,
        })
    return data


@router.get("")
def get_status(db: Session = Depends(get_db), user: User = Depends(require_auth)):
    return _serialize(_get_or_create(db), user)


@router.put("")
def set_folder(payload: DriveConfigUpdate, request: Request,
               db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    folder_id = parse_folder(payload.folder)
    if not folder_id:
        raise HTTPException(status_code=400, detail=(
            "That doesn't look like a Google Drive folder. Open the shared drive in your browser "
            "and paste the address bar link, or the folder ID."))
    if folder_id != row.folder_id:
        row.folder_id = folder_id
        row.folder_title = None
        db.commit()
        audit.record(db, admin.username, "drive_config_change", ip=client_ip(request),
                     detail=f"shared drive folder set to {folder_id}")
    db.refresh(row)
    return _serialize(row, admin)


@router.post("/key")
def upload_key(payload: DriveKeyUpload, request: Request,
               db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    try:
        info = drive.parse_key(payload.key_json)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = _get_or_create(db)
    # Re-serialised from the parsed dict so stray text never lands in the DB.
    row.sa_key = json.dumps(info)
    row.sa_email = info["client_email"]
    row.key_uploaded_at = _now()
    row.key_uploaded_by = admin.username
    db.commit()
    db.refresh(row)
    audit.record(db, admin.username, "drive_key_upload", ip=client_ip(request),
                 detail=f"service account {row.sa_email} (key not logged)")
    return _serialize(row, admin)


@router.delete("/key")
def remove_key(request: Request, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    row = _get_or_create(db)
    had_key = bool(row.sa_key)
    row.sa_key = None
    row.sa_email = None
    row.key_uploaded_at = None
    row.key_uploaded_by = None
    db.commit()
    db.refresh(row)
    if had_key:
        audit.record(db, admin.username, "drive_key_remove", ip=client_ip(request),
                     detail="service-account key removed")
    return _serialize(row, admin)


@router.post("/test")
def test_connection(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Check the key works and the shared-drive folder is reachable, without uploading."""
    row = _get_or_create(db)
    if not _ready(row):
        raise HTTPException(status_code=400,
                            detail="Upload the key and set the shared-drive folder first.")
    try:
        with drive.make_client() as client:
            token = drive.access_token(client, json.loads(row.sa_key))
            row.folder_title = drive.check_target(client, token, row.folder_id)
    except DriveError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db.commit()
    db.refresh(row)
    return {**_serialize(row, admin), "test_ok": True}


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
        if not _ready(row):
            raise HTTPException(status_code=400, detail=MSG_NOT_SET_UP)

        content = build_attendance_csv(db, day, day).encode("utf-8")
        try:
            with drive.make_client() as client:
                token = drive.access_token(client, json.loads(row.sa_key))
                row.folder_title = drive.check_target(client, token, row.folder_id)
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
                     detail=f"{action} in shared drive folder '{row.folder_title}'")
        return {**_serialize(row, user), "replaced": result["replaced"]}
    finally:
        _send_lock.release()
