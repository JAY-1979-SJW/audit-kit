from app.core.db import SessionLocal
from app.core.router import router
from app.services.quantity import unit_price


@router.get("/walls/area")
def wall_area(width: float, height: float, count: int) -> dict:
    db = SessionLocal()
    total = 0.0
    for _ in range(count):
        total += width * height
    db.add({"area": total})
    db.commit()
    return {"area": total, "price": total * unit_price()}


@router.get("/health")
def health() -> dict:
    return {"ok": True}
