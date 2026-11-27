"""Google Drive send: sign-in, create-vs-replace, staff errors, roles, audit."""

import json
import unittest
from datetime import datetime
from unittest.mock import patch

import httpx
from fastapi import FastAPI
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


class FakeGoogle:
    """In-memory stand-in for Google's OAuth and Drive endpoints."""

    def __init__(self):
        self.files = {}          # id -> {"name", "parents", "content", "mime"}
        self.calls = []
        self.token_status = "ok"
        self.device_status = "pending"
        self.upload_status = 200
        self.offline = False
        self._next = 0

    def _id(self):
        self._next += 1
        return f"id{self._next}"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        url = str(req.url)
        self.calls.append((req.method, url))
        if url.startswith(drive.DEVICE_CODE_URL):
            return httpx.Response(200, json={
                "device_code": "DEV", "user_code": "ABCD-EFGH",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800, "interval": 5})
        if url.startswith(drive.TOKEN_URL):
            body = dict(httpx.QueryParams(req.content.decode()))
            if body["grant_type"] == drive.DEVICE_GRANT:
                if self.device_status == "pending":
                    return httpx.Response(428, json={"error": "authorization_pending"})
                if self.device_status == "denied":
                    return httpx.Response(403, json={"error": "access_denied"})
                return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT"})
            if self.token_status == "revoked":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3599})
        if url.startswith(drive.REVOKE_URL):
            return httpx.Response(200)
        assert req.headers.get("authorization") == "Bearer AT"
        if req.method == "GET" and url.startswith(drive.FILES + "/"):
            fid = req.url.path.rsplit("/", 1)[1]
            if fid in self.files:
                return httpx.Response(200, json={"id": fid, "trashed": False})
            return httpx.Response(404, json={})
        if req.method == "POST" and url.startswith(drive.FILES):
            meta = json.loads(req.content)
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "parents": [], "mime": meta["mimeType"]}
            return httpx.Response(200, json={"id": fid})
        if req.method == "GET" and url.startswith(drive.FILES):
            q = req.url.params["q"]
            hits = [{"id": k} for k, v in self.files.items()
                    if f"name = '{v['name']}'" in q and any(f"'{p}' in parents" in q for p in v["parents"])]
            return httpx.Response(200, json={"files": hits[:1]})
        if self.upload_status != 200:
            return httpx.Response(self.upload_status, json={})
        if req.method == "POST" and url.startswith(drive.UPLOAD):
            boundary = req.headers["content-type"].split("boundary=")[1]
            parts = req.content.split(f"--{boundary}".encode())
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1])
            content = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            fid = self._id()
            self.files[fid] = {"name": meta["name"], "parents": meta["parents"],
                               "mime": meta["mimeType"], "content": content}
            return httpx.Response(200, json={"id": fid})
        if req.method == "PATCH" and url.startswith(drive.UPLOAD):
            fid = req.url.path.rsplit("/", 1)[1]
            self.files[fid]["content"] = req.content
            return httpx.Response(200, json={"id": fid})
        raise AssertionError(f"unexpected call {req.method} {url}")


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
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Admin access required")
            return self.user

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_auth] = lambda: self.user
        app.dependency_overrides[require_admin] = admin_only
        self.client = TestClient(app)

        patcher = patch.object(drive, "make_client",
                               lambda: httpx.Client(transport=httpx.MockTransport(self.google)))
        patcher.start()
        self.addCleanup(patcher.stop)
        today = patch.object(drive_router, "operational_today",
                             lambda: datetime(2026, 9, 1).date())
        today.start()
        self.addCleanup(today.stop)

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
    def connect(self):
        r = self.client.put("/drive", json={"client_id": "cid", "client_secret": "csecret"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/drive/connect")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pending"]["user_code"], "ABCD-EFGH")
        self.google.device_status = "approved"
        r = self.client.post("/drive/connect/poll")
        self.assertEqual(r.json()["status"], "connected", r.text)

    def audits(self, action):
        with self.Session() as db:
            return [a.detail for a in db.query(AuditLog).filter_by(action=action).all()]

    # tests
    def test_secrets_are_never_returned(self):
        self.connect()
        body = self.client.get("/drive").text
        self.assertNotIn("csecret", body)
        self.assertNotIn("RT", json.loads(body).values())
        self.assertTrue(json.loads(body)["client_secret_set"])
        for detail in self.audits("drive_config_change"):
            self.assertNotIn("csecret", detail)

    def test_poll_while_pending_then_connected(self):
        self.client.put("/drive", json={"client_id": "cid", "client_secret": "csecret"})
        self.client.post("/drive/connect")
        r = self.client.post("/drive/connect/poll")
        self.assertEqual(r.json()["status"], "pending")
        self.assertFalse(r.json()["connected"])
        self.google.device_status = "approved"
        r = self.client.post("/drive/connect/poll")
        self.assertTrue(r.json()["connected"])
        self.assertEqual(self.audits("drive_connect"), ["Google Drive signed in"])

    def test_declined_sign_in_gives_clear_message(self):
        self.client.put("/drive", json={"client_id": "cid", "client_secret": "csecret"})
        self.client.post("/drive/connect")
        self.google.device_status = "denied"
        r = self.client.post("/drive/connect/poll")
        self.assertEqual(r.status_code, 400)
        self.assertIn("declined", r.json()["detail"])

    def test_first_send_creates_folder_and_file(self):
        self.connect()
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["replaced"])
        self.assertTrue(r.json()["last_send_ok"])
        names = sorted(f["name"] for f in self.google.files.values())
        self.assertEqual(names, ["FactorPunch Attendance", "attendance-2026-09-01.csv"])
        csv_file = next(f for f in self.google.files.values() if f["name"].endswith(".csv"))
        self.assertIn(b"Aisha", csv_file["content"])
        self.assertIn(b"01 Sep 2026 Punch In", csv_file["content"])

    def test_same_day_resend_replaces_instead_of_duplicating(self):
        self.connect()
        self.client.post("/drive/send")
        r = self.client.post("/drive/send")
        self.assertTrue(r.json()["replaced"])
        csvs = [f for f in self.google.files.values() if f["name"].endswith(".csv")]
        self.assertEqual(len(csvs), 1)
        folders = [f for f in self.google.files.values() if f["mime"] == drive.FOLDER_MIME]
        self.assertEqual(len(folders), 1)
        self.assertEqual(len(self.audits("drive_send")), 2)
        self.assertIn("replaced", self.audits("drive_send")[1])

    def test_viewer_can_send_but_not_configure(self):
        self.connect()
        self.user = VIEWER
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["last_send_by"], "cashier")
        self.assertNotIn("client_id", r.json())
        self.assertEqual(self.client.put("/drive", json={"folder_name": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/drive/connect").status_code, 403)
        self.assertEqual(self.client.post("/drive/disconnect").status_code, 403)

    def test_send_before_setup_says_ask_an_admin(self):
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Ask an admin", r.json()["detail"])

    def test_revoked_sign_in_is_reported_and_stored(self):
        self.connect()
        self.google.token_status = "revoked"
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("reconnect", r.json()["detail"])
        status = self.client.get("/drive").json()
        self.assertFalse(status["last_send_ok"])
        self.assertIn("reconnect", status["last_error"])
        self.assertTrue(self.audits("drive_send")[0].startswith("failed:"))

    def test_offline_gives_internet_message(self):
        self.connect()
        self.google.offline = True
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("internet connection", r.json()["detail"])

    def test_deleted_folder_is_recreated(self):
        self.connect()
        self.client.post("/drive/send")
        self.google.files.clear()
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["replaced"])
        self.assertEqual(len(self.google.files), 2)

    def test_forbidden_upload_message(self):
        self.connect()
        self.google.upload_status = 403
        r = self.client.post("/drive/send")
        self.assertEqual(r.status_code, 502)
        self.assertIn("refused", r.json()["detail"])

    def test_future_day_rejected(self):
        self.connect()
        r = self.client.post("/drive/send", params={"day": "2026-09-02"})
        self.assertEqual(r.status_code, 400)

    def test_past_day_uses_that_date_in_filename(self):
        self.connect()
        r = self.client.post("/drive/send", params={"day": "2026-08-31"})
        self.assertEqual(r.json()["last_send_file"], "attendance-2026-08-31.csv")

    def test_changing_client_id_drops_old_sign_in(self):
        self.connect()
        r = self.client.put("/drive", json={"client_id": "other"})
        self.assertFalse(r.json()["connected"])

    def test_disconnect_revokes_and_clears(self):
        self.connect()
        r = self.client.post("/drive/disconnect")
        self.assertFalse(r.json()["connected"])
        self.assertTrue(any(u.startswith(drive.REVOKE_URL) for _, u in self.google.calls))
        with self.Session() as db:
            self.assertIsNone(db.query(DriveIntegration).first().refresh_token)


if __name__ == "__main__":
    unittest.main()
