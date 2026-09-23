import csv
import io
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.deps import require_admin, require_auth

from app import config
from app.database import get_db
from app.models import AttendanceLog, Device, Employee, User
from app.schemas import AttendanceOut

router = APIRouter(prefix="/attendance", tags=["attendance"], dependencies=[Depends(require_auth)])


def _at(day: date, value: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(value))


def _segments(employee: Employee, day: date):
    pairs = [(employee.shift1_start, employee.shift1_end)]
    if employee.shift2_start and employee.shift2_end:
        pairs.append((employee.shift2_start, employee.shift2_end))
    result = []
    for index, (start_text, end_text) in enumerate(pairs, 1):
        start = _at(day, start_text)
        end = _at(day, end_text)
        if end <= start:
            end += timedelta(days=1)
        result.append((index, start, end))
    return result


def _credited_time(punch_in, punch_out, scheduled_start, scheduled_end):
    """Return hours/status, counting only time inside the scheduled window."""
    if punch_in is None:
        return 0.0, "Missing punch in"
    credited_start = max(punch_in, scheduled_start)
    credited_end = min(punch_out or scheduled_end, scheduled_end)
    hours = max(0.0, (credited_end - credited_start).total_seconds() / 3600)
    if not punch_out:
        status = "Estimated to shift end - missing punch out"
    elif punch_out > scheduled_end:
        status = "Late checkout capped at shift end"
    elif punch_in < scheduled_start:
        status = "Early punch capped at shift start"
    else:
        status = "Complete"
    return hours, status


def _classify_punches(values, scheduled_start, scheduled_end, has_shift):
    """Classify one or more punches as in/out without trusting device status."""
    if len(values) >= 2:
        return min(values), max(values)
    value = values[0]
    if not has_shift:
        return value, None
    distance_to_start = abs((value - scheduled_start).total_seconds())
    distance_to_end = abs((value - scheduled_end).total_seconds())
    if distance_to_end < distance_to_start:
        return None, value
    return value, None


@router.get("/export")
def export_attendance(
    from_date: date = Query(...),
    to_date: date = Query(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    user_id: Optional[list[str]] = Query(None),
):
    """Export shift-aware daily attendance and employee totals as CSV.

    Raw punches are never rewritten. Employees without an assigned schedule
    use the shop's default rule: one operational day runs 03:00–02:59, the
    first punch is in, the last is out, and credited time is capped at 13h.
    """
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (to_date - from_date).days > 366:
        raise HTTPException(status_code=400, detail="Export period cannot exceed 366 days")

    summary, daily = _pdf_rows(db, from_date, to_date, user_id)
    days, matrix = _attendance_matrix(summary, daily, from_date, to_date)
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["Employee ID", "Employee Name", "Shift"]
    for day in days:
        label = day.strftime("%d %b %Y")
        header.extend([
            f"{label} Punch In", f"{label} Punch Out",
            f"{label} Hours", f"{label} Status",
        ])
    header.extend(["Total Hours", "Total Punches"])
    writer.writerow(header)
    for employee in summary:
        row = [employee["user_id"], employee["name"], employee["shift"]]
        for day in days:
            value = matrix.get(employee["user_id"], {}).get(day.isoformat())
            row.extend([
                value["in"] if value else "",
                value["out"] if value else "",
                f"{value['hours']:.2f}" if value else "",
                value["status"] if value else "No punch",
            ])
        row.extend([f"{employee['hours']:.2f}", employee["punches"]])
        writer.writerow(row)

    output.seek(0)
    filename = f"attendance-{from_date.isoformat()}-to-{to_date.isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf_rows(
    db: Session,
    from_date: date,
    to_date: date,
    selected_user_ids: Optional[list[str]] = None,
):
    """Build the same shift-aware facts as CSV in presentation-friendly rows."""
    # FastAPI's Query default is present when this endpoint helper is invoked
    # directly (tests/maintenance scripts), but only real lists are filters.
    if not isinstance(selected_user_ids, (list, tuple, set)):
        selected_user_ids = None
    range_start = datetime.combine(from_date, time.min) - timedelta(hours=4)
    range_end = datetime.combine(to_date + timedelta(days=1), time.min) + timedelta(hours=4)
    query = db.query(AttendanceLog).filter(
        AttendanceLog.timestamp >= range_start,
        AttendanceLog.timestamp < range_end,
    )
    if selected_user_ids:
        query = query.filter(AttendanceLog.user_id.in_(selected_user_ids))
    punches = query.order_by(AttendanceLog.user_id, AttendanceLog.timestamp).all()
    user_ids = sorted({row.user_id for row in punches})
    employees = {
        row.user_id: row
        for row in db.query(Employee).filter(Employee.user_id.in_(user_ids)).all()
    } if user_ids else {}
    by_user = {user_id: [] for user_id in user_ids}
    for row in punches:
        by_user[row.user_id].append(row.timestamp.replace(tzinfo=None))

    daily = []
    totals = {
        user_id: {"hours": 0.0, "punches": 0, "completed": 0}
        for user_id in user_ids
    }
    day = from_date
    while day <= to_date:
        for user_id in user_ids:
            employee = employees.get(user_id)
            has_shift = bool(employee and employee.shift1_start and employee.shift1_end)
            if has_shift:
                segments = _segments(employee, day)
                start, end = segments[0][1], segments[-1][2]
                values = [v for v in by_user[user_id] if start - timedelta(hours=3) <= v <= end + timedelta(hours=3)]
            else:
                start = datetime.combine(day, time(hour=3))
                end = start + timedelta(days=1)
                values = [v for v in by_user[user_id] if start <= v < end]
                segments = [(1, min(values), min(values) + timedelta(hours=13))] if values else []
            if not values:
                continue

            if len(segments) == 1:
                buckets = [values]
            else:
                boundary = segments[0][2] + (segments[1][1] - segments[0][2]) / 2
                buckets = [[v for v in values if v <= boundary], [v for v in values if v > boundary]]

            for (index, scheduled_start, scheduled_end), bucket in zip(segments, buckets):
                if not bucket:
                    continue
                punch_in, punch_out = _classify_punches(
                    bucket, scheduled_start, scheduled_end, has_shift,
                )

                # Credit only time inside the scheduled window. Arriving late
                # reduces credited hours; arriving early or leaving late never
                # adds time outside the shift. With no checkout, use the shift
                # end (or the 13-hour fallback end) and mark it as estimated.
                hours, status = _credited_time(
                    punch_in, punch_out, scheduled_start, scheduled_end,
                )
                name = (employee.name if employee else "") or user_id
                shift = (employee.shift_name if employee else None) or (f"Shift {index}" if has_shift else "Default 13-hour")
                daily.append({
                    "user_id": user_id, "name": name, "date": day.isoformat(),
                    "shift": shift,
                    "in": punch_in.strftime("%H:%M") if punch_in else "-",
                    "in_value": punch_in,
                    "out": (
                        punch_out.strftime("%H:%M")
                        + ("+1" if punch_out.date() > day else "")
                    ) if punch_out else "-",
                    "out_value": punch_out,
                    "hours": hours, "punches": len(bucket), "status": status,
                })
                totals[user_id]["hours"] += hours
                totals[user_id]["punches"] += len(bucket)
                totals[user_id]["completed"] += int(hours > 0)
        day += timedelta(days=1)

    summary = []
    for user_id in user_ids:
        employee = employees.get(user_id)
        values = totals[user_id]
        summary.append({
            "user_id": user_id,
            "name": (employee.name if employee else "") or user_id,
            "shift": (employee.shift_name if employee else None) or (
                "Fixed" if employee and employee.shift1_start else "Default 13-hour"
            ),
            **values,
        })
    return summary, daily


def _attendance_matrix(summary, daily, from_date: date, to_date: date):
    """Aggregate shift segments into one first-punch/hours cell per employee/day."""
    days = []
    current = from_date
    while current <= to_date:
        days.append(current)
        current += timedelta(days=1)

    matrix = {row["user_id"]: {} for row in summary}
    for row in daily:
        value = matrix[row["user_id"]].setdefault(row["date"], {
            "ins": [], "outs": [], "hours": 0.0, "punches": 0, "statuses": [],
        })
        if row["in"] != "-":
            value["ins"].append((row["in_value"], row["in"]))
        if row["out"] != "-":
            value["outs"].append((row["out_value"], row["out"]))
        value["hours"] += row["hours"]
        value["punches"] += row["punches"]
        value["statuses"].append(row["status"])
    for employee_days in matrix.values():
        for value in employee_days.values():
            value["in"] = min(value["ins"], key=lambda item: item[0])[1] if value["ins"] else "-"
            value["out"] = max(value["outs"], key=lambda item: item[0])[1] if value["outs"] else "-"
            value["missing_in"] = any("missing punch in" in status.lower() for status in value["statuses"])
            value["missing_out"] = any("missing punch out" in status.lower() for status in value["statuses"])
            if value["missing_in"] and value["missing_out"]:
                value["status"] = "Missing punch in and punch out"
            elif value["missing_out"]:
                value["status"] = "Estimated - missing punch out"
            elif value["missing_in"]:
                value["status"] = "Missing punch in"
            elif any("capped" in status.lower() for status in value["statuses"]):
                value["status"] = "Capped to shift window"
            else:
                value["status"] = "Complete"
    return days, matrix


@router.get("/export.pdf")
def export_attendance_pdf(
    from_date: date = Query(...),
    to_date: date = Query(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    user_id: Optional[list[str]] = Query(None),
):
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (to_date - from_date).days > 366:
        raise HTTPException(status_code=400, detail="Export period cannot exceed 366 days")

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    summary, daily = _pdf_rows(db, from_date, to_date, user_id)
    days, matrix = _attendance_matrix(summary, daily, from_date, to_date)
    stream = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=colors.HexColor("#172033"), spaceAfter=5 * mm,
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, textColor=colors.HexColor("#172033"), spaceBefore=3 * mm, spaceAfter=3 * mm,
    )
    note_style = ParagraphStyle(
        "Note", parent=styles["BodyText"], fontSize=8.5, leading=11,
        textColor=colors.HexColor("#5B6475"), spaceAfter=4 * mm,
    )
    page_size = landscape(A4)
    doc = SimpleDocTemplate(
        stream, pagesize=page_size, leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=15 * mm,
        title=f"Attendance report {from_date} to {to_date}",
    )

    def page_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D9DEE8"))
        canvas.line(14 * mm, 10 * mm, page_size[0] - 14 * mm, 10 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#70798A"))
        canvas.drawString(14 * mm, 6 * mm, f"FactorPunch Sync | {from_date} to {to_date}")
        canvas.drawRightString(page_size[0] - 14 * mm, 6 * mm, f"Page {document.page}")
        canvas.restoreState()

    header_bg = colors.HexColor("#2463EB")
    grid = colors.HexColor("#D9DEE8")
    alternate = colors.HexColor("#F5F7FB")
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.35, grid),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]

    story = [
        Paragraph("Attendance report", title_style),
        Paragraph(
            f"Period: {from_date.strftime('%d %b %Y')} to {to_date.strftime('%d %b %Y')}. "
            "Unassigned employees use the 03:00 business-day boundary and a maximum of 13 credited hours per day. "
            "Late arrivals reduce credited hours, and time outside the shift window is never credited. "
            "Missing punch-outs are estimated only to the shift end and remain marked with !.",
            note_style,
        ),
        Paragraph("Period overview", section_style),
    ]
    summary_data = [["Employee", "ID", "Shift rule", "Credited shifts", "Punches", "Total hours"]]
    for row in summary:
        summary_data.append([
            row["name"], row["user_id"], row["shift"], row["completed"],
            row["punches"], f"{row['hours']:.2f}",
        ])
    if len(summary_data) == 1:
        summary_data.append(["No attendance records", "", "", "", "", ""])
    summary_table = Table(summary_data, repeatRows=1, colWidths=[52*mm, 30*mm, 45*mm, 32*mm, 25*mm, 30*mm])
    summary_table.setStyle(TableStyle(table_style + [
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, alternate]),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
    ]))
    story.append(summary_table)

    # Two weeks per A4 landscape page keeps one payroll period on each sheet.
    for offset in range(0, len(days), 14):
        block = days[offset:offset + 14]
        story.extend([
            PageBreak(),
            Paragraph(
                f"Daily punch-in and hours — {block[0].strftime('%d %b')} to {block[-1].strftime('%d %b %Y')}",
                section_style,
            ),
            Paragraph(
                "Each day shows IN, OUT, and credited hours. ! marks a missing punch; +1 means next day.",
                note_style,
            ),
        ])
        matrix_data = [["Employee", "ID"] + [day.strftime("%a\n%d %b") for day in block] + ["Period\nhours"]]
        for employee in summary:
            row = [employee["name"], employee["user_id"]]
            for day in block:
                value = matrix.get(employee["user_id"], {}).get(day.isoformat())
                if not value:
                    row.append("—")
                else:
                    missing_in = value["missing_in"]
                    missing_out = value["missing_out"]
                    in_text = "IN -- !" if missing_in else f"IN {value['in']}"
                    out_text = "OUT -- !" if missing_out else f"OUT {value['out']}"
                    row.append(f"{in_text}\n{out_text}\n{value['hours']:.2f} h")
            row.append(f"{employee['hours']:.2f}")
            matrix_data.append(row)
        if len(matrix_data) == 1:
            matrix_data.append(["No attendance records", ""] + ["—"] * len(block) + ["0.00"])
        available = 269 * mm
        fixed = 64 * mm
        day_width = (available - fixed) / max(len(block), 1)
        matrix_table = Table(
            matrix_data, repeatRows=1,
            colWidths=[35 * mm, 15 * mm] + [day_width] * len(block) + [14 * mm],
        )
        matrix_table.setStyle(TableStyle(table_style + [
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, alternate]),
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 5.7),
            ("LEADING", (0, 0), (-1, -1), 6.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(matrix_table)
    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)

    filename = f"attendance-{from_date.isoformat()}-to-{to_date.isoformat()}.pdf"
    return Response(
        content=stream.getvalue(), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_query(db, device_sn, user_id, from_date, to_date):
    q = db.query(AttendanceLog)
    if device_sn:
        q = q.filter(AttendanceLog.device_sn == device_sn)
    if user_id:
        q = q.filter(AttendanceLog.user_id == user_id)
    if from_date:
        q = q.filter(AttendanceLog.timestamp >= from_date)
    if to_date:
        q = q.filter(AttendanceLog.timestamp <= to_date)
    return q


@router.get("")
def list_attendance(
    device_sn: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    from_date: Optional[datetime] = Query(None),
    to_date: Optional[datetime] = Query(None),
    limit: int = Query(50, le=1000),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    q = _build_query(db, device_sn, user_id, from_date, to_date)
    total = q.count()
    rows = q.order_by(AttendanceLog.timestamp.desc()).offset(offset).limit(limit).all()

    # A punch time is the device's own wall-clock with no offset, so it is
    # meaningless without a label. Records stamped at ingest carry their own;
    # rows that predate the column resolve to their device's zone, and then to
    # the configured default. The same order the HRM push uses — the UI must
    # never show a time it cannot say the meaning of, and must never invent
    # one by re-zoning it into the viewer's locale.
    device_zones = dict(db.query(Device.serial_number, Device.timezone).all())

    items = []
    for r in rows:
        item = AttendanceOut.model_validate(r)
        if not item.timezone:
            item.timezone = device_zones.get(r.device_sn) or config.DEFAULT_DEVICE_TIMEZONE
        items.append(item)

    return {"total": total, "items": items}
