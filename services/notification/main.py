# ==============================================================
# notification-service/main.py
# Handles: SMS (SNS), Email (SES), WhatsApp, Push notifications
# ==============================================================
import uuid
import os
import boto3
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Boolean, Text
from typing import Optional
import logging

from shared.database import Base, engine, get_db
from shared.auth import staff_only, admin_only

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Shivam Hospital — Notification Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

REGION = os.getenv("AWS_REGION", "eu-north-1")

# ── Models ────────────────────────────────────────────────────
class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id  = Column(String, nullable=True, index=True)
    channel     = Column(String, nullable=False)  # sms/email/whatsapp/push
    recipient   = Column(String, nullable=False)
    subject     = Column(String, nullable=True)
    message     = Column(Text, nullable=False)
    status      = Column(String, default="sent")   # sent/failed/delivered
    sent_at     = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── Schemas ───────────────────────────────────────────────────
class SMSRequest(BaseModel):
    phone:      str
    message:    str
    patient_id: Optional[str]

class EmailRequest(BaseModel):
    to_email:   str
    subject:    str
    body_html:  str
    body_text:  str
    patient_id: Optional[str]

class BulkNotificationRequest(BaseModel):
    patient_ids: list
    channel:     str   # sms/email
    message:     str
    subject:     Optional[str]

class AppointmentReminderRequest(BaseModel):
    patient_phone: str
    patient_email: Optional[str]
    patient_name:  str
    doctor_name:   str
    appt_date:     str
    appt_time:     str
    appt_type:     str
    video_link:    Optional[str]

# ── SMS via AWS SNS ───────────────────────────────────────────
def send_sms(phone: str, message: str) -> bool:
    try:
        sns = boto3.client("sns", region_name=REGION)
        sns.publish(
            PhoneNumber=phone,
            Message=message,
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {
                    "DataType": "String",
                    "StringValue": "Transactional"
                },
                "AWS.SNS.SMS.SenderID": {
                    "DataType": "String",
                    "StringValue": "SHIVAM"
                }
            }
        )
        return True
    except Exception as e:
        logger.error(f"SMS failed to {phone}: {e}")
        return False

# ── Email via AWS SES ─────────────────────────────────────────
def send_email(to_email: str, subject: str, body_html: str, body_text: str) -> bool:
    try:
        ses = boto3.client("ses", region_name=REGION)
        ses.send_email(
            Source=os.getenv("FROM_EMAIL", "noreply@shivamhospital.in"),
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": body_text, "Charset": "UTF-8"},
                    "Html": {"Data": body_html, "Charset": "UTF-8"}
                }
            }
        )
        return True
    except Exception as e:
        logger.error(f"Email failed to {to_email}: {e}")
        return False

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status": "healthy", "service": "notification-service"}

@app.post("/notify/sms")
def send_sms_notification(request: SMSRequest, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Send SMS to a patient."""
    success = send_sms(request.phone, request.message)

    log = NotificationLog(
        patient_id=request.patient_id,
        channel="sms",
        recipient=request.phone,
        message=request.message,
        status="sent" if success else "failed"
    )
    db.add(log)
    db.commit()

    if not success:
        raise HTTPException(status_code=500, detail="SMS delivery failed")
    return {"message": "SMS sent", "status": "sent"}

@app.post("/notify/email")
def send_email_notification(request: EmailRequest, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Send email to a patient."""
    success = send_email(request.to_email, request.subject, request.body_html, request.body_text)

    log = NotificationLog(
        patient_id=request.patient_id,
        channel="email",
        recipient=request.to_email,
        subject=request.subject,
        message=request.body_text,
        status="sent" if success else "failed"
    )
    db.add(log)
    db.commit()

    if not success:
        raise HTTPException(status_code=500, detail="Email delivery failed")
    return {"message": "Email sent", "status": "sent"}

@app.post("/notify/appointment-reminder")
def send_appointment_reminder(request: AppointmentReminderRequest, db: Session = Depends(get_db)):
    """Send appointment reminder via SMS + Email."""
    sms_msg = (
        f"Reminder: Your appointment with Dr. {request.doctor_name} "
        f"is on {request.appt_date} at {request.appt_time}.\n"
    )
    if request.video_link:
        sms_msg += f"Video link: {request.video_link}\n"
    sms_msg += "Shivam Hospital"

    email_html = f"""
    <html><body>
    <h2>Appointment Reminder — Shivam Hospital</h2>
    <p>Dear {request.patient_name},</p>
    <table border="1" cellpadding="8">
      <tr><td><b>Doctor</b></td><td>Dr. {request.doctor_name}</td></tr>
      <tr><td><b>Date</b></td><td>{request.appt_date}</td></tr>
      <tr><td><b>Time</b></td><td>{request.appt_time}</td></tr>
      <tr><td><b>Type</b></td><td>{request.appt_type}</td></tr>
      {"<tr><td><b>Video Link</b></td><td><a href='" + request.video_link + "'>Join Call</a></td></tr>" if request.video_link else ""}
    </table>
    <p>Please arrive 10 minutes early.</p>
    <p>Shivam Hospital Team</p>
    </body></html>
    """

    # Send SMS
    sms_ok = send_sms(request.patient_phone, sms_msg)

    # Send Email if provided
    email_ok = True
    if request.patient_email:
        email_ok = send_email(
            request.patient_email,
            f"Appointment Reminder — Dr. {request.doctor_name} on {request.appt_date}",
            email_html,
            sms_msg
        )

    return {
        "sms_sent":   sms_ok,
        "email_sent": email_ok,
        "message":    "Reminders sent"
    }

@app.post("/notify/lab-report-ready")
def notify_lab_report_ready(
    patient_phone: str,
    patient_name:  str,
    report_type:   str,
    db: Session = Depends(get_db)
):
    """Notify patient their lab report is ready."""
    message = (
        f"Dear {patient_name}, your {report_type} report is ready. "
        f"View it in the Shivam Hospital app under 'My Health Records'. "
        f"Shivam Hospital"
    )
    success = send_sms(patient_phone, message)

    log = NotificationLog(
        channel="sms",
        recipient=patient_phone,
        message=message,
        status="sent" if success else "failed"
    )
    db.add(log)
    db.commit()

    return {"message": "Notification sent", "status": "sent" if success else "failed"}

@app.post("/notify/low-inventory")
def notify_low_inventory(
    item_name:  str,
    quantity:   int,
    user: dict = Depends(admin_only),
    db: Session = Depends(get_db)
):
    """Alert admin when pharmacy/surgical inventory is low."""
    try:
        sns = boto3.client("sns", region_name=REGION)
        sns.publish(
            TopicArn=os.getenv("INVENTORY_TOPIC_ARN", ""),
            Subject=f"LOW STOCK ALERT: {item_name}",
            Message=f"ALERT: {item_name} is running low. Current stock: {quantity} units. Please reorder immediately."
        )
        return {"message": "Low inventory alert sent"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/notify/logs/{patient_id}")
def get_notification_logs(patient_id: str, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Get notification history for a patient."""
    return db.query(NotificationLog).filter(
        NotificationLog.patient_id == patient_id
    ).order_by(NotificationLog.sent_at.desc()).limit(50).all()

