from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.domain.schemas import DevUserListResponse, TokenResponse
from app.infra.db import get_db
from app.infra.repos import UserRepo
from app.services.mobile_auth_service import MobileAuthService


router = APIRouter(prefix="/api/v1/dev")


def _is_localhost(host: str) -> bool:
    if not host:
        return False
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@router.post("/token", response_model=TokenResponse)
def issue_dev_token(
    request: Request,
    device_id: str = Query(default="local-dev"),
    wecom_user_id: str = Query(default="local-dev"),
    db: Session = Depends(get_db),
    auth_service: MobileAuthService = Depends(lambda: MobileAuthService()),
) -> TokenResponse:
    host = request.client.host if request.client else ""
    if not _is_localhost(host):
        raise HTTPException(status_code=403, detail="dev token endpoint is localhost-only")
    user = UserRepo(db).get_or_create(wecom_user_id, timezone_name="Asia/Shanghai")
    return auth_service.issue_tokens(db, user_id=user.id, device_id=device_id)


@router.get("/users", response_model=DevUserListResponse)
def list_dev_users(
    request: Request,
    limit: int = Query(default=50, ge=1, le=300),
    db: Session = Depends(get_db),
) -> DevUserListResponse:
    host = request.client.host if request.client else ""
    if not _is_localhost(host):
        raise HTTPException(status_code=403, detail="dev users endpoint is localhost-only")
    users = UserRepo(db).list_wecom_ids(limit=limit)
    return DevUserListResponse(users=users)
