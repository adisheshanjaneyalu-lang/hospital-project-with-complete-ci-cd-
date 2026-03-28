# ==============================================================
# records-service/main.py
# Handles: lab reports, X-rays, prescriptions — stored in S3
# ==============================================================
import uuid
import os
import boto3
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Text, Integer
import logging

from shared.database import Base, engine, get_db
from shared.auth import get_current_user, patient_only, staff_only, any_user

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Shivam Hospital — Records Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── S3 Config ─────────────────────────────────────────────────
S3_BUCKETS = {
    "lab_reports":    os.getenv("S3_LAB_REPORTS",    "shivam-hospital-lab-reports"),
    "xrays":          os.getenv("S3_XRAYS",          "shivam-hospital-xrays"),
    "prescriptions":  os.getenv("S3_PRESCRIPTIONS",  "shivam-hospital-prescriptions"),
    "medical_records":os.getenv("S3_MEDICAL_RECORDS","shivam-hospital-medical-records"),
}

def get_s3():
    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1"))

# ── Models ────────────────────────────────────────────────────
class HealthRecord(Base):
    __tablename__ = "health_records"
    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id    = Column(String, nullable=False, index=True)
    record_type   = Column(String, nullable=False)  # lab_report/xray/prescription/discharge
    title         = Column(String, nullable=False)
    description   = Column(Text, nullable=True)
    s3_bucket     = Column(String, nullable=False)
    s3_key        = Column(String, nullable=False)
    file_size     = Column(Integer, nullable=True)
    uploaded_by   = Column(String, nullable=False)  # doctor/lab/admin
    appointment_id= Column(String, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

class Prescription(Base):
    __tablename__ = "prescriptions"
    id             = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id     = Column(String, nullable=False, index=True)
    doctor_id      = Column(String, nullable=False)
    doctor_name    = Column(String, nullable=False)
    appointment_id = Column(String, nullable=True)
    diagnosis      = Column(Text, nullable=False)
    medicines      = Column(Text, nullable=False)   # JSON string
    instructions   = Column(Text, nullable=True)
    s3_key         = Column(String, nullable=True)  # signed PDF in S3
    created_at     = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── Schemas ───────────────────────────────────────────────────
class PrescriptionCreate(BaseModel):
    patient_id:     str
    appointment_id: Optional[str]
    diagnosis:      str
    medicines:      list   # [{"name": "Paracetamol", "dose": "500mg", "frequency": "3x/day", "days": 5}]
    instructions:   Optional[str]

class PresignedURLRequest(BaseModel):
    record_id: str

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status": "healthy", "service": "records-service"}

@app.post("/records/upload")
async def upload_record(
    patient_id:     str  = Form(...),
    record_type:    str  = Form(...),   # lab_report / xray / prescription
    title:          str  = Form(...),
    description:    str  = Form(None),
    appointment_id: str  = Form(None),
    file:           UploadFile = File(...),
    user: dict = Depends(staff_only),
    db:   Session = Depends(get_db)
):
    """
    Upload medical file to S3.
    Only doctors/admins can upload. File stored in correct bucket.
    """
    # Validate file type
    allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/dicom"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"File type {file.content_type} not allowed")

    # Max 50MB
    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Max 50MB")

    # Determine S3 bucket
    bucket_map = {
        "lab_report":   "lab_reports",
        "xray":         "xrays",
        "prescription": "prescriptions",
    }
    bucket_key = bucket_map.get(record_type, "medical_records")
    bucket     = S3_BUCKETS[bucket_key]

    # Upload to S3 — organised by patient_id/year/month/
    s3_key = (
        f"{patient_id}/"
        f"{datetime.utcnow().year}/{datetime.utcnow().month:02d}/"
        f"{str(uuid.uuid4())}_{file.filename}"
    )

    s3 = get_s3()
    s3.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=contents,
        ContentType=file.content_type,
        ServerSideEncryption="AES256",
        Metadata={
            "patient_id":  patient_id,
            "record_type": record_type,
            "uploaded_by": user.get("sub", "unknown"),
            "title":       title
        }
    )

    # Save metadata to RDS
    record = HealthRecord(
        patient_id=patient_id,
        record_type=record_type,
        title=title,
        description=description,
        s3_bucket=bucket,
        s3_key=s3_key,
        file_size=len(contents),
        uploaded_by=user.get("sub"),
        appointment_id=appointment_id
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # Send notification via SNS
    try:
        sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "eu-north-1"))
        sns.publish(
            TopicArn=os.getenv("LAB_REPORT_TOPIC_ARN", ""),
            Message=f"New {record_type} uploaded for patient {patient_id}: {title}"
        )
    except Exception as e:
        logger.error(f"SNS notification failed: {e}")

    return {"record_id": record.id, "message": "File uploaded successfully"}

@app.get("/records/patient/{patient_id}")
def get_patient_records(
    patient_id:  str,
    record_type: Optional[str] = None,
    user: dict   = Depends(any_user),
    db: Session  = Depends(get_db)
):
    """
    Get all records for a patient.
    Patients can only see their own. Doctors/Admin see all.
    """
    groups = user.get("cognito:groups", [])
    if "patients" in groups and user.get("sub") != patient_id:
        raise HTTPException(status_code=403, detail="You can only view your own records")

    query = db.query(HealthRecord).filter(HealthRecord.patient_id == patient_id)
    if record_type:
        query = query.filter(HealthRecord.record_type == record_type)

    return query.order_by(HealthRecord.created_at.desc()).all()

@app.post("/records/download-url")
def get_download_url(request: PresignedURLRequest, user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """
    Generate a presigned S3 URL for downloading a file.
    URL expires in 15 minutes — secure access without making S3 public.
    """
    record = db.query(HealthRecord).filter(HealthRecord.id == request.record_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Patients can only access their own records
    groups = user.get("cognito:groups", [])
    if "patients" in groups and user.get("sub") != record.patient_id:
        raise HTTPException(status_code=403, detail="Access denied")

    s3  = get_s3()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": record.s3_bucket, "Key": record.s3_key},
        ExpiresIn=900  # 15 minutes
    )

    return {"download_url": url, "expires_in": 900, "filename": record.title}

@app.post("/prescriptions/create")
def create_prescription(
    request: PrescriptionCreate,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """Doctor creates digital prescription."""
    import json as jsonlib

    prescription = Prescription(
        patient_id=request.patient_id,
        doctor_id=user.get("sub"),
        doctor_name=user.get("name", "Doctor"),
        appointment_id=request.appointment_id,
        diagnosis=request.diagnosis,
        medicines=jsonlib.dumps(request.medicines),
        instructions=request.instructions
    )
    db.add(prescription)
    db.commit()
    db.refresh(prescription)

    return prescription

@app.get("/prescriptions/patient/{patient_id}")
def get_patient_prescriptions(
    patient_id: str,
    user: dict  = Depends(any_user),
    db: Session = Depends(get_db)
):
    """Get all prescriptions for a patient."""
    groups = user.get("cognito:groups", [])
    if "patients" in groups and user.get("sub") != patient_id:
        raise HTTPException(status_code=403, detail="Access denied")

    prescriptions = db.query(Prescription).filter(
        Prescription.patient_id == patient_id
    ).order_by(Prescription.created_at.desc()).all()

    # Parse medicines JSON for each prescription
    import json as jsonlib
    result = []
    for p in prescriptions:
        p_dict = {c.name: getattr(p, c.name) for c in p.__table__.columns}
        p_dict["medicines"] = jsonlib.loads(p.medicines)
        result.append(p_dict)

    return result

@app.get("/prescriptions/{prescription_id}/history")
def get_patient_history(
    prescription_id: str,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """Doctor — one click access to patient visit history."""
    prescription = db.query(Prescription).filter(Prescription.id == prescription_id).first()
    if not prescription:
        raise HTTPException(status_code=404, detail="Not found")

    history = db.query(Prescription).filter(
        Prescription.patient_id == prescription.patient_id
    ).order_by(Prescription.created_at.desc()).limit(20).all()

    return {"patient_id": prescription.patient_id, "visit_count": len(history), "history": history}

