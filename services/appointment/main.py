# ==============================================================
# appointment-service/main.py
# Handles: slot booking, live queue, video consult, doctor schedule
# ==============================================================
import uuid
import os
from datetime import datetime, date, timedelta
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Boolean, Date, Time, Integer, Text
import boto3
import json
import logging

from shared.database import Base, engine, get_db, redis_client
from shared.auth import get_current_user, patient_only, doctor_only, staff_only, any_user

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Shivam Hospital — Appointment Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Models ────────────────────────────────────────────────────
class Doctor(Base):
    __tablename__ = "doctors"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name        = Column(String, nullable=False)
    speciality  = Column(String, nullable=False)
    language    = Column(String, default="English,Hindi")
    fees        = Column(Integer, default=500)
    hospital_id = Column(String)
    is_available= Column(Boolean, default=True)
    on_leave    = Column(Boolean, default=False)
    on_break    = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

class Appointment(Base):
    __tablename__ = "appointments"
    id           = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id   = Column(String, nullable=False, index=True)
    patient_name = Column(String, nullable=False)
    patient_phone= Column(String, nullable=False)
    doctor_id    = Column(String, nullable=False, index=True)
    doctor_name  = Column(String, nullable=False)
    appt_date    = Column(Date, nullable=False)
    appt_time    = Column(String, nullable=False)
    status       = Column(String, default="booked")  # booked/waiting/in-cabin/completed/no-show/cancelled
    type         = Column(String, default="in-person") # in-person/teleconsult
    video_link   = Column(String, nullable=True)
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── Schemas ───────────────────────────────────────────────────
class BookAppointmentRequest(BaseModel):
    doctor_id:    str
    appt_date:    str   # YYYY-MM-DD
    appt_time:    str   # HH:MM
    type:         str = "in-person"
    notes:        str = None

class UpdateQueueStatus(BaseModel):
    appointment_id: str
    status:         str  # waiting/in-cabin/completed/no-show

class DoctorAvailabilityUpdate(BaseModel):
    on_leave: bool = False
    on_break: bool = False

# ── Helper: Send notification ────────────────────────────────
def send_appointment_notification(patient_phone: str, message: str):
    """Send SMS via SNS when appointment is booked/updated."""
    try:
        sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "eu-north-1"))
        sns.publish(
            PhoneNumber=patient_phone,
            Message=message,
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}
            }
        )
    except Exception as e:
        logger.error(f"Notification failed: {e}")

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status": "healthy", "service": "appointment-service"}

@app.get("/doctors")
def get_doctors(
    speciality: Optional[str] = None,
    language:   Optional[str] = None,
    max_fees:   Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Patient — discover doctors with filters."""
    query = db.query(Doctor).filter(Doctor.is_available == True, Doctor.on_leave == False)
    if speciality:
        query = query.filter(Doctor.speciality.ilike(f"%{speciality}%"))
    if language:
        query = query.filter(Doctor.language.ilike(f"%{language}%"))
    if max_fees:
        query = query.filter(Doctor.fees <= max_fees)
    return query.all()

@app.get("/doctors/{doctor_id}/slots")
def get_available_slots(doctor_id: str, date: str, db: Session = Depends(get_db)):
    """
    Get available slots for a doctor on a date.
    Checks Redis for locked slots and RDS for booked slots.
    """
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    if doctor.on_leave or doctor.on_break:
        return {"available_slots": [], "message": "Doctor not available today"}

    # Generate all slots (9AM - 5PM, 15 min each)
    all_slots = []
    start = datetime.strptime("09:00", "%H:%M")
    end   = datetime.strptime("17:00", "%H:%M")
    while start < end:
        all_slots.append(start.strftime("%H:%M"))
        start += timedelta(minutes=15)

    # Remove booked slots from RDS
    booked = db.query(Appointment).filter(
        Appointment.doctor_id == doctor_id,
        Appointment.appt_date == date,
        Appointment.status.notin_(["cancelled", "no-show"])
    ).all()
    booked_times = {a.appt_time for a in booked}

    # Remove locked slots from Redis (being booked right now)
    locked_times = set()
    for slot in all_slots:
        lock_key = f"slot_lock:{doctor_id}:{date}:{slot}"
        if redis_client.exists(lock_key):
            locked_times.add(slot)

    available = [s for s in all_slots if s not in booked_times and s not in locked_times]
    return {"doctor_id": doctor_id, "date": date, "available_slots": available}

@app.post("/appointments/book")
def book_appointment(
    request: BookAppointmentRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(any_user),
    db: Session = Depends(get_db)
):
    """
    Book an appointment.
    1. Lock slot in Redis (30 sec) to prevent double booking
    2. Save to RDS
    3. Send SMS confirmation
    """
    doctor = db.query(Doctor).filter(Doctor.id == request.doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    # Lock slot in Redis for 30 seconds (prevents double booking)
    lock_key   = f"slot_lock:{request.doctor_id}:{request.appt_date}:{request.appt_time}"
    lock_value = user.get("sub")  # user ID

    locked = redis_client.set(lock_key, lock_value, nx=True, ex=30)
    if not locked:
        raise HTTPException(status_code=409, detail="Slot just taken by another patient. Choose another time.")

    try:
        # Check again in RDS
        existing = db.query(Appointment).filter(
            Appointment.doctor_id == request.doctor_id,
            Appointment.appt_date == request.appt_date,
            Appointment.appt_time == request.appt_time,
            Appointment.status.notin_(["cancelled", "no-show"])
        ).first()

        if existing:
            redis_client.delete(lock_key)
            raise HTTPException(status_code=409, detail="Slot already booked")

        # Generate video link for teleconsult
        video_link = None
        if request.type == "teleconsult":
            video_link = f"https://meet.shivamhospital.in/{str(uuid.uuid4())[:8]}"

        appointment = Appointment(
            id=str(uuid.uuid4()),
            patient_id=user.get("sub"),
            patient_name=user.get("name", "Patient"),
            patient_phone=user.get("phone_number", ""),
            doctor_id=request.doctor_id,
            doctor_name=doctor.name,
            appt_date=request.appt_date,
            appt_time=request.appt_time,
            status="booked",
            type=request.type,
            video_link=video_link,
            notes=request.notes
        )
        db.add(appointment)
        db.commit()
        db.refresh(appointment)

        # Make lock permanent (slot is now booked)
        redis_client.persist(lock_key)

        # Send SMS in background
        msg = (
            f"Appointment confirmed!\n"
            f"Doctor: Dr. {doctor.name}\n"
            f"Date: {request.appt_date} at {request.appt_time}\n"
            f"Type: {request.type}\n"
        )
        if video_link:
            msg += f"Video link: {video_link}\n"
        msg += "Shivam Hospital"

        background_tasks.add_task(
            send_appointment_notification,
            user.get("phone_number", ""),
            msg
        )

        # Also publish to SNS topic for other services
        try:
            sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "eu-north-1"))
            sns.publish(
                TopicArn=os.getenv("APPOINTMENT_TOPIC_ARN", ""),
                Message=json.dumps({
                    "event":          "appointment_booked",
                    "appointment_id": appointment.id,
                    "patient_id":     appointment.patient_id,
                    "doctor_id":      appointment.doctor_id,
                    "date":           str(appointment.appt_date),
                    "time":           appointment.appt_time
                })
            )
        except Exception as e:
            logger.error(f"SNS publish failed: {e}")

        return appointment

    except HTTPException:
        raise
    except Exception as e:
        redis_client.delete(lock_key)
        logger.error(f"Booking failed: {e}")
        raise HTTPException(status_code=500, detail="Booking failed. Please try again.")

@app.get("/appointments/my")
def get_my_appointments(user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Get logged-in patient's appointments."""
    return db.query(Appointment).filter(
        Appointment.patient_id == user.get("sub")
    ).order_by(Appointment.appt_date.desc()).all()

@app.get("/queue/doctor/{doctor_id}")
def get_doctor_queue(doctor_id: str, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """
    Doctor's live queue — Waiting / In-Cabin / No-Show.
    Checks Redis first (fast), falls back to RDS.
    """
    today   = date.today().isoformat()

    # Try Redis cache first
    cache_key = f"queue:{doctor_id}:{today}"
    cached    = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fall back to RDS
    appointments = db.query(Appointment).filter(
        Appointment.doctor_id  == doctor_id,
        Appointment.appt_date  == today,
        Appointment.status.in_(["booked", "waiting", "in-cabin"])
    ).order_by(Appointment.appt_time).all()

    queue = {
        "waiting":  [a for a in appointments if a.status == "waiting"],
        "in_cabin": [a for a in appointments if a.status == "in-cabin"],
        "upcoming": [a for a in appointments if a.status == "booked"],
        "total":    len(appointments)
    }

    # Cache in Redis for 30 seconds
    redis_client.setex(cache_key, 30, json.dumps(queue, default=str))
    return queue

@app.patch("/queue/update-status")
def update_queue_status(
    request: UpdateQueueStatus,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """Doctor updates patient status in queue."""
    appointment = db.query(Appointment).filter(Appointment.id == request.appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")

    appointment.status = request.status
    db.commit()

    # Invalidate Redis queue cache
    cache_key = f"queue:{appointment.doctor_id}:{appointment.appt_date}"
    redis_client.delete(cache_key)

    return {"message": f"Status updated to {request.status}"}

@app.patch("/doctors/{doctor_id}/availability")
def update_doctor_availability(
    doctor_id: str,
    request: DoctorAvailabilityUpdate,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """Doctor toggles On-Leave or Emergency Break."""
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if not doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    doctor.on_leave = request.on_leave
    doctor.on_break = request.on_break
    db.commit()

    status = "on leave" if request.on_leave else ("on break" if request.on_break else "available")
    return {"message": f"Dr. {doctor.name} is now {status}"}

@app.delete("/appointments/{appointment_id}/cancel")
def cancel_appointment(
    appointment_id: str,
    user: dict = Depends(any_user),
    db: Session = Depends(get_db)
):
    """Cancel an appointment — frees the slot."""
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")

    appointment.status = "cancelled"
    db.commit()

    # Free the slot lock in Redis
    lock_key = f"slot_lock:{appointment.doctor_id}:{appointment.appt_date}:{appointment.appt_time}"
    redis_client.delete(lock_key)

    return {"message": "Appointment cancelled successfully"}

