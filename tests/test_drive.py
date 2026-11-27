"""Google Drive send: service-account key, shared-drive target, create-vs-replace,
staff-facing errors, roles and audit."""

import base64
import json
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.deps import require_admin, require_auth
from app.models import AttendanceLog, AuditLog, DriveIntegration, Employee, User
from app.routers import drive as drive_router
from app.services import drive

ADMIN = User(id=1, username="boss", role="admin", password_hash="x")
VIEWER = User(id=2, username="cashier", role="viewer", password_hash="x")
SA_EMAIL = "factorpunch@rasheed-test.iam.gserviceaccount.com"
SHARED = "0AbCdEfGhIjKlMnOpQ"     # a shared drive's root ID
MY_DRIVE = "1MyDriveFolderXYZ123"  # a folder in someone's My Drive

_PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
KEY = {
    "type": "service_account", "project_id": "rasheed-test",
    "private_key_id": "abc123", "private_key": _PEM, "client_email": SA_EMAIL,
    "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
}


def _claims(assertion: str) -> dict:
    payload = assertion.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class FakeGoogle:
    """In-memory stand-in for Google's token and Drive endpoints."""

    def __init__(self):
        self.files = {
            SHARED: {"name": "Rasheed Restaurant", "mime": drive.FOLDER_MIME, "driveId": SHARED},
            MY_DRIVE: {"name": "My stuff", "mime": drive.FOLDER_MIME},
        }
        self.calls = []
        self.key_revoked = False
        self.upload_status = 200
        self.offline = False
        self._next = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        url = str(req.url)
        self.calls.append((req.method, url))
        if url.startswith(drive.TOKEN_URL):
            body = dict(httpx.QueryParams(req.content.decode()))
            assert body["grant_type"] == drive.JWT_GRANT
            claims = _claims(body["assertion"])
            assert claims["iss"] == SA_EMAIL and claims["scope"] == drive.SCOPE
            if self.key_revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3599})
        assert req.headers.get("authorization") == "Bearer AT"
        # Every Drive call must opt in to shared drives.
        assert req.url.params.get("supportsAllDrives") == "true", url
        if req.method == "GET" and url.startswith(drive.FILES + "/"):
            fid = req.url.path.rsplit("/", 1)[1]
            meta = self.files.get(fid)
            if not meta:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"id": fid, "name": meta["name"], "mimeType": meta["mime"],
                                             "trashed": False, "driveId": meta.get("driveId")})
        if req.method == "GET" and url.startswith(drive.FILES):
            assert req.url.params.get("includeItemsFromAllDrives") == "true"
            q = req.url.params["q"]
            hits = [{"id": k} for k, v in self.files.items()
                    if f"name = '{v['name']}'" in q and any(f"'{p}' in parents" in q for p in v.get("parents", []))]
            return httpx.Response(200, json={"files": hits[:1]})
        if self.upload_status != 200:
            return httpx.Response(self.upload_status, json={})
        if req.method == "POST" and url.startswith(drive.UPLOAD):
            boundary = req.headers["content-type"].split("boundary=")[1]
            parts = req.content.split(f"--{boundary}".encode())
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1])
            content = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            self._next += 1
            fid = f"f{self._next}"
            self.files[fid] = {"name": meta["name"], "parents": meta["parents"],
                               "mime": meta["mimeType"], "content": content}
            return httpx.Response(200, json={"id": fid})
        if req.method == "PATCH" and url.startswith(drive.UPLOAD):
            fid = req.url.path.rsplit("/", 1)[1]
            self.files[fid]["content"] = req.content
            return httpx.Response(200, json={"id": fid})
        raise AssertionError(f"unexpected call {req.method} {url}")

    def csvs(self):
        return [f for f in self.files.values() if f["name"].endswith(".csv")]


class DriveSendTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.google = FakeGoogle()
        self.user = ADMIN

        app = FastAPI()
        app.include_router(drive_router.router)

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        def admin_only():
            if self.user.role != "admin":
                raise HTTPException(status_code=403, detail="Admin access required")
            return self.user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_auth] = lambda: self.user
        app.dependency_overrides[require_admin] = admin_only
        self.client = TestClient(app)

        for patcher in (
            patch.object(drive, "make_client",
                         lambda: httpx.Client(transport=httpx.MockTransport(self.google))),
            patch.object(drive_router, "operational_today", lambda: datetime(2026, 9, 1).date()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.Session() as db:
            db.add(Employee(user_id="1001", name="Aisha", shift_name="Morning",
                            shift1_start="09:00", shift1_end="17:00"))
            db.add_all([
                AttendanceLog(device_sn="TESTDEVICE001", user_id="1001",
                              timestamp=datetime(2026, 9, 1, 9, 0), status=0, punch=0,
                              source="adms_push"),
                AttendanceLog(device_sn="TESTDEVICE001", user_id="1001",
                              timestamp=datetime(2026, 9, 1, 17, 0), status=0, punch=1,
                              source="adms_push"),
            ])
            db.commit()

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    # helpers
    def set_up_drive(self, folder=f"https://drive.google.com/drive/u/0/folders/{SHARED}"):
        r = self.client.post("/drive/key", json={"key_json": json.dumps(KEY)})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.put("/drive", json={"folder": folder})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def audits(self, action):
        with self.Session() as db:
            return [a.detail for a in db.query(AuditLog).filter_by(action=action).all()]

    # key handling
    def test_private_key_is_never_returned_or_logged(self):
        self.set_up_drive()
        body = self.client.get("/drive").json()
        self.assertTrue(body["key_set"])
        self.assertEqual(body["sa_email"], SA_EMAIL)
        self.assertNotIn("PRIVATE KEY", json.dumps(body))
        for action in ("drive_key_upload", "drive_config_change"):
            for detail in self.audits(action):
                self.assertNotIn("PRIVATE KEY", detail)

    def test_rejects_files_that_are_not_service_account_keys(self):
        for bad, words in (("not json", "valid JSON"),
                           (json.dumps({"type": "authorized_user"}), "service-account key"),
                           (json.dumps({**KEY, "private_key": ""}), "missing fields"),
                           (json.dumps({**KEY, "private_key": "garbage"}), "can't be read")):
            r = self.client.post("/drive/key", json={"key_json": bad})
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn(words, r.json()["detail"])
        self.assertFalse(self.client.get("/drive").json()["key_set"])

    def test_folder_accepts_link_or_id_and_rejects_junk(self):
        self.assertEqual(drive_router.parse_folder(f"https://drive.google.com/drive/folders/{SHARED}?usp=x"), SHARED)
        self.assertEqual(drive_router.parse_folder(f"  {SHARED} "), SHARED)
        self.assertIsNone(drive_router.parse_folder("https://example.com/hello world"))
        r = self.client.put("/drive", json={"folder": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_remove_key_turns_sending_off(self):
        self.set_up_drive()
        r = self.client.delete("/drive/key")
        self.assertFalse(r.json()["ready"])
        with self.Session() as db:
            self.assertIsNone(db.query(DriveIntegration).first().sa_key)
        self.assertEqual(self.client.post("/drive/send").status_code, 400)

    # test button
    def test_test_button_reports_shared_drive_name(self):
        self.set_up_drive()
        r = self.client.post("/drive/test")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["folder_title"], "Rasheed Restaurant")
        self.assertEqual(self.google.csvs(), [])

    def test_my_drive_folder_is_refused(self):
        self.set_up_drive(folder=MY_DRIVE)
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("not a shared drive", r.json()["detail"])
        self.assertEqual(self.google.csvs(), [])

    def test_service_account_not_a_member_gets_clear_message(self):
        self.set_up_drive(folder="0ZzNotSharedWithRobot")
        r = self.client.post("/drive/test")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Content manager", r.json()["detail"])

    # sending
    def test_first_send_creates_file_in_shared_drive(self):
        self.set_up_drive()
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["replaced"])
        self.assertTrue(r.json()["last_send_ok"])
        [csv_file] = self.google.csvs()
        self.assertEqual(csv_file["name"], "attendance-2026-09-01.csv")
        self.assertEqual(csv_file["parents"], [SHARED])
        self.assertIn(b"Aisha", csv_file["content"])
        self.assertIn(b"01 Sep 2026 Punch In", csv_file["content"])
        self.assertIn("Rasheed Restaurant", self.audits("drive_send")[0])

    def test_same_day_resend_replaces_instead_of_duplicating(self):
        self.set_up_drive()
        self.client.post("/drive/send")
        r = self.client.post("/drive/send")
        self.assertTrue(r.json()["replaced"])
        self.assertEqual(len(self.google.csvs()), 1)
        self.assertEqual(len(self.audits("drive_send")), 2)
        self.assertIn("replaced", self.audits("drive_send")[1])

    def test_viewer_can_send_but_not_configure(self):
        self.set_up_drive()
        self.user = VIEWER
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["last_send_by"], "cashier")
        self.assertNotIn("sa_email", r.json())
        self.assertEqual(self.client.put("/drive", json={"folder": SHARED}).status_code, 403)
        self.assertEqual(self.client.post("/drive/key", json={"key_json": "{}"}).status_code, 403)
        self.assertEqual(self.client.delete("/drive/key").status_code, 403)
        self.assertEqual(self.client.post("/drive/test").status_code, 403)

    def test_send_before_setup_says_ask_an_admin(self):
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Ask an admin", r.json()["detail"])

    def test_revoked_key_is_reported_and_stored(self):
        self.set_up_drive()
        self.google.key_revoked = True
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("new key", r.json()["detail"])
        status = self.client.get("/drive").json()
        self.assertFalse(status["last_send_ok"])
        self.assertIn("new key", status["last_error"])
        self.assertTrue(self.audits("drive_send")[0].startswith("failed:"))

    def test_offline_gives_internet_message(self):
        self.set_up_drive()
        self.google.offline = True
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("internet connection", r.json()["detail"])

    def test_forbidden_upload_message(self):
        self.set_up_drive()
        self.google.upload_status = 403
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Content manager", r.json()["detail"])

    def test_future_day_rejected(self):
        self.set_up_drive()
        r = self.client.post("/drive/send", params={"day": "2026-09-02"})
        self.assertEqual(r.status_code, 400)

    def test_past_day_uses_that_date_in_filename(self):
        self.set_up_drive()
        r = self.client.post("/drive/send", params={"day": "2026-08-31"})
        self.assertEqual(r.json()["last_send_file"], "attendance-2026-08-31.csv")


if __name__ == "__main__":
    unittest.main()
