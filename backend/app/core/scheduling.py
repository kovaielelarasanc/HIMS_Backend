# app/core/scheduling.py
from datetime import datetime, time, timedelta
from typing import List, Tuple
from sqlalchemy.orm import Session
from app.models.opd import OpdSchedule, Appointment


def _time_range(start: time, end: time,
                minutes: int) -> List[Tuple[time, time]]:
    slots = []
    cur = datetime.combine(datetime.today().date(), start)
    end_dt = datetime.combine(datetime.today().date(), end)
    delta = timedelta(minutes=minutes)
    while cur + delta <= end_dt:
        slots.append((cur.time(), (cur + delta).time()))
        cur += delta
    return slots


def doctor_slots(db: Session,
                 doctor_user_id: int,
                 date_obj,
                 slot_minutes: int = 15):
    # Try schedule; if none, fallback 09:00-17:00
    weekday = date_obj.weekday()
    sched = db.query(OpdSchedule).filter(
        OpdSchedule.doctor_user_id == doctor_user_id,
        OpdSchedule.weekday == weekday).all()
    ranges = []
    if sched:
        for s in sched:
            ranges.extend(_time_range(s.start_time, s.end_time, slot_minutes))
    else:
        ranges = _time_range(time(9, 0), time(17, 0), slot_minutes)

    # Remove busy slots
    appts = db.query(Appointment).filter(
        Appointment.doctor_user_id == doctor_user_id,
        Appointment.date == date_obj).all()
    busy = {(a.slot_start, a.slot_end) for a in appts}

    free = [(s, e) for (s, e) in ranges if (s, e) not in busy]
    return [{
        "start": s.strftime("%H:%M"),
        "end": e.strftime("%H:%M")
    } for s, e in free]
