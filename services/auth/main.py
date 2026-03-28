# ==============================================================
# auth-service/main.py
# Handles: OTP login, registration, token refresh, user roles
# ==============================================================
import boto3
import json
import os
import random
import string
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Boolean
import logging

from shared.database import Base, engine, get_db, redis_client, get_secret
from shared.auth import get_current_user, admin_only

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Shivam Hospital — Auth Service",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database Models ───────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    id           = Column(String, primary_key=True)
    phone        = Column(String, unique=True, index=True)
    email        = Column(String, unique=True, index=True, nullable=True)
    name         = Column(String)
    role         = Column(String)          # patient / doctor / admin
    hospital_id  = Column(String)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    last_login   = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)

# ── Schemas ───────────────────────────────────────────────────
class SendOTPRequest(BaseModel):
    phone: str

    @validator("phone")
    def validate_phone(cls, v):
        if not v.startswith("+"):
            raise ValueError("Phone must include country code e.g. +919876543210")
        if len(v) < 10:
            raise ValueError("Invalid phone number")
        return v

class VerifyOTPRequest(BaseModel):
    phone:    str
    otp:      str
    name:     str = None
    role:     str = "patients"

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class RegisterDoctorRequest(BaseModel):
    email:       str
    name:        str
    speciality:  str
    hospital_id: str

# ── Cognito Client ────────────────────────────────────────────
def get_cognito_client():
    return boto3.client("cognito-idp", region_name=os.getenv("AWS_REGION", "eu-north-1"))

def get_user_pool_id():
    secret = get_secret("shivam-hospital/production/cognito")
    return secret["user_pool_id"]

def get_client_id():
    secret = get_secret("shivam-hospital/production/cognito")
    return secret["client_id"]

# ── Helper: Generate OTP ─────────────────────────────────────
def generate_otp(length=6) -> str:
    return "".join(random.choices(string.digits, k=length))

# ── Routes ────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    """Route53 health check endpoint."""
    return {"status": "healthy", "service": "auth-service", "timestamp": datetime.utcnow()}

@app.post("/auth/send-otp")
def send_otp(request: SendOTPRequest):
    """
    Patient login Step 1 — Send OTP to mobile.
    OTP stored in Redis with 5-minute expiry.
    SMS sent via AWS SNS.
    """
    otp     = generate_otp()
    otp_key = f"otp:{request.phone}"

    # Store OTP in Redis — expires in 5 minutes
    redis_client.setex(otp_key, 300, otp)

    # Send SMS via AWS SNS
    try:
        sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "eu-north-1"))
        sns.publish(
            PhoneNumber=request.phone,
            Message=f"Your Shivam Hospital OTP is: {otp}. Valid for 5 minutes.",
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {
                    "DataType": "String",
                    "StringValue": "Transactional"
                }
            }
        )
    except Exception as e:
        logger.error(f"SMS failed: {e}")
        # In dev mode, log OTP to console
        if os.getenv("ENV") == "local":
            logger.info(f"DEV OTP for {request.phone}: {otp}")

    return {"message": "OTP sent successfully", "expires_in": 300}

@app.post("/auth/verify-otp")
def verify_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    """
    Patient login Step 2 — Verify OTP.
    Returns JWT access token + refresh token from Cognito.
    """
    otp_key      = f"otp:{request.phone}"
    stored_otp   = redis_client.get(otp_key)

    if not stored_otp:
        raise HTTPException(status_code=400, detail="OTP expired. Please request a new one.")

    if stored_otp != request.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP.")

    # OTP correct — delete from Redis (one-time use)
    redis_client.delete(otp_key)

    cognito = get_cognito_client()
    pool_id = get_user_pool_id()

    # Check if user exists in Cognito
    try:
        cognito.admin_get_user(UserPoolId=pool_id, Username=request.phone)
        user_exists = True
    except cognito.exceptions.UserNotFoundException:
        user_exists = False

    # Register new user if first time
    if not user_exists:
        if not request.name:
            raise HTTPException(status_code=400, detail="Name required for first-time registration.")

        import uuid
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=request.phone,
            UserAttributes=[
                {"Name": "phone_number",      "Value": request.phone},
                {"Name": "phone_number_verified", "Value": "true"},
                {"Name": "name",              "Value": request.name},
                {"Name": "custom:role",       "Value": request.role},
                {"Name": "custom:hospital_id","Value": "SHIVAM-001"},
            ],
            MessageAction="SUPPRESS",
            TemporaryPassword="TempPass@123!"
        )

        # Set permanent password immediately
        cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=request.phone,
            Password="TempPass@123!",
            Permanent=True
        )

        # Add to group
        cognito.admin_add_user_to_group(
            UserPoolId=pool_id,
            Username=request.phone,
            GroupName=request.role
        )

        # Save to RDS
        new_user = User(
            id=str(uuid.uuid4()),
            phone=request.phone,
            name=request.name,
            role=request.role,
            hospital_id="SHIVAM-001"
        )
        db.add(new_user)
        db.commit()

    # Get tokens from Cognito
    tokens = cognito.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=get_client_id(),
        AuthFlow="ADMIN_NO_SRP_AUTH",
        AuthParameters={
            "USERNAME": request.phone,
            "PASSWORD": "TempPass@123!"
        }
    )

    # Update last login
    db.query(User).filter(User.phone == request.phone).update(
        {"last_login": datetime.utcnow()}
    )
    db.commit()

    return {
        "access_token":  tokens["AuthenticationResult"]["AccessToken"],
        "refresh_token": tokens["AuthenticationResult"]["RefreshToken"],
        "id_token":      tokens["AuthenticationResult"]["IdToken"],
        "expires_in":    tokens["AuthenticationResult"]["ExpiresIn"],
        "token_type":    "Bearer"
    }

@app.post("/auth/refresh")
def refresh_token(request: RefreshTokenRequest):
    """Refresh expired access token using refresh token."""
    cognito = get_cognito_client()
    try:
        result = cognito.initiate_auth(
            ClientId=get_client_id(),
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": request.refresh_token}
        )
        return {
            "access_token": result["AuthenticationResult"]["AccessToken"],
            "expires_in":   result["AuthenticationResult"]["ExpiresIn"]
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

@app.post("/auth/logout")
def logout(user: dict = Depends(get_current_user)):
    """Invalidate user session in Cognito."""
    cognito = get_cognito_client()
    try:
        cognito.global_sign_out(AccessToken=user.get("token"))
    except Exception:
        pass
    return {"message": "Logged out successfully"}

@app.get("/auth/me")
def get_me(user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get current user profile."""
    phone   = user.get("phone_number")
    db_user = db.query(User).filter(User.phone == phone).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id":         db_user.id,
        "name":       db_user.name,
        "phone":      db_user.phone,
        "email":      db_user.email,
        "role":       db_user.role,
        "is_active":  db_user.is_active,
        "last_login": db_user.last_login
    }

@app.post("/auth/register-doctor")
def register_doctor(request: RegisterDoctorRequest, admin=Depends(admin_only), db: Session = Depends(get_db)):
    """Admin only — Register a new doctor."""
    import uuid
    cognito = get_cognito_client()
    pool_id = get_user_pool_id()

    cognito.admin_create_user(
        UserPoolId=pool_id,
        Username=request.email,
        UserAttributes=[
            {"Name": "email",                "Value": request.email},
            {"Name": "email_verified",       "Value": "true"},
            {"Name": "name",                 "Value": request.name},
            {"Name": "custom:role",          "Value": "doctors"},
            {"Name": "custom:hospital_id",   "Value": request.hospital_id},
        ]
    )

    cognito.admin_add_user_to_group(
        UserPoolId=pool_id,
        Username=request.email,
        GroupName="doctors"
    )

    doctor = User(
        id=str(uuid.uuid4()),
        email=request.email,
        name=request.name,
        role="doctors",
        hospital_id=request.hospital_id
    )
    db.add(doctor)
    db.commit()

    return {"message": f"Doctor {request.name} registered successfully"}

