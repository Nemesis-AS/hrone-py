import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=2000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def ensure_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uniq_emp_code"
    )
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainTarget = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]

app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


def _hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return _hhmm(emp["shift_end"]) <= _hhmm(emp["shift_start"])


def truncate_to_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < _hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_window(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    d = date.fromisoformat(date_str)
    start = datetime.combine(d, _hhmm(emp["shift_start"]), IST)
    end = datetime.combine(d, _hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start_at: datetime) -> int:
    seconds = int((punch_in - shift_start_at).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end_at: datetime) -> int:
    minutes = int((punch_out - shift_end_at).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derive(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    day: str,
    emp: dict,
) -> dict:
    derived = {
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if status not in PRESENCE or punch_in is None:
        return derived
    shift_start_at, shift_end_at = shift_window(day, emp)
    derived["late_minutes"] = compute_late_minutes(punch_in, shift_start_at)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = hours
        derived["overtime_minutes"] = compute_overtime(punch_out, shift_end_at)
        derived["half_day"] = hours < 4.5
    return derived


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc_ms() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def validation_error(loc: list, msg: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg}])


def month_bounds(month: str) -> tuple[str, str]:
    first = date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


def serialize_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = dt_to_ms(doc["created_at"])
    return out


def _ms_or_none(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def serialize_history(h: dict) -> dict:
    changes = {}
    for field, ch in (h.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            ch = {"from": _ms_or_none(ch.get("from")), "to": _ms_or_none(ch.get("to"))}
        changes[field] = ch
    return {
        "at": dt_to_ms(h["at"]),
        "by": h["by"],
        "reason": h["reason"],
        "changes": changes,
    }


def record_state(doc: dict) -> dict:
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def serialize_record(doc: dict) -> dict:
    state = record_state(doc)
    state["punch_in"] = _ms_or_none(state["punch_in"])
    state["punch_out"] = _ms_or_none(state["punch_out"])
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **state,
        "history": [serialize_history(h) for h in doc.get("history") or []],
    }


def _iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


EpochMillis = Annotated[
    int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)
]
DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_iso_date)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_must_have_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def punch_time(punched_at: Optional[int]) -> datetime:
    if punched_at is None:
        return truncate_to_second(datetime.now(timezone.utc))
    return truncate_to_second(ms_to_dt(punched_at))


IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
IS_PRESENT = {"$in": ["$status", PRESENCE]}
PRESENT_VALUE = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}
WEEKDAY_PRESENT = {"$cond": [IS_WEEKDAY, PRESENT_VALUE, 0]}
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def status_is(status: str) -> dict:
    return {"$eq": ["$status", status]}


def rounded_ratio(numerator, denominator, scale: int) -> dict:
    return {
        "$floor": {
            "$divide": [
                {"$add": [{"$multiply": [numerator, 2 * scale]}, denominator]},
                {"$multiply": [denominator, 2]},
            ]
        }
    }


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(["query", "date_from"], "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": WEEKDAY_PRESENT},
                "leave_days": count_if(status_is("LEAVE")),
                "late_count": count_if(IS_LATE),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": WEEKDAY_PRESENT},
                            "hours_cents": {
                                "$sum": {
                                    "$cond": [
                                        HAS_HOURS,
                                        {"$round": [{"$multiply": ["$work_hours", 100]}, 0]},
                                        0,
                                    ]
                                }
                            },
                            "hours_count": count_if(HAS_HOURS),
                            "late_count": count_if(IS_LATE),
                            "total_late_minutes": {"$sum": {"$cond": [IS_LATE, "$late_minutes", 0]}},
                            "leave_count": count_if(status_is("LEAVE")),
                            "on_duty_count": count_if(status_is("ON_DUTY")),
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$set": {"stats": {"$first": "$stats"}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$stats.present_days"},
                "hours_cents": {"$sum": "$stats.hours_cents"},
                "hours_count": {"$sum": "$stats.hours_count"},
                "late_count": {"$sum": "$stats.late_count"},
                "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                "leave_count": {"$sum": "$stats.leave_count"},
                "on_duty_count": {"$sum": "$stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": [rounded_ratio("$hours_cents", "$hours_count", 1), 100]},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ]
    return pipeline


def check_trend_range(start: date, end: date):
    if end < start:
        raise validation_error(["query", "to"], "to must be on or after from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise validation_error(["query", "to"], f"range cannot exceed {MAX_TREND_DAYS} days")


def trend_pipeline(department: str, start: date, end: date) -> list:
    first, last = start.isoformat(), end.isoformat()
    range_start = datetime.combine(start, time(), timezone.utc)
    range_end = datetime.combine(end + timedelta(days=1), time(), timezone.utc)
    has_rate = {"$ne": ["$rate_units", None]}
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": last}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "present": PRESENT_VALUE,
                            "late": {"$cond": [IS_LATE, 1, 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [{"date": {"$max": ["$joined_on", first]}, "joined": {"$literal": 1}}],
                        "$logs",
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {"$replaceWith": "$events"},
        {"$unionWith": {"pipeline": [{"$documents": [{"date": first}]}]}},
        {
            "$group": {
                "_id": "$date",
                "joined": {"$sum": "$joined"},
                "present": {"$sum": "$present"},
                "late": {"$sum": "$late"},
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$unset": "_id"},
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [range_start, range_end]},
            }
        },
        {
            "$set": {
                "joined": {"$ifNull": ["$joined", 0]},
                "present": {"$ifNull": ["$present", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "headcount": {
                        "$sum": "$joined",
                        "window": {"documents": ["unbounded", "current"]},
                    }
                },
            }
        },
        {
            "$set": {
                "rate_units": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        rounded_ratio("$present", "$headcount", 10000),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "window_units": {"$sum": "$rate_units", "window": window},
                    "window_rates": {"$sum": {"$cond": [has_rate, 1, 0]}, "window": window},
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "is_working_day": "$is_working_day",
                "headcount": "$headcount",
                "present_count": "$present",
                "late_count": "$late",
                "attendance_rate": {
                    "$cond": [has_rate, {"$divide": ["$rate_units", 10000]}, None]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$window_rates", 0]},
                        {"$divide": [rounded_ratio("$window_units", "$window_rates", 1), 10000]},
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_ms()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = punch_time(body.punched_at)
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = punch_time(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        later = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts}, "punch_out": None}
        )
        if later is not None:
            raise validation_error(["body", "punched_at"], "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= record["punch_in"]:
        raise validation_error(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - record["punch_in"] > MAX_SHIFT:
        raise validation_error(["body", "punched_at"], "punched_at is more than 24 hours after punch_in")

    derived = derive(record["status"], record["punch_in"], ts, record["date"], emp)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    day: Annotated[date, Path(alias="date")],
    body: RegularizeIn,
):
    emp = get_employee(emp_code)
    day_str = day.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_str})
    if record is None:
        raise HTTPException(404, "no attendance record for that date")

    status = body.status or record["status"]
    if status in PRESENCE:
        punch_in = truncate_to_second(ms_to_dt(body.punch_in)) if body.punch_in is not None else record.get("punch_in")
        punch_out = truncate_to_second(ms_to_dt(body.punch_out)) if body.punch_out is not None else record.get("punch_out")
        if punch_in is None:
            raise validation_error(["body", "punch_in"], f"{status} requires a punch_in")
        if attendance_date(punch_in, emp) != day_str:
            raise validation_error(["body", "punch_in"], "punch_in must fall on the record's attendance date")
        if punch_out is not None and not punch_in < punch_out <= punch_in + MAX_SHIFT:
            raise validation_error(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(["body", "status"], f"{status} cannot have punch times")
        punch_in = punch_out = None

    before = record_state(record)
    after = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **derive(status, punch_in, punch_out, day_str, emp),
    }
    changes = {
        field: {"from": before[field], "to": value}
        for field, value in after.items()
        if before[field] != value
    }
    if not changes:
        raise validation_error(["body"], "request does not change anything")

    entry = {
        "at": now_utc_ms(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_size = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_size]},
        },
        [
            {
                "$set": {
                    **{field: {"$literal": value} for field, value in after.items()},
                    "history": {
                        "$concatArrays": [
                            {"$ifNull": ["$history", []]},
                            [{"$literal": entry}],
                        ]
                    },
                }
            }
        ],
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working_days = count_weekdays(start, date.fromisoformat(last))

    stats = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), {})
    present_days = stats.get("present_days", 0)
    attendance_pct = None
    if working_days:
        attendance_pct = half_up(Decimal(str(present_days)) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = list(db.employees.aggregate(summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
):
    check_trend_range(start, end)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


def require_params(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ["query", name], "msg": "Field required"} for name in missing]
        )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[date], Query(alias="from")] = None,
    end: Annotated[Optional[date], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "projection": {"_id": 0},
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require_params(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require_params(month=month)
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require_params(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            require_params(department=department, **{"from": start, "to": end})
            check_trend_range(start, end)
            collection, pipeline = "employees", trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command("explain", command, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw)),
    }
