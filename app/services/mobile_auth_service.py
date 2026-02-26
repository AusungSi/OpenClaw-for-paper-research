from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.enums import TokenType
from app.domain.schemas import TokenPayload, TokenResponse
from app.infra.repos import RefreshTokenRepo


class MobileAuthService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def issue_tokens(self, db: Session, user_id: int, device_id: str) -> TokenResponse:
        now = datetime.now(timezone.utc)
        access_exp = now + timedelta(minutes=self.settings.access_token_minutes)
        refresh_exp = now + timedelta(days=self.settings.refresh_token_days)

        access_token = jwt.encode(
            {
                "sub": str(user_id),
                "token_type": TokenType.ACCESS.value,
                "device_id": device_id,
                "iat": int(now.timestamp()),
                "exp": int(access_exp.timestamp()),
            },
            self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
        )
        refresh_token = jwt.encode(
            {
                "sub": str(user_id),
                "token_type": TokenType.REFRESH.value,
                "device_id": device_id,
                "iat": int(now.timestamp()),
                "exp": int(refresh_exp.timestamp()),
                "nonce": secrets.token_hex(8),
            },
            self.settings.jwt_secret,
            algorithm=self.settings.jwt_algorithm,
        )

        refresh_repo = RefreshTokenRepo(db)
        refresh_repo.revoke_all_for_device(user_id, device_id)
        refresh_repo.create(user_id, device_id, refresh_token, refresh_exp)

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.settings.access_token_minutes * 60,
        )

    def verify_access_token(self, token: str) -> TokenPayload:
        payload = jwt.decode(
            token,
            self.settings.jwt_secret,
            algorithms=[self.settings.jwt_algorithm],
        )
        parsed = TokenPayload.model_validate(payload)
        if parsed.token_type != TokenType.ACCESS:
            raise ValueError("invalid token type")
        return parsed

    def refresh_tokens(self, db: Session, refresh_token: str) -> TokenResponse:
        payload = jwt.decode(
            refresh_token,
            self.settings.jwt_secret,
            algorithms=[self.settings.jwt_algorithm],
        )
        parsed = TokenPayload.model_validate(payload)
        if parsed.token_type != TokenType.REFRESH:
            raise ValueError("invalid token type")
        user_id = int(parsed.sub)
        refresh_repo = RefreshTokenRepo(db)
        if not refresh_repo.exists_active(user_id, parsed.device_id, refresh_token):
            raise ValueError("refresh token revoked or expired")
        return self.issue_tokens(db, user_id, parsed.device_id)

