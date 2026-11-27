"""Google Drive upload for the daily attendance CSV.

Access is through a Google **service account**: a robot account that the
restaurant adds to its shared drive as Content manager. Nobody signs in, so
sending keeps working when staff accounts change. The key JSON is uploaded once
in Settings and kept server-side; only its client_email is ever shown.

Files go into a *shared drive* (Rasheed Restaurant's), never a My Drive:
service accounts have no storage of their own, and every call carries
``supportsAllDrives`` so Google lets it reach the shared drive.

Every exception raised to callers is a :class:`DriveError` whose message is
written for restaurant staff and never contains a key or token.
"""

import json
import logging
import time
import uuid

import httpx
from google.auth import crypt, jwt

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

MSG_OFFLINE = "Couldn't reach Google Drive. Check this PC's internet connection and try again."
MSG_BAD_KEY = ("Google rejected the service-account key (it may have been deleted or disabled). "
               "Ask an admin to upload a new key in Settings.")
MSG_NO_ACCESS = ("Google Drive refused the upload. Ask an admin to check the service account "
                 "is a Content manager on the shared drive.")

_REQUIRED_KEY_FIELDS = ("type", "client_email", "private_key", "private_key_id")


class DriveError(Exception):
    """Message is safe to show staff; never contains a key or token."""


def make_client() -> httpx.Client:
    """Factory so tests can swap in an httpx.MockTransport."""
    return httpx.Client(timeout=TIMEOUT)


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    log.warning("drive: %s failed with HTTP %s", what, r.status_code)
    if r.status_code == 401:
        raise DriveError(MSG_BAD_KEY)
    if r.status_code == 403:
        raise DriveError(MSG_NO_ACCESS)
    if r.status_code == 404:
        raise DriveError("The shared-drive folder could not be found. "
                         "Ask an admin to check the folder in Settings.")
    raise DriveError(f"Google Drive returned an error ({r.status_code}). Try again in a few minutes.")


# ── Service-account key ──────────────────────────────────────────────────────

def parse_key(text: str) -> dict:
    """Validate an uploaded key file. Raises ValueError with an admin-facing message."""
    try:
        info = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError("That file isn't valid JSON. Upload the key file Google downloaded.") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("That isn't a service-account key. In Google Cloud, open the service "
                         "account, then Keys > Add key > JSON.")
    if any(not info.get(field) for field in _REQUIRED_KEY_FIELDS):
        raise ValueError("The key file is missing fields. Download a fresh JSON key.")
    try:
        crypt.RSASigner.from_service_account_info(info)
    except Exception:
        raise ValueError("The key file's private key can't be read. Download a fresh JSON key.") from None
    return info


def access_token(client: httpx.Client, info: dict) -> str:
    """Sign a JWT with the service-account key and trade it for an access token."""
    now = int(time.time())
    token_uri = info.get("token_uri") or TOKEN_URL
    signer = crypt.RSASigner.from_service_account_info(info)
    assertion = jwt.encode(signer, {
        "iss": info["client_email"], "scope": SCOPE, "aud": token_uri,
        "iat": now, "exp": now + 3600,
    })
    if isinstance(assertion, bytes):
        assertion = assertion.decode()
    try:
        r = client.post(token_uri, data={"grant_type": JWT_GRANT, "assertion": assertion})
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    if r.status_code >= 400:
        try:
            code = r.json().get("error")
        except ValueError:
            code = None
        log.warning("drive: service-account token request failed: %s", code or r.status_code)
        if code in ("invalid_grant", "invalid_client", "unauthorized_client") or r.status_code in (400, 401):
            raise DriveError(MSG_BAD_KEY)
        raise DriveError("Google sign-in service returned an error. Try again in a few minutes.")
    return r.json()["access_token"]


# ── Files ────────────────────────────────────────────────────────────────────

def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# Every Drive call carries these so it can reach a shared drive, not only
# the signed-in account's own My Drive.
ALL_DRIVES = {"supportsAllDrives": "true"}


def check_target(client: httpx.Client, token: str, folder_id: str) -> str:
    """Confirm the shared drive (or a folder inside it) is reachable; return its name."""
    auth = {"Authorization": f"Bearer {token}"}
    try:
        r = client.get(f"{FILES}/{folder_id}", headers=auth,
                       params={**ALL_DRIVES, "fields": "id,name,mimeType,trashed,driveId"})
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    if r.status_code == 404:
        log.warning("drive: target folder not visible (404)")
        raise DriveError("The shared drive can't be found. Ask an admin to add the service "
                         "account to the shared drive as Content manager.")
    _check(r, "target lookup")
    meta = r.json()
    if meta.get("trashed") or meta.get("mimeType") != FOLDER_MIME:
        raise DriveError("The configured shared-drive folder was deleted or isn't a folder. "
                         "Ask an admin to check the folder ID.")
    if not meta.get("driveId"):
        raise DriveError("That folder is in someone's My Drive, not a shared drive. "
                         "Ask an admin to pick a folder in the shared drive.")
    return meta.get("name") or folder_id


def upload_csv(client: httpx.Client, token: str, folder_id: str,
               filename: str, content: bytes) -> dict:
    """Create `filename` in `folder_id`, or overwrite it if it exists.

    Returns {"id": <drive file id>, "replaced": bool}.
    """
    auth = {"Authorization": f"Bearer {token}"}
    q = f"name = '{_q(filename)}' and '{_q(folder_id)}' in parents and trashed = false"
    try:
        r = client.get(FILES, headers=auth, params={
            **ALL_DRIVES, "includeItemsFromAllDrives": "true", "corpora": "allDrives",
            "q": q, "fields": "files(id)", "pageSize": 1})
        _check(r, "file lookup")
        found = r.json().get("files", [])
        if found:  # same-day re-send: new revision of the same file, no duplicate
            r = client.patch(f"{UPLOAD}/{found[0]['id']}",
                             params={**ALL_DRIVES, "uploadType": "media", "fields": "id"},
                             headers={**auth, "Content-Type": "text/csv"}, content=content)
        else:
            boundary = uuid.uuid4().hex
            meta = json.dumps({"name": filename, "parents": [folder_id], "mimeType": "text/csv"})
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: text/csv\r\n\r\n"
            ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
            r = client.post(UPLOAD, params={**ALL_DRIVES, "uploadType": "multipart", "fields": "id"},
                            headers={**auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                            content=body)
        _check(r, "upload")
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    return {"id": r.json()["id"], "replaced": bool(found)}
