# ==============================================================
# inventory-service/main.py
# Handles: pharmacy stock, bed tracker (ICU/Ward), surgical kits
# ==============================================================
import uuid
import os
import json
import boto3
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, String, DateTime, Integer, Boolean, Float, Text
import logging

from shared.database import Base, engine, get_db, redis_client
from shared.auth import staff_only, admin_only, any_user

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Shivam Hospital — Inventory Service", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

REGION    = os.getenv("AWS_REGION", "eu-north-1")
LOW_STOCK = int(os.getenv("LOW_STOCK_THRESHOLD", "10"))

# ── Models ────────────────────────────────────────────────────
class InventoryItem(Base):
    __tablename__ = "inventory_items"
    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String, nullable=False, index=True)
    category      = Column(String, nullable=False)  # medicine/surgical/equipment
    quantity      = Column(Integer, default=0)
    unit          = Column(String, default="units")  # tablets/ml/units
    reorder_level = Column(Integer, default=10)
    unit_price    = Column(Float, default=0.0)
    supplier      = Column(String, nullable=True)
    expiry_date   = Column(String, nullable=True)
    location      = Column(String, nullable=True)   # pharmacy/ICU/OT
    is_active     = Column(Boolean, default=True)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at    = Column(DateTime, default=datetime.utcnow)

class Bed(Base):
    __tablename__ = "beds"
    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    bed_number    = Column(String, nullable=False, unique=True)
    ward_type     = Column(String, nullable=False)  # ICU/General/Private/Semi-Private/Emergency
    floor         = Column(Integer, default=1)
    is_occupied   = Column(Boolean, default=False)
    patient_id    = Column(String, nullable=True)
    patient_name  = Column(String, nullable=True)
    admitted_at   = Column(DateTime, nullable=True)
    notes         = Column(Text, nullable=True)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"
    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    item_id       = Column(String, nullable=False, index=True)
    item_name     = Column(String, nullable=False)
    transaction   = Column(String, nullable=False)  # add/use/return/expire
    quantity      = Column(Integer, nullable=False)
    performed_by  = Column(String, nullable=False)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── Seed Beds on startup ──────────────────────────────────────
def seed_beds(db: Session):
    if db.query(Bed).count() == 0:
        beds = []
        # ICU — 10 beds
        for i in range(1, 11):
            beds.append(Bed(bed_number=f"ICU-{i:02d}", ward_type="ICU", floor=3))
        # General Ward — 30 beds
        for i in range(1, 31):
            beds.append(Bed(bed_number=f"GEN-{i:02d}", ward_type="General", floor=2))
        # Private — 10 beds
        for i in range(1, 11):
            beds.append(Bed(bed_number=f"PVT-{i:02d}", ward_type="Private", floor=4))
        # Emergency — 5 beds
        for i in range(1, 6):
            beds.append(Bed(bed_number=f"EMR-{i:02d}", ward_type="Emergency", floor=1))
        db.bulk_save_objects(beds)
        db.commit()
        logger.info(f"Seeded {len(beds)} beds")

# ── Schemas ───────────────────────────────────────────────────
class AddInventoryRequest(BaseModel):
    name:          str
    category:      str
    quantity:      int
    unit:          str = "units"
    reorder_level: int = 10
    unit_price:    float = 0.0
    supplier:      Optional[str]
    expiry_date:   Optional[str]
    location:      Optional[str]

class UpdateStockRequest(BaseModel):
    item_id:     str
    quantity:    int       # positive = add, negative = use
    transaction: str       # add/use/return/expire
    notes:       Optional[str]

class AdmitPatientRequest(BaseModel):
    bed_id:      str
    patient_id:  str
    patient_name:str
    notes:       Optional[str]

class DischargePatientRequest(BaseModel):
    bed_id: str

# ── Helper: Send low stock alert ─────────────────────────────
def send_low_stock_alert(item_name: str, quantity: int):
    try:
        sns = boto3.client("sns", region_name=REGION)
        sns.publish(
            TopicArn=os.getenv("INVENTORY_TOPIC_ARN", ""),
            Subject=f"⚠️ LOW STOCK: {item_name}",
            Message=f"LOW STOCK ALERT\nItem: {item_name}\nCurrent Stock: {quantity} units\nPlease reorder immediately."
        )
    except Exception as e:
        logger.error(f"Alert failed: {e}")

# ── Routes ────────────────────────────────────────────────────
@app.get("/health")
def health(): return {"status": "healthy", "service": "inventory-service"}

# ── PHARMACY INVENTORY ────────────────────────────────────────
@app.post("/inventory/add")
def add_inventory(request: AddInventoryRequest, user: dict = Depends(admin_only), db: Session = Depends(get_db)):
    """Admin adds new item to inventory."""
    item = InventoryItem(**request.dict())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item

@app.get("/inventory")
def get_inventory(
    category: Optional[str] = None,
    low_stock: bool = False,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """Get all inventory items."""
    query = db.query(InventoryItem).filter(InventoryItem.is_active == True)
    if category:
        query = query.filter(InventoryItem.category == category)
    if low_stock:
        # Items below reorder level
        from sqlalchemy import text
        query = query.filter(InventoryItem.quantity <= InventoryItem.reorder_level)

    return query.order_by(InventoryItem.name).all()

@app.patch("/inventory/update-stock")
def update_stock(
    request: UpdateStockRequest,
    user: dict = Depends(staff_only),
    db: Session = Depends(get_db)
):
    """
    Update stock quantity.
    Positive quantity = add stock
    Negative quantity = use stock
    """
    item = db.query(InventoryItem).filter(InventoryItem.id == request.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    new_qty = item.quantity + request.quantity
    if new_qty < 0:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient stock. Available: {item.quantity}, Requested: {abs(request.quantity)}"
        )

    item.quantity  = new_qty
    item.updated_at= datetime.utcnow()
    db.commit()

    # Log transaction
    txn = InventoryTransaction(
        item_id=request.item_id,
        item_name=item.name,
        transaction=request.transaction,
        quantity=request.quantity,
        performed_by=user.get("sub", "unknown"),
        notes=request.notes
    )
    db.add(txn)
    db.commit()

    # Alert if below reorder level
    if new_qty <= item.reorder_level:
        send_low_stock_alert(item.name, new_qty)

    return {
        "item_name":   item.name,
        "old_quantity":item.quantity - request.quantity,
        "new_quantity":new_qty,
        "low_stock":   new_qty <= item.reorder_level
    }

@app.get("/inventory/low-stock-alerts")
def get_low_stock_items(user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Get all items below reorder level."""
    from sqlalchemy import text
    items = db.query(InventoryItem).filter(
        InventoryItem.is_active == True,
        InventoryItem.quantity <= InventoryItem.reorder_level
    ).all()

    return {
        "alert_count": len(items),
        "items": [
            {
                "id":            i.id,
                "name":          i.name,
                "category":      i.category,
                "quantity":      i.quantity,
                "reorder_level": i.reorder_level,
                "shortage":      i.reorder_level - i.quantity,
                "location":      i.location
            }
            for i in items
        ]
    }

# ── BED TRACKER ───────────────────────────────────────────────
@app.get("/beds")
def get_bed_map(ward_type: Optional[str] = None, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """
    Visual bed map — shows all beds with occupied/available status.
    Cached in Redis, refreshed every 30 seconds.
    """
    cache_key = f"bed_map:{ward_type or 'all'}"
    cached    = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Seed beds if empty
    seed_beds(db)

    query = db.query(Bed)
    if ward_type:
        query = query.filter(Bed.ward_type == ward_type)

    beds = query.order_by(Bed.ward_type, Bed.bed_number).all()

    # Group by ward type
    ward_map = {}
    for bed in beds:
        if bed.ward_type not in ward_map:
            ward_map[bed.ward_type] = {"total": 0, "occupied": 0, "available": 0, "beds": []}
        ward_map[bed.ward_type]["total"] += 1
        if bed.is_occupied:
            ward_map[bed.ward_type]["occupied"] += 1
        else:
            ward_map[bed.ward_type]["available"] += 1
        ward_map[bed.ward_type]["beds"].append({
            "id":          bed.id,
            "bed_number":  bed.bed_number,
            "ward_type":   bed.ward_type,
            "is_occupied": bed.is_occupied,
            "patient_name":bed.patient_name,
            "admitted_at": str(bed.admitted_at) if bed.admitted_at else None
        })

    result = {"ward_summary": ward_map, "timestamp": datetime.utcnow().isoformat()}

    # Cache for 30 seconds
    redis_client.setex(cache_key, 30, json.dumps(result))
    return result

@app.post("/beds/admit")
def admit_patient(request: AdmitPatientRequest, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Admit patient to a bed."""
    bed = db.query(Bed).filter(Bed.id == request.bed_id).first()
    if not bed:
        raise HTTPException(status_code=404, detail="Bed not found")
    if bed.is_occupied:
        raise HTTPException(status_code=409, detail=f"Bed {bed.bed_number} is already occupied")

    bed.is_occupied  = True
    bed.patient_id   = request.patient_id
    bed.patient_name = request.patient_name
    bed.admitted_at  = datetime.utcnow()
    bed.notes        = request.notes
    db.commit()

    # Invalidate Redis cache
    redis_client.delete("bed_map:all")
    redis_client.delete(f"bed_map:{bed.ward_type}")

    return {"message": f"Patient admitted to bed {bed.bed_number}", "bed_number": bed.bed_number}

@app.post("/beds/discharge")
def discharge_patient(request: DischargePatientRequest, user: dict = Depends(staff_only), db: Session = Depends(get_db)):
    """Discharge patient — free the bed."""
    bed = db.query(Bed).filter(Bed.id == request.bed_id).first()
    if not bed:
        raise HTTPException(status_code=404, detail="Bed not found")

    patient_name    = bed.patient_name
    bed.is_occupied = False
    bed.patient_id  = None
    bed.patient_name= None
    bed.admitted_at = None
    bed.notes       = None
    db.commit()

    # Invalidate Redis cache
    redis_client.delete("bed_map:all")
    redis_client.delete(f"bed_map:{bed.ward_type}")

    return {"message": f"{patient_name} discharged from bed {bed.bed_number}"}

@app.get("/beds/availability-summary")
def get_availability_summary(user: dict = Depends(any_user), db: Session = Depends(get_db)):
    """Quick summary for dashboard — total/occupied/available per ward."""
    seed_beds(db)
    beds = db.query(Bed).all()

    summary = {}
    for bed in beds:
        if bed.ward_type not in summary:
            summary[bed.ward_type] = {"total": 0, "occupied": 0, "available": 0}
        summary[bed.ward_type]["total"] += 1
        if bed.is_occupied:
            summary[bed.ward_type]["occupied"] += 1
        else:
            summary[bed.ward_type]["available"] += 1

    return summary

