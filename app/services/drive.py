"""Google Drive upload for the daily attendance CSV.

Sign-in uses Google's OAuth *device flow*: an admin opens google.com/device on
any phone or PC, types the short code the Settings page shows, and approves.
That works on a LAN-only POS PC with no public redirect URL, and needs nothing
beyond httpx.

The scope is ``drive.file``: the app can only see files and folders it created
itself, never the rest of the account's Drive. That is why the app creates its
own target folder instead of asking for an existing folder ID.

Every exception raised to callers is a :class:`DriveError` whose message is
written for restaurant staff and never contains a token or client secret.
"""

import base64
import json
import logging
import uuid

import httpx

log = logging.getLogger(__name__)

# openid + email let us read which Google account approved the sign-in, so
# it can be checked against DRIVE_ALLOWED_ACCOUNT before it is kept.
SCOPE = "openid email https://www.googleapis.com/auth/drive.file"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

MSG_OFFLINE = "Couldn't reach Google Drive. Check this PC's internet connection and try again."
MSG_RECONNECT = ("Google sign-in has expired or was removed. "
                 "Ask an admin to reconnect Google Drive in Settings.")
MSG_NO_ACCESS = ("Google Drive refused the upload. "
                 "Ask an admin to reconnect Google Drive in Settings.")


class DriveError(Exception):
    """Message is safe to show staff; never contains a token."""


def make_client() -> httpx.Client:
    """Factory so tests can swap in an httpx.MockTransport."""
    return httpx.Client(timeout=TIMEOUT)


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code < 400:
        return
    log.warning("drive: %s failed with HTTP %s", what, r.status_code)
    if r.status_code == 401:
        raise DriveError(MSG_RECONNECT)
    if r.status_code == 403:
        raise DriveError(MSG_NO_ACCESS)
    if r.status_code == 404:
        raise DriveError("The Drive folder could not be found. Try sending again.")
    raise DriveError(f"Google Drive returned an error ({r.status_code}). Try again in a few minutes.")


# ── Sign-in (device flow) ────────────────────────────────────────────────────

def start_device_flow(client: httpx.Client, client_id: str) -> dict:
    """Ask Google for a user code. Returns Google's JSON:
    device_code, user_code, verification_url, expires_in, interval."""
    try:
        r = client.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": SCOPE})
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    if r.status_code >= 400:
        log.warning("drive: device code request failed with HTTP %s", r.status_code)
        raise DriveError("Google rejected the OAuth client ID. Check it was created as type "
                         "\"TVs and Limited Input devices\" and has the Drive API enabled.")
    return r.json()


def poll_device_flow(client: httpx.Client, client_id: str, client_secret: str,
                     device_code: str) -> dict | None:
    """One poll. Returns the token JSON once approved, None while still pending."""
    try:
        r = client.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret,
            "device_code": device_code, "grant_type": DEVICE_GRANT,
        })
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    if r.status_code < 400:
        return r.json()
    error = _error_code(r)
    if error in ("authorization_pending", "slow_down"):
        return None
    if error == "access_denied":
        raise DriveError("Sign-in was declined on the Google page. Start again to retry.")
    if error == "expired_token":
        raise DriveError("The sign-in code expired. Start again to get a new code.")
    if error == "invalid_client":
        raise DriveError("Google rejected the OAuth client ID or secret. Check them in Settings.")
    log.warning("drive: device token poll failed: %s", error or r.status_code)
    raise DriveError("Google sign-in failed. Start again to retry.")


def access_token(client: httpx.Client, client_id: str, client_secret: str,
                 refresh_token: str) -> str:
    """Trade the stored refresh token for a short-lived access token."""
    try:
        r = client.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    if r.status_code >= 400:
        log.warning("drive: token refresh failed: %s", _error_code(r) or r.status_code)
        if _error_code(r) in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise DriveError(MSG_RECONNECT)
        raise DriveError("Google sign-in service returned an error. Try again in a few minutes.")
    return r.json()["access_token"]


def signed_in_email(tokens: dict) -> str | None:
    """The verified address from the id_token Google returned with the grant.

    The token came straight from Google's token endpoint over TLS in reply to
    our own request, so (per OpenID Connect) its signature need not be
    re-checked here. Returns None if Google did not vouch for the address.
    """
    raw = tokens.get("id_token") or ""
    try:
        payload = raw.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    if claims.get("email_verified") not in (True, "true"):
        return None
    return (claims.get("email") or "").strip().lower() or None


def revoke(client: httpx.Client, refresh_token: str) -> None:
    """Best-effort: tell Google to forget the grant. Never raises."""
    try:
        client.post(REVOKE_URL, data={"token": refresh_token})
    except Exception:
        log.warning("drive: token revoke failed", exc_info=True)


def _error_code(r: httpx.Response) -> str | None:
    try:
        return r.json().get("error")
    except ValueError:
        return None


# ── Files ────────────────────────────────────────────────────────────────────

def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def ensure_folder(client: httpx.Client, token: str, folder_id: str | None,
                  folder_name: str) -> str:
    """Return a usable folder ID, creating the folder if it is missing or trashed."""
    auth = {"Authorization": f"Bearer {token}"}
    try:
        if folder_id:
            r = client.get(f"{FILES}/{folder_id}", headers=auth, params={"fields": "id,trashed"})
            if r.status_code == 200 and not r.json().get("trashed"):
                return folder_id
            if r.status_code not in (200, 404):
                _check(r, "folder lookup")
        r = client.post(FILES, headers=auth, params={"fields": "id"},
                        json={"name": folder_name, "mimeType": FOLDER_MIME})
        _check(r, "folder create")
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    return r.json()["id"]


def upload_csv(client: httpx.Client, token: str, folder_id: str,
               filename: str, content: bytes) -> dict:
    """Create `filename` in `folder_id`, or overwrite it if it exists.

    Returns {"id": <drive file id>, "replaced": bool}.
    """
    auth = {"Authorization": f"Bearer {token}"}
    q = f"name = '{_q(filename)}' and '{_q(folder_id)}' in parents and trashed = false"
    try:
        r = client.get(FILES, headers=auth, params={"q": q, "fields": "files(id)", "pageSize": 1})
        _check(r, "file lookup")
        found = r.json().get("files", [])
        if found:  # same-day re-send: new revision of the same file, no duplicate
            r = client.patch(f"{UPLOAD}/{found[0]['id']}",
                             params={"uploadType": "media", "fields": "id"},
                             headers={**auth, "Content-Type": "text/csv"}, content=content)
        else:
            boundary = uuid.uuid4().hex
            meta = json.dumps({"name": filename, "parents": [folder_id], "mimeType": "text/csv"})
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: text/csv\r\n\r\n"
            ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
            r = client.post(UPLOAD, params={"uploadType": "multipart", "fields": "id"},
                            headers={**auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                            content=body)
        _check(r, "upload")
    except httpx.TransportError:
        raise DriveError(MSG_OFFLINE) from None
    return {"id": r.json()["id"], "replaced": bool(found)}
