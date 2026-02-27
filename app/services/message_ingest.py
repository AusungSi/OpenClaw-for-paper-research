from __future__ import annotations

from collections.abc import Callable
import secrets
from datetime import timedelta
from time import perf_counter

import orjson
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timezone import now_utc
from app.core.logging import get_logger
from app.domain.enums import OperationType, PendingActionStatus, ReminderSource, VoiceRecordStatus
from app.domain.models import User
from app.domain.schemas import IntentDraft
from app.infra.repos import InboundMessageRepo, MobileRepo, PendingActionRepo, UserRepo, VoiceRecordRepo
from app.infra.wecom_client import WeComClient
from app.services.asr_service import AsrError, AsrService
from app.services.confirm_service import ConfirmService
from app.services.intent_service import IntentService
from app.services.reply_generation_service import ReplyGenerationService
from app.services.reply_renderer import ReplyRenderer
from app.services.reminder_service import ReminderService


logger = get_logger("message_ingest")


class MessageIngestService:
    def __init__(
        self,
        intent_service: IntentService,
        confirm_service: ConfirmService,
        reminder_service: ReminderService,
        wecom_client: WeComClient,
        asr_service: AsrService | None = None,
        reply_generation_service: ReplyGenerationService | None = None,
        reply_renderer: ReplyRenderer | None = None,
    ) -> None:
        self.settings = get_settings()
        self.intent_service = intent_service
        self.confirm_service = confirm_service
        self.reminder_service = reminder_service
        self.wecom_client = wecom_client
        self.asr_service = asr_service
        self.reply_generation_service = reply_generation_service
        self.reply_renderer = reply_renderer or ReplyRenderer()
        self.dedup_duplicates = 0
        self.dedup_failures = 0

    @property
    def webhook_dedup_ok(self) -> bool:
        return self.dedup_failures == 0

    def process_text_message(
        self,
        db: Session,
        wecom_user_id: str,
        msg_id: str,
        raw_xml: str,
        text: str,
        reply_sink: Callable[[str], None] | None = None,
        message_source: ReminderSource = ReminderSource.WECHAT,
    ) -> None:
        user_repo = UserRepo(db)
        inbound_repo = InboundMessageRepo(db)
        user = user_repo.get_or_create(wecom_user_id, timezone_name=self.settings.default_timezone)

        try:
            is_new = inbound_repo.create_if_new(msg_id, user.id, "text", raw_xml, text.strip())
        except Exception:
            self.dedup_failures += 1
            logger.exception(
                "message_dedup_failed category=dedup msg_id=%s wecom_user_id=%s",
                msg_id,
                wecom_user_id,
            )
            self._send_user_text(wecom_user_id, self.reply_renderer.busy_fallback(), reply_sink=reply_sink)
            return
        if not is_new:
            self.dedup_duplicates += 1
            logger.info(
                "duplicate_message_ignored category=dedup msg_id=%s wecom_user_id=%s",
                msg_id,
                wecom_user_id,
            )
            return
        self._handle_normalized_message(
            db,
            user,
            wecom_user_id,
            msg_id,
            text.strip(),
            inbound_repo,
            reply_sink=reply_sink,
            message_source=message_source,
        )

    def process_voice_message(
        self,
        db: Session,
        wecom_user_id: str,
        msg_id: str,
        raw_xml: str,
        media_id: str | None,
        audio_format: str | None,
        recognition: str | None,
    ) -> None:
        user_repo = UserRepo(db)
        inbound_repo = InboundMessageRepo(db)
        voice_repo = VoiceRecordRepo(db)

        user = user_repo.get_or_create(wecom_user_id, timezone_name=self.settings.default_timezone)
        recognition_text = (recognition or "").strip()

        try:
            is_new = inbound_repo.create_if_new(msg_id, user.id, "voice", raw_xml, recognition_text)
        except Exception:
            self.dedup_failures += 1
            logger.exception(
                "voice_dedup_failed category=dedup msg_id=%s wecom_user_id=%s",
                msg_id,
                wecom_user_id,
            )
            self._send_user_text(wecom_user_id, self.reply_renderer.busy_fallback())
            return
        if not is_new:
            self.dedup_duplicates += 1
            logger.info(
                "duplicate_message_ignored category=dedup msg_id=%s wecom_user_id=%s",
                msg_id,
                wecom_user_id,
            )
            return

        if recognition_text:
            voice_repo.create_or_update(
                user_id=user.id,
                wecom_msg_id=msg_id,
                media_id=media_id,
                audio_format=audio_format,
                source="wecom_recognition",
                transcript_text=recognition_text,
                status=VoiceRecordStatus.TRANSCRIBED,
                error=None,
                latency_ms=0,
            )
            self._handle_normalized_message(db, user, wecom_user_id, msg_id, recognition_text, inbound_repo)
            return

        if not self.asr_service:
            voice_repo.create_or_update(
                user_id=user.id,
                wecom_msg_id=msg_id,
                media_id=media_id,
                audio_format=audio_format,
                source="local",
                transcript_text=None,
                status=VoiceRecordStatus.FAILED,
                error="asr_service_not_configured",
                latency_ms=None,
            )
            self._send_user_text(wecom_user_id, "我暂时还没法处理语音，先发文字给我也可以。")
            return

        started = perf_counter()
        try:
            asr_result = self.asr_service.transcribe_wecom_media(
                wecom_client=self.wecom_client,
                media_id=media_id or "",
                audio_format=audio_format,
            )
            transcript = asr_result.text.strip()
            voice_repo.create_or_update(
                user_id=user.id,
                wecom_msg_id=msg_id,
                media_id=media_id,
                audio_format=audio_format,
                source=asr_result.provider,
                transcript_text=transcript,
                status=VoiceRecordStatus.TRANSCRIBED,
                error=None,
                latency_ms=asr_result.latency_ms,
            )
        except AsrError as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            voice_repo.create_or_update(
                user_id=user.id,
                wecom_msg_id=msg_id,
                media_id=media_id,
                audio_format=audio_format,
                source="local",
                transcript_text=None,
                status=VoiceRecordStatus.FAILED,
                error=str(exc),
                latency_ms=elapsed_ms,
            )
            logger.warning(
                "voice_transcribe_failed category=processing msg_id=%s wecom_user_id=%s error=%s",
                msg_id,
                wecom_user_id,
                exc,
            )
            self._send_user_text(wecom_user_id, self._voice_asr_failure_reply(exc))
            return

        if not transcript:
            self._send_user_text(wecom_user_id, "这条语音我没听清，你可以再发一次，或者直接发文字。")
            return
        self._handle_normalized_message(db, user, wecom_user_id, msg_id, transcript, inbound_repo)

    def _handle_normalized_message(
        self,
        db: Session,
        user: User,
        wecom_user_id: str,
        msg_id: str,
        normalized: str,
        inbound_repo: InboundMessageRepo,
        reply_sink: Callable[[str], None] | None = None,
        message_source: ReminderSource = ReminderSource.WECHAT,
    ) -> None:
        pending_repo = PendingActionRepo(db)

        if not normalized:
            self._send_user_text(wecom_user_id, self.reply_renderer.empty_message(), reply_sink=reply_sink)
            return

        if self._is_pair_command(normalized):
            code = self._create_pair_code(db, user.id)
            self._send_user_text(
                wecom_user_id,
                self.reply_renderer.pair_code(code, self.settings.pair_code_minutes),
                reply_sink=reply_sink,
            )
            return

        pending = pending_repo.latest_pending_for_user(user.id)
        if pending:
            decision = self.confirm_service.parse_decision(normalized)
            if decision == PendingActionStatus.CONFIRMED:
                try:
                    draft = IntentDraft.model_validate(orjson.loads(pending.draft_json))
                    result = self.reminder_service.apply_confirmed_draft(db, user.id, draft)
                except Exception:
                    logger.exception(
                        "confirmed_action_failed category=processing msg_id=%s wecom_user_id=%s",
                        msg_id,
                        wecom_user_id,
                    )
                    self._send_user_text(wecom_user_id, self.reply_renderer.busy_fallback(), reply_sink=reply_sink)
                    return
                pending_repo.mark_status(pending, PendingActionStatus.CONFIRMED)
                self._send_user_text(wecom_user_id, result, reply_sink=reply_sink)
                return
            if decision == PendingActionStatus.REJECTED:
                pending_repo.mark_status(pending, PendingActionStatus.REJECTED)
                self._send_user_text(wecom_user_id, self.reply_renderer.action_canceled(), reply_sink=reply_sink)
                return
            self._send_user_text(wecom_user_id, self.reply_renderer.pending_action_waiting(), reply_sink=reply_sink)
            return

        try:
            context_messages = inbound_repo.recent_texts(user.id, limit=5, exclude_msg_id=msg_id)
            draft = self.intent_service.parse_intent(normalized, user.timezone, context_messages=context_messages)
        except Exception:
            logger.exception(
                "intent_parse_failed category=parse msg_id=%s wecom_user_id=%s",
                msg_id,
                wecom_user_id,
            )
            self._send_user_text(wecom_user_id, self.reply_renderer.busy_fallback(), reply_sink=reply_sink)
            return
        draft.source = message_source
        if draft.clarification_question:
            self._send_user_text(
                wecom_user_id,
                self.reply_renderer.clarification(draft.clarification_question),
                reply_sink=reply_sink,
            )
            return

        if draft.operation == OperationType.QUERY:
            try:
                summary_items = self.reminder_service.query_summary_items(db, user.id)
                fallback = self.reply_renderer.query_empty() if not summary_items else self.reply_renderer.query_summary(summary_items)
                summary = fallback
                if self.reply_generation_service:
                    summary = self.reply_generation_service.generate_query_summary(summary_items, fallback)
            except Exception:
                logger.exception(
                    "query_summary_failed category=processing msg_id=%s wecom_user_id=%s",
                    msg_id,
                    wecom_user_id,
                )
                self._send_user_text(wecom_user_id, self.reply_renderer.busy_fallback(), reply_sink=reply_sink)
                return
            self._send_user_text(wecom_user_id, summary, reply_sink=reply_sink)
            return

        self.confirm_service.create_pending_action(
            repo=pending_repo,
            user_id=user.id,
            action_type=draft.operation,
            draft=draft,
            source_message_id=msg_id,
        )
        fallback = self.reply_renderer.confirmation_prompt(
            operation=draft.operation,
            content=draft.content,
            timezone=draft.timezone,
            schedule=draft.schedule.value if draft.schedule else None,
            run_at_local=draft.run_at_local,
            rrule=draft.rrule,
        )
        reply = fallback
        if self.reply_generation_service:
            reply = self.reply_generation_service.generate_confirmation_prompt(draft, fallback)
        self._send_user_text(wecom_user_id, reply, reply_sink=reply_sink)

    @staticmethod
    def _is_pair_command(text: str) -> bool:
        return text.strip().lower() in {"配对", "配对码", "pair", "绑定设备", "mobile"}

    def _create_pair_code(self, db: Session, user_id: int) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        expires = now_utc() + timedelta(minutes=self.settings.pair_code_minutes)
        MobileRepo(db).create_pair_code(user_id=user_id, pair_code=code, expires_at=expires)
        return code

    def _send_user_text(
        self,
        wecom_user_id: str,
        content: str,
        reply_sink: Callable[[str], None] | None = None,
    ) -> None:
        if reply_sink:
            reply_sink(content)
            return
        ok, error = self.wecom_client.send_text(wecom_user_id, content)
        if not ok:
            logger.warning(
                "reply_send_failed category=external wecom_user_id=%s error=%s",
                wecom_user_id,
                error,
            )

    @staticmethod
    def _voice_asr_failure_reply(exc: AsrError) -> str:
        message = str(exc).lower()
        if "missing media_id" in message or "download_media_failed" in message or "download_media_http_error" in message:
            return "这条语音我没拿到完整音频，麻烦重发一次试试。"
        if "ffmpeg command not found" in message or "faster-whisper is not installed" in message:
            return "语音功能还没准备好（本地依赖缺失），先发文字给我，我照样能处理。"
        if "too long" in message:
            return "这条语音有点长，超过 60 秒了。可以分两条发，或者直接发文字。"
        if "timed out" in message or "timeout" in message:
            return "语音处理超时了，麻烦再发一次，或者直接发文字给我。"
        return "这条语音我没听清，你可以再发一次，或者直接发文字。"
