from __future__ import annotations

import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.core.config import get_settings
from app.core.logging import get_logger


logger = get_logger("asr")


class AsrError(Exception):
    pass


class AsrValidationError(AsrError):
    pass


class AsrTimeoutError(AsrError):
    pass


@dataclass
class TranscriptionResult:
    text: str
    language: str
    provider: str
    model: str
    request_id: str | None
    latency_ms: int
    used_fallback: bool = False


class AsrProvider(Protocol):
    name: str
    mode: str
    model: str

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        language_hint: str | None = None,
    ) -> TranscriptionResult:
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str | None]:
        raise NotImplementedError


class LocalAsrProvider:
    name = "local"
    mode = "local"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._model = None
        self.model = self.settings.asr_local_model

    def healthcheck(self) -> tuple[bool, str | None]:
        try:
            import faster_whisper  # noqa: F401
            subprocess.run(
                ["ffmpeg", "-version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return True, None
        except Exception as exc:
            return False, f"local_asr_unavailable:{exc}"

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        language_hint: str | None = None,
    ) -> TranscriptionResult:
        if not audio_bytes:
            raise AsrValidationError("audio payload is empty")

        started = time.perf_counter()
        suffix = self._guess_suffix(filename, mime_type)

        with tempfile.TemporaryDirectory(prefix="memomate-asr-") as tmp_dir:
            input_path = Path(tmp_dir) / f"input{suffix}"
            output_path = Path(tmp_dir) / "normalized.wav"

            input_path.write_bytes(audio_bytes)
            self._convert_to_wav(input_path, output_path)
            duration = self._wav_duration_seconds(output_path)
            if duration > self.settings.asr_max_audio_seconds:
                raise AsrValidationError(
                    f"audio is too long ({duration:.1f}s), max is {self.settings.asr_max_audio_seconds}s"
                )

            model = self._load_model()
            segments, info = model.transcribe(
                str(output_path),
                language=language_hint or None,
                beam_size=5,
                vad_filter=True,
            )
            text = "".join(segment.text for segment in segments).strip()
            if not text:
                raise AsrError("transcription is empty")

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        language = getattr(info, "language", None) or language_hint or "zh"
        return TranscriptionResult(
            text=text,
            language=str(language or "zh"),
            provider=self.name,
            model=self.model,
            request_id=uuid4().hex,
            latency_ms=elapsed_ms,
            used_fallback=False,
        )

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrError("faster-whisper is not installed") from exc

        self._model = WhisperModel(
            self.settings.asr_local_model,
            device=self.settings.asr_local_device,
            compute_type=self.settings.asr_local_compute_type,
        )
        return self._model

    def _convert_to_wav(self, input_path: Path, output_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(output_path),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.settings.asr_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise AsrError("ffmpeg command not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise AsrTimeoutError("audio conversion timed out") from exc
        except subprocess.CalledProcessError as exc:
            raise AsrError(f"ffmpeg conversion failed: {exc.stderr.strip()}") from exc

    @staticmethod
    def _wav_duration_seconds(path: Path) -> float:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            framerate = wav_file.getframerate()
            if framerate <= 0:
                return 0.0
            return frames / float(framerate)

    @staticmethod
    def _guess_suffix(filename: str | None, mime_type: str | None) -> str:
        if filename and "." in filename:
            return "." + filename.rsplit(".", 1)[-1].lower()
        mime_map = {
            "audio/amr": ".amr",
            "audio/x-amr": ".amr",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/ogg": ".ogg",
        }
        if mime_type and mime_type.lower() in mime_map:
            return mime_map[mime_type.lower()]
        return ".amr"


class IflytekAsrProvider:
    name = "iflytek"
    mode = "external"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.model = "iflytek-reserved"

    def healthcheck(self) -> tuple[bool, str | None]:
        if not self.settings.asr_external_enabled:
            return True, None
        missing = [
            key
            for key, value in (
                ("ASR_IFLYTEK_APP_ID", self.settings.asr_iflytek_app_id),
                ("ASR_IFLYTEK_API_KEY", self.settings.asr_iflytek_api_key),
                ("ASR_IFLYTEK_API_SECRET", self.settings.asr_iflytek_api_secret),
            )
            if not value
        ]
        if missing:
            return False, f"iflytek_config_missing:{','.join(missing)}"
        return False, "iflytek_provider_placeholder_not_implemented"

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        language_hint: str | None = None,
    ) -> TranscriptionResult:
        raise AsrError("iflytek provider is reserved and not implemented in this phase")


class AsrService:
    def __init__(
        self,
        provider: AsrProvider | None = None,
        providers: list[AsrProvider] | None = None,
    ) -> None:
        self.settings = get_settings()
        if providers:
            self.providers = providers
        elif provider:
            self.providers = [provider]
        else:
            self.providers = self._build_provider_chain()
        self._last_error: str | None = None

    @property
    def provider_name(self) -> str:
        return getattr(self.providers[0], "name", "unknown")

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def health_status(self) -> tuple[bool, str, str | None]:
        if not self.settings.asr_enabled:
            return False, self.provider_name, "asr_disabled"
        ok, error = self.providers[0].healthcheck()
        if error:
            self._last_error = error
        else:
            self._last_error = None
        return ok, self.provider_name, error

    def capability(self) -> dict[str, str | bool | None]:
        primary = self.providers[0]
        return {
            "enabled": self.settings.asr_enabled,
            "provider": primary.name,
            "model": getattr(primary, "model", None),
            "mode": getattr(primary, "mode", None),
            "fallback_enabled": self.settings.asr_fallback_enabled,
        }

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        language_hint: str | None = None,
    ) -> TranscriptionResult:
        if not self.settings.asr_enabled:
            raise AsrError("asr is disabled")
        providers = self.providers if self.settings.asr_fallback_enabled else self.providers[:1]
        first_exc: Exception | None = None
        error_chain: list[str] = []
        for index, provider in enumerate(providers):
            try:
                result = provider.transcribe(
                    audio_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    language_hint=language_hint,
                )
                if index > 0:
                    result.used_fallback = True
                self._last_error = None
                return result
            except AsrError as exc:
                if first_exc is None:
                    first_exc = exc
                error_chain.append(f"{provider.name}:{exc}")
                self._last_error = str(exc)
            except Exception as exc:
                if first_exc is None:
                    first_exc = exc
                error_chain.append(f"{provider.name}:{exc}")
                self._last_error = str(exc)
        if error_chain:
            self._last_error = " | ".join(error_chain)
        if first_exc is None:
            raise AsrError("asr provider chain is empty")
        if isinstance(first_exc, AsrError):
            raise first_exc
        raise AsrError(str(first_exc)) from first_exc

    def transcribe_wecom_media(
        self,
        *,
        wecom_client,
        media_id: str,
        audio_format: str | None = None,
    ) -> TranscriptionResult:
        if not media_id:
            raise AsrValidationError("missing media_id")
        content, mime_type = wecom_client.download_media(media_id)
        ext = (audio_format or "amr").lower().strip(".")
        filename = f"wechat_voice.{ext}"
        return self.transcribe_bytes(content, filename=filename, mime_type=mime_type, language_hint="zh")

    def _build_provider_chain(self) -> list[AsrProvider]:
        providers: list[AsrProvider] = []
        primary = self.settings.asr_provider.lower().strip()
        external_provider = self._build_external_provider(optional=True)
        if primary == "external":
            if external_provider is not None:
                providers.append(external_provider)
            else:
                providers.append(LocalAsrProvider())
        else:
            providers.append(LocalAsrProvider())

        if self.settings.asr_fallback_enabled:
            for item in self._parse_fallback_order():
                if item == "local":
                    self._append_unique(providers, LocalAsrProvider())
                elif item == "external" and external_provider is not None:
                    self._append_unique(providers, external_provider)
        return providers

    def _build_external_provider(self, optional: bool = False) -> AsrProvider | None:
        if not self.settings.asr_external_enabled:
            if optional:
                return None
            raise AsrError("external asr provider is disabled")
        external_provider = self.settings.asr_external_provider.lower().strip()
        if external_provider == "iflytek":
            return IflytekAsrProvider()
        raise AsrError(f"unsupported external asr provider: {external_provider}")

    def _parse_fallback_order(self) -> list[str]:
        values = [part.strip().lower() for part in self.settings.fallback_order.split(",") if part.strip()]
        if not values:
            return ["external", "local", "template"]
        out: list[str] = []
        for item in values:
            if item not in out:
                out.append(item)
        return out

    @staticmethod
    def _append_unique(providers: list[AsrProvider], candidate: AsrProvider) -> None:
        names = {provider.name for provider in providers}
        if candidate.name not in names:
            providers.append(candidate)
