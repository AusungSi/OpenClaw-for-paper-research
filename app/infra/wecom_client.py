from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import mimetypes

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger


logger = get_logger("wecom")


class WeComClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._last_send_ok: bool = True
        self._last_send_error: str | None = None

    def _token_valid(self) -> bool:
        if not self._access_token or not self._expires_at:
            return False
        return self._expires_at > datetime.now(timezone.utc) + timedelta(seconds=30)

    def _get_access_token(self) -> str:
        if self._token_valid():
            return self._access_token or ""

        if not self.settings.wecom_corp_id or not self.settings.wecom_secret:
            raise RuntimeError("WeCom corp id or secret is not configured")

        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        params = {"corpid": self.settings.wecom_corp_id, "corpsecret": self.settings.wecom_secret}
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()

        if payload.get("errcode") != 0:
            raise RuntimeError(f"Get WeCom access token failed: {payload}")

        expires_in = int(payload.get("expires_in", 7200))
        self._access_token = str(payload["access_token"])
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in - 60))
        return self._access_token

    def send_text(self, user_id: str, content: str) -> tuple[bool, str | None]:
        if not user_id:
            return self._mark_send_failed("config", "missing_user_id", "user id is empty")
        try:
            token = self._get_access_token()
        except Exception as exc:
            return self._mark_send_failed("config", "token_error", str(exc))

        url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self.settings.wecom_agent_id,
            "text": {"content": content},
            "safe": 0,
        }
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(url, params={"access_token": token}, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            return self._mark_send_failed("network", "timeout", str(exc))
        except httpx.HTTPError as exc:
            return self._mark_send_failed("network", "http_error", str(exc))
        except Exception as exc:
            return self._mark_send_failed("unknown", "unexpected", str(exc))

        if data.get("errcode") != 0:
            errcode = str(data.get("errcode", "unknown"))
            errmsg = str(data.get("errmsg", "unknown"))
            return self._mark_send_failed("external", f"wecom_{errcode}", errmsg)
        self._mark_send_ok()
        return True, None

    def send_file(self, user_id: str, file_path: str) -> tuple[bool, str | None]:
        if not user_id:
            return self._mark_send_failed("config", "missing_user_id", "user id is empty")
        path = Path(file_path).expanduser()
        if not path.exists() or not path.is_file():
            return self._mark_send_failed("config", "file_not_found", f"file not found: {path}")
        try:
            token = self._get_access_token()
        except Exception as exc:
            return self._mark_send_failed("config", "token_error", str(exc))

        try:
            media_id = self._upload_file(token=token, path=path)
        except Exception as exc:
            return self._mark_send_failed("network", "upload_error", str(exc))

        url = "https://qyapi.weixin.qq.com/cgi-bin/message/send"
        payload = {
            "touser": user_id,
            "msgtype": "file",
            "agentid": self.settings.wecom_agent_id,
            "file": {"media_id": media_id},
            "safe": 0,
        }
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(url, params={"access_token": token}, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            return self._mark_send_failed("network", "timeout", str(exc))
        except httpx.HTTPError as exc:
            return self._mark_send_failed("network", "http_error", str(exc))
        except Exception as exc:
            return self._mark_send_failed("unknown", "unexpected", str(exc))
        if data.get("errcode") != 0:
            errcode = str(data.get("errcode", "unknown"))
            errmsg = str(data.get("errmsg", "unknown"))
            return self._mark_send_failed("external", f"wecom_{errcode}", errmsg)
        self._mark_send_ok()
        return True, None

    def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        if not media_id:
            raise RuntimeError("media_id is empty")
        token = self._get_access_token()
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/get"
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(url, params={"access_token": token, "media_id": media_id})
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"download_media_timeout:{exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"download_media_http_error:{exc}") from exc

        content_type = resp.headers.get("content-type")
        if "application/json" in (content_type or ""):
            payload = resp.json()
            raise RuntimeError(f"download_media_failed:{payload}")
        return resp.content, content_type

    def healthcheck(self) -> bool:
        try:
            self._get_access_token()
            return True
        except Exception:
            return False

    def last_send_status(self) -> tuple[bool, str | None]:
        return self._last_send_ok, self._last_send_error

    def _mark_send_ok(self) -> None:
        self._last_send_ok = True
        self._last_send_error = None

    def _mark_send_failed(self, category: str, code: str, message: str) -> tuple[bool, str]:
        error = f"{category}:{code}:{message}"
        self._last_send_ok = False
        self._last_send_error = error
        logger.warning(
            "wecom_send_failed category=%s code=%s message=%s",
            category,
            code,
            message,
        )
        return False, error

    def _upload_file(self, *, token: str, path: Path) -> str:
        url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload"
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fp:
            files = {"media": (path.name, fp, mime_type)}
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    url,
                    params={"access_token": token, "type": "file"},
                    files=files,
                )
                resp.raise_for_status()
                payload = resp.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"upload_failed:{payload.get('errcode')}:{payload.get('errmsg')}")
        media_id = payload.get("media_id")
        if not isinstance(media_id, str) or not media_id.strip():
            raise RuntimeError("upload_failed:missing_media_id")
        return media_id.strip()
