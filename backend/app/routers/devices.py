"""/devices* routes: create, list, get, update, delete."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import DiagnosticSession, RobotModel, User, UserDevice
from ..schemas import DeviceCreate, DeviceRead, DeviceUpdate
from ..security import get_current_user
from ._shared import owned_device

router = APIRouter(prefix="/api/v1")

DEVICE_PAGE_SIZE = 100
MAX_DEVICE_PAGE_SIZE = 200


@router.post("/devices", response_model=DeviceRead, status_code=201)
def create_device(
    payload: DeviceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> UserDevice:
    robot_model = db.scalar(
        select(RobotModel).where(RobotModel.id == payload.robot_model_id, RobotModel.active.is_(True))
    )
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    device = UserDevice(user_id=user.id, **payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)
    device.robot_model = robot_model
    return device


@router.get("/devices", response_model=list[DeviceRead])
def list_devices(
    limit: int = Query(default=DEVICE_PAGE_SIZE, ge=1, le=MAX_DEVICE_PAGE_SIZE),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[UserDevice]:
    # 无 limit 的列表接口迟早会被一个绑了几千台设备的账号拖垮（体检 D5）
    stmt = (
        select(UserDevice)
        .options(selectinload(UserDevice.robot_model))
        .where(UserDevice.user_id == user.id)
        .order_by(UserDevice.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


@router.get("/devices/{device_id}", response_model=DeviceRead)
def get_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> UserDevice:
    return owned_device(db, device_id, user)


@router.patch("/devices/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: int,
    payload: DeviceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UserDevice:
    device = owned_device(db, device_id, user)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, key, value)
    db.commit()
    db.refresh(device)
    return device


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(
    device_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    device = owned_device(db, device_id, user)
    diagnostic_count = db.scalar(
        select(func.count()).select_from(DiagnosticSession).where(DiagnosticSession.device_id == device.id)
    )
    if diagnostic_count:
        raise HTTPException(status_code=409, detail="Device with diagnostic history cannot be deleted")
    db.delete(device)
    db.commit()
    return Response(status_code=204)
