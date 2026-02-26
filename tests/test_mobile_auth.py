from __future__ import annotations

from datetime import timedelta

from app.core.timezone import now_utc
from app.domain.models import User
from app.infra.repos import MobileRepo
from app.services.mobile_auth_service import MobileAuthService


def test_pair_issue_and_refresh_tokens(db_session):
    now = now_utc()
    user = User(
        wecom_user_id="wangwu",
        timezone="Asia/Shanghai",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()

    mobile_repo = MobileRepo(db_session)
    mobile_repo.create_pair_code(user.id, "ABC123", now + timedelta(minutes=5))
    claimed = mobile_repo.claim_pair_code("ABC123", "iphone-001")
    assert claimed is not None

    auth_service = MobileAuthService()
    tokens = auth_service.issue_tokens(db_session, user_id=user.id, device_id="iphone-001")
    parsed = auth_service.verify_access_token(tokens.access_token)
    assert int(parsed.sub) == user.id

    refreshed = auth_service.refresh_tokens(db_session, tokens.refresh_token)
    assert refreshed.access_token
    assert refreshed.refresh_token

