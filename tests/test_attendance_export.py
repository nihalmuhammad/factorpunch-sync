import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.deps import require_admin, require_auth
from app.models import AttendanceLog, Employee, User
from app.routers import attendance
from app.routers.attendance import _classify_punches, _credited_time, router


class AttendanceCreditTests(unittest.TestCase):
    def test_export_routes_register_employee_ids_as_query_parameters(self):
        for path in ("/attendance/export", "/attendance/export.pdf"):
            route = next(item for item in router.routes if item.path == path)
            query_names = {param.name for param in route.dependant.query_params}
            self.assertIn("user_id", query_names)

    def test_checkout_without_checkin_receives_zero_hours(self):
        day = "2026-09-01"
        hours, status = _credited_time(
            None,
            datetime.fromisoformat(f"{day}T22:43:00"),
            datetime.fromisoformat(f"{day}T09:00:00"),
            datetime.fromisoformat(f"{day}T22:00:00"),
        )
        self.assertEqual(hours, 0.0)
        self.assertEqual(status, "Missing punch in")

    def test_single_punch_near_shift_end_is_checkout(self):
        day = "2026-09-01"
        punch_in, punch_out = _classify_punches(
            [datetime.fromisoformat(f"{day}T22:43:00")],
            datetime.fromisoformat(f"{day}T09:00:00"),
            datetime.fromisoformat(f"{day}T22:00:00"),
            True,
        )
        self.assertIsNone(punch_in)
        self.assertEqual(punch_out, datetime.fromisoformat(f"{day}T22:43:00"))

    def test_single_punch_near_shift_start_is_checkin(self):
        day = "2026-09-01"
        punch_in, punch_out = _classify_punches(
            [datetime.fromisoformat(f"{day}T09:55:00")],
            datetime.fromisoformat(f"{day}T09:00:00"),
            datetime.fromisoformat(f"{day}T22:00:00"),
            True,
        )
        self.assertEqual(punch_in, datetime.fromisoformat(f"{day}T09:55:00"))
        self.assertIsNone(punch_out)

    def test_late_arrival_and_late_checkout_are_bounded_by_shift(self):
        day = "2026-09-01"
        hours, status = _credited_time(
            datetime.fromisoformat(f"{day}T08:00:00"),
            datetime.fromisoformat(f"{day}T21:00:00"),
            datetime.fromisoformat(f"{day}T07:00:00"),
            datetime.fromisoformat(f"{day}T20:00:00"),
        )
        self.assertEqual(hours, 12.0)
        self.assertEqual(status, "Late checkout capped at shift end")

    def test_missing_checkout_is_estimated_only_to_shift_end(self):
        day = "2026-09-01"
        hours, status = _credited_time(
            datetime.fromisoformat(f"{day}T08:00:00"),
            None,
            datetime.fromisoformat(f"{day}T07:00:00"),
            datetime.fromisoformat(f"{day}T20:00:00"),
        )
        self.assertEqual(hours, 12.0)
        self.assertIn("missing punch out", status)

    def test_early_arrival_does_not_add_time_before_shift(self):
        day = "2026-09-01"
        hours, status = _credited_time(
            datetime.fromisoformat(f"{day}T06:30:00"),
            datetime.fromisoformat(f"{day}T20:00:00"),
            datetime.fromisoformat(f"{day}T07:00:00"),
            datetime.fromisoformat(f"{day}T20:00:00"),
        )
        self.assertEqual(hours, 13.0)
        self.assertEqual(status, "Early punch capped at shift start")


class AttendanceExportHttpTests(unittest.TestCase):
    """Exercise repeated employee filters through FastAPI's real query layer."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        app = FastAPI()
        app.include_router(attendance.router)

        def override_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        admin = User(id=1, username="tester", role="admin", password_hash="x")
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_auth] = lambda: admin
        app.dependency_overrides[require_admin] = lambda: admin
        self.client = TestClient(app)

        db = self.Session()
        try:
            db.add_all([
                Employee(user_id="1001", name="Aisha", shift_name="Morning",
                         shift1_start="09:00", shift1_end="17:00"),
                Employee(user_id="1002", name="Omar", shift_name="Morning",
                         shift1_start="09:00", shift1_end="17:00"),
            ])
            for user_id in ("1001", "1002"):
                db.add_all([
                    AttendanceLog(device_sn="TESTDEVICE001", user_id=user_id,
                                  timestamp=datetime(2026, 9, 1, 9, 0),
                                  status=0, punch=0, source="adms_push"),
                    AttendanceLog(device_sn="TESTDEVICE001", user_id=user_id,
                                  timestamp=datetime(2026, 9, 1, 17, 0),
                                  status=0, punch=1, source="adms_push"),
                ])
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_csv_employee_filter_is_applied_by_http_endpoint(self):
        response = self.client.get(
            "/attendance/export",
            params=[
                ("from_date", "2026-09-01"),
                ("to_date", "2026-09-01"),
                ("user_id", "1001"),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Aisha", response.text)
        self.assertNotIn("Omar", response.text)

    def test_pdf_receives_multiple_employee_filters_from_query(self):
        original = attendance._pdf_rows
        with patch.object(attendance, "_pdf_rows", wraps=original) as rows:
            response = self.client.get(
                "/attendance/export.pdf",
                params=[
                    ("from_date", "2026-09-01"),
                    ("to_date", "2026-09-01"),
                    ("user_id", "1001"),
                    ("user_id", "1002"),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(rows.call_args.args[3], ["1001", "1002"])


if __name__ == "__main__":
    unittest.main()
