#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIFY_TMP="$(mktemp -d)"
trap 'rm -rf "$VERIFY_TMP"' EXIT

cd "$PROJECT_ROOT"
python3 -m unittest discover -v -s tests -p 'test_*.py'

DB="$VERIFY_TMP/clinic.db"
OUTBOX="$VERIFY_TMP/outbox"
REPORT="$VERIFY_TMP/title-vi-2026-07.csv"

python3 clinic_scheduler.py --db "$DB" init --clinic-name "Valley Rural Clinic" >/dev/null
python3 clinic_scheduler.py --db "$DB" add-interpreter \
  --name "Interpreter One" \
  --contact "interpreter-1@clinic.test" \
  --language Spanish \
  --modality video >/dev/null
python3 clinic_scheduler.py --db "$DB" add-availability \
  --interpreter 1 --weekday Wednesday --start 08:00 --end 17:00 >/dev/null
python3 clinic_scheduler.py --db "$DB" add-appointment \
  --patient-ref PATIENT-001 \
  --patient-contact patient-001@clinic.test \
  --language Spanish \
  --modality video \
  --start 2026-07-08T09:00 \
  --end 2026-07-08T10:00 >/dev/null
python3 clinic_scheduler.py --db "$DB" add-appointment \
  --patient-ref PATIENT-002 \
  --patient-contact patient-002@clinic.test \
  --language Mam \
  --modality in-person \
  --start 2026-07-08T10:00 \
  --end 2026-07-08T11:00 >/dev/null
python3 clinic_scheduler.py --db "$DB" schedule >"$VERIFY_TMP/schedule.json"
python3 clinic_scheduler.py --db "$DB" dispatch-confirmations --outbox "$OUTBOX" \
  >"$VERIFY_TMP/dispatch.json"
python3 clinic_scheduler.py --db "$DB" record-no-show \
  --appointment 1 --party patient --note "Clinic follow-up required" >/dev/null
python3 clinic_scheduler.py --db "$DB" monthly-report \
  --month 2026-07 --output "$REPORT" >/dev/null

python3 - "$DB" "$OUTBOX" "$REPORT" "$VERIFY_TMP/schedule.json" <<'PY'
import csv
import json
import sqlite3
import stat
import sys
from pathlib import Path

db_path, outbox_text, report_text, schedule_text = sys.argv[1:]
outbox = Path(outbox_text)
report_path = Path(report_text)
schedule_result = json.loads(Path(schedule_text).read_text(encoding="utf-8"))

assert [row["appointment_id"] for row in schedule_result["scheduled"]] == [1]
assert [row["appointment_id"] for row in schedule_result["unmatched"]] == [2]
assert schedule_result["unmatched"][0]["failed_language"] == 1

db = sqlite3.connect(db_path)
appointments = db.execute(
    "SELECT id, status, assigned_interpreter_id FROM appointments ORDER BY id"
).fetchall()
assert appointments == [(1, "patient_no_show", 1), (2, "requested", None)]
assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 2
assert db.execute(
    "SELECT COUNT(*) FROM confirmations WHERE dispatched_at IS NOT NULL"
).fetchone()[0] == 2
assert db.execute(
    "SELECT party FROM no_show_events WHERE appointment_id = 1"
).fetchone()[0] == "patient"
db.close()

messages = sorted(outbox.glob("confirmation-*.txt"))
assert len(messages) == 2
assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in messages)
message_text = "\n".join(path.read_text(encoding="utf-8") for path in messages)
assert "Interpreter confirmed" in message_text
assert "Interpretation assignment" in message_text

with report_path.open(newline="", encoding="utf-8") as handle:
    report_rows = list(csv.DictReader(handle))
assert [row["language"] for row in report_rows] == ["ALL", "mam", "spanish"]
overall = report_rows[0]
assert overall["clinic_name"] == "Valley Rural Clinic"
assert overall["requested_visits"] == "2"
assert overall["assigned_visits"] == "1"
assert overall["patient_no_shows"] == "1"
assert overall["unmatched_visits"] == "1"
assert overall["access_rate_percent"] == "50.0"
print("WORKFLOW PASS: assignment, unmatched diagnosis, confirmations, no-show, and report")
PY

python3 scripts/audit_source.py

test -f README.md
grep -q '^## Status$' README.md
grep -q '^VERIFY PASS: 5 unit tests and full CLI workflow passed$' README.md
if grep -q 'TODO' README.md; then
  echo "README CHECK FAIL: TODO remains" >&2
  exit 1
fi
echo "README PASS: status and observed success line are present"
echo "VERIFY PASS: 5 unit tests and full CLI workflow passed"
