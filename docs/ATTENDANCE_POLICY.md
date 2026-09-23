# Attendance Calculation Policy

Raw terminal punches are retained unchanged. Reports interpret those records
using the rules below.

## Assigned fixed shift

For a shift from 07:00 to 20:00:

- IN 07:00, OUT 20:00: 13 credited hours.
- IN 08:00, OUT 20:00: 12 credited hours.
- IN 08:00, OUT 21:00: 12 credited hours; late checkout is capped at 20:00.
- IN 06:30, OUT 20:00: 13 credited hours; early arrival is capped at 07:00.
- IN 08:00, missing OUT: 12 estimated hours to shift end, visibly marked.
- Missing IN, OUT 20:00: zero hours because arrival time is unknown.

For one punch on an assigned shift, proximity determines its meaning:

- Closer to scheduled start: IN, missing OUT.
- Closer to scheduled end: OUT, missing IN.

For two or more punches in one shift segment, the earliest is IN and the latest
is OUT. Split shifts are separated using the midpoint of the scheduled gap.

## No assigned shift

Exports are never blocked. The operational day begins at 03:00 and ends before
03:00 the following day. The first punch is IN, the last punch is OUT, and
credited time is capped at 13 hours. A lone punch uses the 13-hour fallback and
is marked as estimated because OUT is missing.

## Export display

- PDF: A4 landscape, fourteen dates per attendance page.
- Every populated cell shows IN, OUT, and credited hours.
- `!` marks a missing punch.
- `+1` after OUT means checkout occurred on the next calendar day.
- CSV provides separate daily Punch In, Punch Out, Hours, and Status columns.
- Operators may export all employees or any selected group.
