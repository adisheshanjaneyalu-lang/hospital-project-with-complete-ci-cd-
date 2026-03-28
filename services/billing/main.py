# ==============================================================
# billing-service/main.py
# Handles: Razorpay payments, insurance split, TPA/IPD billing
# ==============================================================
import uuid
import os
import razorpay
import json
import hmac
import hashlib
import boto3
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Integer, Float, Text, Boolean
import logging

from shared.database import Base, engine, get_db, get_secret
from shared.auth import get_current_user, patient_only, staff_only, admin_only, any_user

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Shivam Hospital — Billing Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Razorpay Client ───────────────────────────────────────────
def get_razorpay_client():
    secret    = get_secret("shivam-hospital/production/razorpay")
    return razorpay.Client(auth=(secret["key_id"], secret["key_secret"]))

def get_razorpay_key_id():
    secret = get_secret("shivam-hospital/production/razorpay")
    return secret["key_id"]

# ── Models ────────────────────────────────────────────────────
class Bill(Base):
    __tablename__ = "bills"
    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id          = Column(String, nullable=False, index=True)
    patient_name        = Column(String, nullable=False)
    appointment_id      = Column(String, nullable=True)
    bill_type           = Column(String, default="OPD")  # OPD/IPD/pharmacy/lab
    total_amount        = Column(Float, nullable=False)
    insurance_cover     = Column(Float, default=0.0)
    patient_copay       = Column(Float, nullable=False)
    insurance_id        = Column(String, nullable=True)
    tpa_name            = Column(String, nullable=True)
    status              = Column(String, default="pending")  # pending/paid/partially_paid/insurance_claimed
    razorpay_order_id   = Column(String, nullable=True)
    razorpay_payment_id = Column(String, nullable=True)
    items               = Column(Text, nullable=False)   # JSON list of line items
    created_at          = Column(DateTime, default=datetime.utcnow)
    paid_at             = Column(DateTime, nullable=True)

class InsuranceCard(Base):
    __tablename__ = "insurance_cards"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id      = Column(String, nullable=False, index=True)
    insurance_name  = Column(String, nullable=False)
    policy_number   = Column(String, nullable=False)
    tpa_name        = Column(String, nullable=True)
    sum_insured     = Column(Float, nullable=False)
    valid_till      = Column(String, nullable=False)
    is_cashless     = Column(Boolean, default=True)
    s3_key          = Column(String, nullable=True)  # card image in S3
    created_at      = Column(DateTime, default=datetime.utcnow)

class TPAClaim(Base):
    __tablename__ = "tpa_claims"
    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    patient_id      = Column(String, nullable=False)
    bill_id         = Column(String, nullable=False)
    claim_amount    = Column(Float, nullable=False)
    tpa_name        = Column(String, nullable=False)
    claim_type      = Column(String, default="pre-auth")  # pre-auth/final-settlement
    status          = Column(String, default="submitted")  # submitted/approved/rejected/settled
    documents_s3    = Column(Text, nullable=True)  # JSON list of S3 keys
    remarks         = Column(Text, nullable=True)
    submitted_at    = Column(DateTime, default=datetime.utcnow)
    settled_at      = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)

# ── Schemas ───────────────────────────────────────────────────
class CreateBillRequest(BaseModel):
    patient_id:     str
    patient_name:   str
    appointment_id: Optional[str]
    bill_type:      str = "OPD"
    items:          list  # [{"name": "Consultation", "amount": 500}, ...]
    insurance_id:   Optional[str]

class PaymentVerifyRequest(BaseModel):
    razorpay_order_id:   str
    razorpay_payment_id: str
    razorpay_signature:  str

class TPAClaimRequest(BaseModel):
    bill_id:      str
    tpa_name:     str
    claim_type:   str = "pre-auth"
    document_keys:list = []

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status": "healthy", "service": "billing-service"}

@app.post("/bills/create")
def create_bill(
    request: CreateBillRequest,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """
    Admin creates bill.
    Auto-splits between insurance cover and patient co-pay.
    """
    # Calculate total
    total = sum(item.get("amount", 0) for item in request.items)

    # Check insurance if provided
    insurance_cover = 0.0
    tpa_name        = None

    if request.insurance_id:
        card = db.query(InsuranceCard).filter(
            InsuranceCard.id == request.insurance_id,
            InsuranceCard.patient_id == request.patient_id
        ).first()

        if card and card.is_cashless:
            # Insurance covers up to sum_insured
            insurance_cover = min(total * 0.8, card.sum_insured)  # insurance covers 80%
            tpa_name        = card.tpa_name

    patient_copay = total - insurance_cover

    bill = Bill(
        patient_id=request.patient_id,
        patient_name=request.patient_name,
        appointment_id=request.appointment_id,
        bill_type=request.bill_type,
        total_amount=total,
        insurance_cover=insurance_cover,
        patient_copay=patient_copay,
        insurance_id=request.insurance_id,
        tpa_name=tpa_name,
        items=json.dumps(request.items)
    )
    db.add(bill)
    db.commit()
    db.refresh(bill)

    return {
        "bill_id":        bill.id,
        "total_amount":   total,
        "insurance_cover":insurance_cover,
        "patient_copay":  patient_copay,
        "tpa_name":       tpa_name,
        "items":          request.items
    }

@app.post("/bills/{bill_id}/create-payment-order")
def create_payment_order(bill_id: str, user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Create Razorpay order for patient co-pay amount."""
    bill = db.query(Bill).filter(Bill.id == bill_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    if bill.status == "paid":
        raise HTTPException(status_code=400, detail="Bill already paid")

    rzp = get_razorpay_client()

    # Amount in paise (Razorpay uses smallest currency unit)
    amount_paise = int(bill.patient_copay * 100)

    order = rzp.order.create({
        "amount":   amount_paise,
        "currency": "INR",
        "receipt":  bill_id,
        "notes": {
            "bill_id":    bill_id,
            "patient_id": bill.patient_id,
            "hospital":   "Shivam Hospital"
        }
    })

    # Save order ID
    bill.razorpay_order_id = order["id"]
    db.commit()

    return {
        "order_id":     order["id"],
        "amount":       bill.patient_copay,
        "amount_paise": amount_paise,
        "currency":     "INR",
        "key_id":       get_razorpay_key_id(),  # frontend needs this
        "bill_id":      bill_id
    }

@app.post("/bills/verify-payment")
def verify_payment(request: PaymentVerifyRequest, db: Session = Depends(get_db)):
    """
    Verify Razorpay payment signature.
    This is critical — always verify signature to prevent fraud.
    """
    # Get Razorpay secret
    secret    = get_secret("shivam-hospital/production/razorpay")
    key_secret= secret["key_secret"]

    # Verify signature
    generated_signature = hmac.new(
        key_secret.encode("utf-8"),
        f"{request.razorpay_order_id}|{request.razorpay_payment_id}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if generated_signature != request.razorpay_signature:
        raise HTTPException(status_code=400, detail="Payment signature invalid — possible fraud attempt")

    # Mark bill as paid
    bill = db.query(Bill).filter(Bill.razorpay_order_id == request.razorpay_order_id).first()
    if bill:
        bill.status              = "paid"
        bill.razorpay_payment_id = request.razorpay_payment_id
        bill.paid_at             = datetime.utcnow()
        db.commit()

        # Send payment confirmation via SNS
        try:
            sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "eu-north-1"))
            sns.publish(
                TopicArn=os.getenv("BILLING_TOPIC_ARN",""),
                Message=json.dumps({
                    "event":      "payment_received",
                    "bill_id":    bill.id,
                    "patient_id": bill.patient_id,
                    "amount":     bill.patient_copay,
                    "payment_id": request.razorpay_payment_id
                })
            )
        except Exception as e:
            logger.error(f"SNS failed: {e}")

    return {"message": "Payment verified successfully", "bill_id": bill.id if bill else None}

@app.post("/insurance/upload-card")
def upload_insurance_card(
    patient_id:     str,
    insurance_name: str,
    policy_number:  str,
    tpa_name:       Optional[str] = None,
    sum_insured:    float = 0.0,
    valid_till:     str = "",
    user: dict = Depends(any_user),
    db: Session = Depends(get_db)
):
    """Patient uploads insurance card details."""
    card = InsuranceCard(
        patient_id=patient_id,
        insurance_name=insurance_name,
        policy_number=policy_number,
        tpa_name=tpa_name,
        sum_insured=sum_insured,
        valid_till=valid_till,
        is_cashless=True
    )
    db.add(card)
    db.commit()
    return {"message": "Insurance card added", "card_id": card.id}

@app.get("/insurance/{patient_id}/eligibility")
def check_cashless_eligibility(patient_id: str, user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Check if patient is eligible for cashless treatment."""
    cards = db.query(InsuranceCard).filter(InsuranceCard.patient_id == patient_id).all()
    if not cards:
        return {"eligible": False, "message": "No insurance card on file"}

    # Check validity
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    valid_cards = [c for c in cards if c.valid_till >= today]

    if not valid_cards:
        return {"eligible": False, "message": "All insurance cards expired"}

    card = valid_cards[0]
    return {
        "eligible":      card.is_cashless,
        "insurance_name":card.insurance_name,
        "tpa_name":      card.tpa_name,
        "sum_insured":   card.sum_insured,
        "valid_till":    card.valid_till,
        "card_id":       card.id
    }

@app.post("/tpa/submit-claim")
def submit_tpa_claim(request: TPAClaimRequest, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Admin submits TPA pre-auth or final settlement claim."""
    bill = db.query(Bill).filter(Bill.id == request.bill_id).first()
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    claim = TPAClaim(
        patient_id=bill.patient_id,
        bill_id=request.bill_id,
        claim_amount=bill.insurance_cover,
        tpa_name=request.tpa_name,
        claim_type=request.claim_type,
        documents_s3=json.dumps(request.document_keys)
    )
    db.add(claim)
    db.commit()

    return {"message": "TPA claim submitted", "claim_id": claim.id, "status": "submitted"}

@app.get("/tpa/claims/{patient_id}")
def get_tpa_claims(patient_id: str, user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Get all TPA claims for a patient — live claim status tracker."""
    return db.query(TPAClaim).filter(TPAClaim.patient_id == patient_id).all()

@app.get("/bills/patient/{patient_id}")
def get_patient_bills(patient_id: str, user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Get all bills for a patient."""
    bills = db.query(Bill).filter(Bill.patient_id == patient_id).order_by(Bill.created_at.desc()).all()
    result = []
    for b in bills:
        b_dict = {c.name: getattr(b, c.name) for c in b.__table__.columns}
        b_dict["items"] = json.loads(b.items)
        result.append(b_dict)
    return result

