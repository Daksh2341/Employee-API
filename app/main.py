"""Employee Attendance & Analytics API."""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="employee_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="employee_department_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="employee_department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="attendance_employee_date_status")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)], name="attendance_month_late")

@app.on_event("startup")
def startup():
    create_indexes()


def fail422(message="invalid input"):
    raise HTTPException(422, message)


def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception: fail422("date must be YYYY-MM-DD")


def parse_month(s: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s): fail422("month must be YYYY-MM")
    y, m = map(int, s.split("-")); start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return start, end


def milliseconds(v):
    if v is None: return None
    return int(v.replace(tzinfo=UTC).timestamp() * 1000)


def instant(value: Optional[int]) -> datetime:
    if value is None: value = int(datetime.now(UTC).timestamp() * 1000)
    if type(value) is not int or not 100000000000 <= value <= 4102444800000: fail422("timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value // 1000, UTC)


def ist(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def shift_time(s: str) -> time:
    try:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s): raise ValueError()
        return time.fromisoformat(s)
    except Exception: fail422("shift time must be HH:MM")


def attendance_day(dt: datetime, emp) -> date:
    local = ist(dt).astimezone(IST)
    shs, she = shift_time(emp["shift_start"]), shift_time(emp["shift_end"])
    d = local.date()
    if she <= shs and local.time().replace(tzinfo=None) < she: d -= timedelta(days=1)
    return d


def late_minutes(pin: datetime, emp) -> int:
    local = ist(pin).astimezone(IST); shs = shift_time(emp["shift_start"])
    d = local.date()
    if shift_time(emp["shift_end"]) <= shs and local.time().replace(tzinfo=None) < shift_time(emp["shift_end"]): d -= timedelta(days=1)
    start = datetime.combine(d, shs, IST)
    delta = int((local - start).total_seconds())
    return delta // 60 if delta > 600 else 0


def shift_end_instant(pin: datetime, emp) -> datetime:
    local = ist(pin).astimezone(IST); shs, she = shift_time(emp["shift_start"]), shift_time(emp["shift_end"])
    d = local.date()
    if she <= shs and local.time().replace(tzinfo=None) < she: d -= timedelta(days=1)
    end_day = d + timedelta(days=1) if she <= shs else d
    return datetime.combine(end_day, she, IST).astimezone(UTC)


def derived(pin, pout, emp):
    lm = late_minutes(pin, emp) if pin else 0
    if not pin or not pout: return {"late_minutes": lm, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((ist(pout) - ist(pin)).total_seconds())
    hours = float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    overtime = max(0, int((ist(pout) - shift_end_instant(pin, emp)).total_seconds()) // 60)
    if overtime < 30: overtime = 0
    return {"late_minutes": lm, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}


def serialize_record(doc):
    if not doc: return doc
    out = {k: v for k, v in doc.items() if k != "_id"}
    for k in ("punch_in", "punch_out"):
        out[k] = milliseconds(out.get(k))
    out["history"] = out.get("history") or []
    for h in out["history"]:
        h["at"] = milliseconds(h.get("at"))
        for ch in h.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(ch.get(side), datetime): ch[side] = milliseconds(ch[side])
    out.setdefault("late_minutes", 0); out.setdefault("overtime_minutes", 0); out.setdefault("half_day", False)
    return out


def serialize_employee(doc):
    out = {k:v for k,v in doc.items() if k != "_id"}
    out["created_at"] = milliseconds(out["created_at"])
    return out


def rate_round(v, places=2):
    return float(Decimal(str(v)).quantize(Decimal("1") if places == 0 else Decimal("0." + "0"*(places-1) + "1"), rounding=ROUND_HALF_UP))

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date
    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls,v):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v): raise ValueError("must be HH:MM")
        return v
    @field_validator("shift_end")
    @classmethod
    def shifts_differ(cls,v,info):
        if info.data.get("shift_start") == v: raise ValueError("shift times must differ")
        return v

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not 100000000000 <= v <= 4102444800000): raise ValueError("epoch milliseconds required")
        return v

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not 100000000000 <= v <= 4102444800000): raise ValueError("epoch milliseconds required")
        return v

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or not 100000000000 <= v <= 4102444800000): raise ValueError("epoch milliseconds required")
        return v

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["joined_on"]=doc["joined_on"].isoformat(); doc["created_at"]=datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return serialize_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None, page:int=Query(1,ge=1), page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    items=[serialize_employee(x) for x in db.employees.find(q,{"_id":0}).sort("emp_code",1).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=instant(body.punched_at); d=attendance_day(ts,emp).isoformat()
    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":late_minutes(ts,emp),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return serialize_record(doc)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code is not None:q["emp_code"]=emp_code
    if date_from:
        parse_date(date_from); q.setdefault("date",{})["$gte"]=date_from
    if date_to:
        parse_date(date_to); q.setdefault("date",{})["$lte"]=date_to
    if date_from and date_to and date_from>date_to: fail422("date_from must be <= date_to")
    if status:q["status"]=status
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q,{"_id":0}).sort([("date",-1),("emp_code",1)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[serialize_record(x) for x in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=instant(body.punched_at)
    doc=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts},"punch_out":None},sort=[("punch_in",-1)])
    if not doc:
        if db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":ts}}): raise HTTPException(409,"already punched out")
        raise HTTPException(404,"no punch-in found")
    if ts<=ist(doc["punch_in"]) or ts-ist(doc["punch_in"])>timedelta(hours=24): fail422("punch-out must be after punch-in and within 24 hours")
    fields=derived(doc["punch_in"],ts,emp)
    updated=db.attendance_logs.find_one_and_update({"_id":doc["_id"],"punch_out":None},{"$set":{"punch_out":ts,**fields}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"already punched out")
    return serialize_record(updated)

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code:str,date_str:str,body:RegularizeIn):
    parse_date(date_str)
    emp=db.employees.find_one({"emp_code":emp_code})
    doc=db.attendance_logs.find_one({"emp_code":emp_code,"date":date_str})
    if not emp or not doc: raise HTTPException(404,"employee or attendance record not found")
    status=body.status if body.status is not None else doc["status"]
    if status in ("ABSENT","LEAVE") and (body.punch_in is not None or body.punch_out is not None): fail422("absence/leave cannot have punch times")
    pin=instant(body.punch_in) if body.punch_in is not None else doc.get("punch_in")
    pout=instant(body.punch_out) if body.punch_out is not None else doc.get("punch_out")
    if status in ("ABSENT","LEAVE"): pin=pout=None
    elif pin is None: fail422("presence status requires punch_in")
    elif attendance_day(pin,emp).isoformat()!=date_str: fail422("punch_in must belong to record date")
    if pout is not None and (pout<=pin or pout-pin>timedelta(hours=24)): fail422("invalid punch_out interval")
    fields={"status":status,"punch_in":pin,"punch_out":pout,**derived(pin,pout,emp)}
    old={**doc,"late_minutes":doc.get("late_minutes",0),"overtime_minutes":doc.get("overtime_minutes",0),"half_day":doc.get("half_day",False)}
    changes={}
    for k,v in fields.items():
        a=old.get(k)
        if k in ("punch_in","punch_out"):
            a=ist(a) if a else None; v=ist(v) if v else None
        if a!=v:
            changes[k]={"from":old.get(k),"to":fields[k]}
    if not changes: fail422("request makes no changes")
    hist={"at":datetime.now(UTC),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    history_guard = {"history": doc.get("history", [])} if "history" in doc else {"history": {"$exists": False}}
    updated=db.attendance_logs.find_one_and_update({"_id":doc["_id"], **history_guard},{"$set":fields,"$push":{"history":hist}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record changed concurrently")
    return serialize_record(updated)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    start,end=parse_month(month); emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    first=max(start,parse_date(emp["joined_on"])); working=sum(1 for i in range((end-first).days+1) if (first+timedelta(days=i)).weekday()<5) if first<=end else 0
    p=[{"$match":{"emp_code":emp_code,"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$in":["$status",PRESENCE]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    rows=list(db.attendance_logs.aggregate(p)); x=rows[0] if rows else {}
    present=float(x.get("present_days",0)); pct=rate_round(present/working*100,2) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,"leave_days":x.get("leave_days",0),"late_count":x.get("late_count",0),"total_late_minutes":x.get("total_late_minutes",0),"total_overtime_minutes":x.get("total_overtime_minutes",0),"attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    start,end=parse_month(month)
    pipeline = [
        {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}
            ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"log_day": {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$dateFromString": {"dateString": {"$concat": ["$logs.date", "T00:00:00Z"]}}}, None]}}},
        {"$group": {
            "_id": "$department", "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$not": [{"$in": [{"$dayOfWeek": "$log_day"}, [1, 7]]}]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employees"}, "present_days": 1,
            "avg_work_hours": {"$avg": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    items=list(db.employees.aggregate(pipeline))
    for x in items:
        if x.get("avg_work_hours") is not None:x["avg_work_hours"]=rate_round(x["avg_work_hours"])
        x["present_days"]=float(x["present_days"])
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    start,end=parse_month(month)
    p=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None:p.append({"$match":{"employee.department":department}})
    p += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},{"$project":{"_id":0,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1,"rank":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(p))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date), parse_date(to_date)
    if end < start or (end - start).days + 1 > 92: fail422("invalid date range")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    p = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date", "present": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": {"$concat": ["$_id", "T00:00:00Z"]}}}, "present": 1, "late": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)]}}},
        {"$set": {"present": {"$ifNull": ["$present", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}, "dep": department}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dep"]}, {"$lte": ["$joined_on", "$$day"]}]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}, "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$date"}, [1, 7]]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}, "is_working_day": 1, "headcount": 1, "present_count": "$present", "late_count": "$late", "attendance_rate": 1, "moving_avg_7d": 1}}, {"$sort": {"date": 1}}
    ]
    items = list(db.attendance_logs.aggregate(p, allowDiskUse=True))
    # If there are no matching logs, seed the output from employees and a date spine in MongoDB.
    if not items:
        p = [
            {"$documents": [{"date": datetime.combine(start, time.min, UTC)}]},
            {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)]}}},
            {"$lookup": {"from": "employees", "let": {"day": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "hc"}},
            {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}, "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$date"}, [1, 7]]}]}, "present_count": 0.0, "late_count": 0}},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, 0.0, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}, {"$sort": {"date": 1}}
        ]
        items = list(db.command("aggregate", 1, pipeline=p, cursor={})["cursor"]["firstBatch"])
    for item in items:
        item["present_count"] = float(item.get("present_count", 0))
        for k in ("attendance_rate", "moving_avg_7d"):
            if item.get(k) is not None: item[k] = rate_round(item[k], 4)
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to"),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed:fail422("unknown endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if date_from:q.setdefault("date",{})["$gte"]=parse_date(date_from).isoformat()
        if date_to:q.setdefault("date",{})["$lte"]=parse_date(date_to).isoformat()
        if date_from and date_to and date_from>date_to:fail422("invalid date range")
        if status:q["status"]=status
        cmd={"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        result=db.command("explain",cmd,verbosity="executionStats"); collection="attendance_logs"
    else:
        if not month and endpoint!="department_trend":fail422("month is required")
        if endpoint=="department_trend":
            if not department or not from_date or not to_date:fail422("department, from and to are required")
            start,end=parse_date(from_date),parse_date(to_date)
            if end<start or (end-start).days+1>92:fail422("invalid date range")
            query={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
        else:
            start,end=parse_month(month)
            query={"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}
            if endpoint=="employee_monthly":
                if not emp_code:fail422("emp_code is required")
                query["emp_code"]=emp_code
            if endpoint=="late_leaderboard":query["late_minutes"]={"$gt":0}
        cmd={"find":"attendance_logs","filter":query,"sort":{"date":1}}
        result=db.command("explain",cmd,verbosity="executionStats"); collection="attendance_logs"
    return {"endpoint":endpoint,"collection":collection,"explain":result}
