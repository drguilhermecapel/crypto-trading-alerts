"""Optional, environment-configured Telegram and SMTP delivery.

The module intentionally uses only Python's standard-library network clients.
Credentials are read from environment variables and are never included in
results, exception messages, or logs.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib import error, parse, request

from .config import DeliveryConfig

TELEGRAM_MESSAGE_LIMIT = 4096
_MAX_NOTIFICATION_CHARS = 100_000
_DEFAULT_TIMEOUT_SECONDS = 12.0
_TELEGRAM_TOKEN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_CHAT = re.compile(r"^(?:-?[0-9]{1,24}|@[A-Za-z0-9_]{5,32})$")


class NotificationConfigError(ValueError):
    """Raised when an enabled channel lacks safe environment configuration."""


class _TransportError(RuntimeError):
    def __init__(self, code: str, *, attempted: int = 1, delivered: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.attempted = attempted
        self.delivered = delivered


@dataclass(frozen=True, slots=True)
class ChannelResult:
    """Secret-free delivery outcome for one channel."""

    channel: str
    enabled: bool
    success: bool
    attempted: int
    delivered: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Combined outcomes; enabled channels succeed independently."""

    telegram: ChannelResult
    email: ChannelResult

    @property
    def success(self) -> bool:
        return self.telegram.success and self.email.success


@dataclass(frozen=True, slots=True)
class _TelegramSettings:
    token: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class _EmailSettings:
    host: str
    port: int
    username: str
    password: str
    from_email: str
    to_email: str
    security: str


UrlOpen = Callable[..., Any]
SmtpFactory = Callable[[str, int, float, bool], Any]


def _default_smtp_factory(host: str, port: int, timeout: float, use_ssl: bool) -> Any:
    if use_ssl:
        return smtplib.SMTP_SSL(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    return smtplib.SMTP(host, port, timeout=timeout)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise NotificationConfigError(f"missing required environment variable: {name}")
    if value != value.strip() or "\x00" in value:
        raise NotificationConfigError(f"invalid environment variable: {name}")
    return value


def _email_address(value: str, variable: str) -> str:
    display_name, address = parseaddr(value)
    if (
        display_name
        or address != value
        or "@" not in address
        or address.startswith("@")
        or address.endswith("@")
        or "\r" in address
        or "\n" in address
    ):
        raise NotificationConfigError(f"invalid email address in environment variable: {variable}")
    return address


def _smtp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise NotificationConfigError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise NotificationConfigError("SMTP_PORT must be between 1 and 65535")
    return port


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("timeout_seconds must be numeric")
    result = float(value)
    if not 1.0 <= result <= 60.0:
        raise ValueError("timeout_seconds must be between 1 and 60")
    return result


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> tuple[str, ...]:
    """Split plain text without exceeding Telegram's per-message limit."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not text:
        return ("",)

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
        if remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]
    chunks.append(remaining)
    return tuple(chunks)


class Notifier:
    """Deliver plain-text notifications to explicitly enabled channels."""

    def __init__(
        self,
        *,
        telegram: _TelegramSettings | None,
        email: _EmailSettings | None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        urlopen: UrlOpen = request.urlopen,
        smtp_factory: SmtpFactory = _default_smtp_factory,
    ) -> None:
        self._telegram = telegram
        self._email = email
        self._timeout = _timeout(timeout_seconds)
        self._urlopen = urlopen
        self._smtp_factory = smtp_factory

    @classmethod
    def from_environment(
        cls,
        delivery: DeliveryConfig,
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        urlopen: UrlOpen = request.urlopen,
        smtp_factory: SmtpFactory = _default_smtp_factory,
    ) -> Notifier:
        """Build enabled transports exclusively from environment variables."""

        if not isinstance(delivery, DeliveryConfig):
            raise TypeError("delivery must be a DeliveryConfig")
        env = os.environ if environment is None else environment

        telegram: _TelegramSettings | None = None
        if delivery.telegram_enabled:
            token = _required(env, "TELEGRAM_BOT_TOKEN")
            chat_id = _required(env, "TELEGRAM_CHAT_ID")
            if not _TELEGRAM_TOKEN.fullmatch(token):
                raise NotificationConfigError("TELEGRAM_BOT_TOKEN has an invalid format")
            if not _TELEGRAM_CHAT.fullmatch(chat_id):
                raise NotificationConfigError("TELEGRAM_CHAT_ID has an invalid format")
            telegram = _TelegramSettings(token=token, chat_id=chat_id)

        email: _EmailSettings | None = None
        if delivery.email_enabled:
            host = _required(env, "SMTP_HOST")
            if any(character.isspace() for character in host) or "/" in host or ":" in host:
                raise NotificationConfigError("SMTP_HOST must be a hostname without a port")
            from_email = _email_address(_required(env, "SMTP_FROM"), "SMTP_FROM")
            to_email = _email_address(_required(env, "SMTP_TO"), "SMTP_TO")
            username = _required(env, "SMTP_USERNAME")
            if username != username.strip():
                raise NotificationConfigError("SMTP_USERNAME has an invalid format")
            password = _required(env, "SMTP_PASSWORD")
            security = env.get("SMTP_SECURITY", "starttls").lower()
            if security not in {"starttls", "ssl"}:
                raise NotificationConfigError("SMTP_SECURITY must be starttls or ssl")
            email = _EmailSettings(
                host=host,
                port=_smtp_port(_required(env, "SMTP_PORT")),
                username=username,
                password=password,
                from_email=from_email,
                to_email=to_email,
                security=security,
            )

        return cls(
            telegram=telegram,
            email=email,
            timeout_seconds=timeout_seconds,
            urlopen=urlopen,
            smtp_factory=smtp_factory,
        )

    def send(self, subject: str, body: str) -> DeliveryResult:
        """Attempt each enabled channel and return structured, secret-free results."""

        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must be a non-empty string")
        if "\r" in subject or "\n" in subject or len(subject) > 255:
            raise ValueError("subject must be one line and at most 255 characters")
        if not isinstance(body, str):
            raise TypeError("body must be a string")
        if len(subject) + len(body) > _MAX_NOTIFICATION_CHARS:
            raise ValueError("notification exceeds the safe size limit")

        telegram_result = self._send_telegram(f"{subject}\n\n{body}".rstrip())
        email_result = self._send_email(subject, body)
        return DeliveryResult(telegram=telegram_result, email=email_result)

    def _send_telegram(self, text: str) -> ChannelResult:
        if self._telegram is None:
            return ChannelResult("telegram", False, True, 0, 0)

        chunks = split_telegram_text(text)
        delivered = 0
        for attempted, chunk in enumerate(chunks, start=1):
            try:
                self._post_telegram_chunk(chunk)
            except _TransportError as exc:
                return ChannelResult(
                    "telegram",
                    True,
                    False,
                    attempted,
                    delivered,
                    exc.code,
                )
            except Exception as exc:  # Network libraries expose many implementation errors.
                return ChannelResult(
                    "telegram",
                    True,
                    False,
                    attempted,
                    delivered,
                    _safe_error_code(exc, "telegram_error"),
                )
            delivered += 1
        return ChannelResult("telegram", True, True, len(chunks), delivered)

    def _post_telegram_chunk(self, text: str) -> None:
        settings = self._telegram
        if settings is None:
            raise _TransportError("telegram_not_configured")
        endpoint = f"https://api.telegram.org/bot{settings.token}/sendMessage"
        encoded = parse.urlencode({"chat_id": settings.chat_id, "text": text}).encode("utf-8")
        outgoing = request.Request(  # noqa: S310 -- endpoint is fixed to official HTTPS.
            endpoint,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            method="POST",
        )
        try:
            with self._urlopen(outgoing, timeout=self._timeout) as response:
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                payload = response.read(65_537)
        except error.HTTPError as exc:
            code = exc.code if isinstance(exc.code, int) else 0
            raise _TransportError(f"telegram_http_{code}") from None
        except TimeoutError:
            raise _TransportError("telegram_timeout") from None
        except (error.URLError, OSError):
            raise _TransportError("telegram_network_error") from None

        if status != 200 or len(payload) > 65_536:
            raise _TransportError("telegram_protocol_error")
        try:
            response_data = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise _TransportError("telegram_protocol_error") from None
        if not isinstance(response_data, dict) or response_data.get("ok") is not True:
            raise _TransportError("telegram_rejected")

    def _send_email(self, subject: str, body: str) -> ChannelResult:
        if self._email is None:
            return ChannelResult("email", False, True, 0, 0)

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._email.from_email
        message["To"] = self._email.to_email
        message.set_content(body)
        try:
            use_ssl = self._email.security == "ssl"
            with self._smtp_factory(
                self._email.host,
                self._email.port,
                self._timeout,
                use_ssl,
            ) as server:
                if not use_ssl:
                    server.ehlo()
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(self._email.username, self._email.password)
                server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            return ChannelResult("email", True, False, 1, 0, "smtp_authentication_failed")
        except TimeoutError:
            return ChannelResult("email", True, False, 1, 0, "smtp_timeout")
        except smtplib.SMTPException:
            return ChannelResult("email", True, False, 1, 0, "smtp_error")
        except OSError:
            return ChannelResult("email", True, False, 1, 0, "smtp_network_error")
        except Exception:
            return ChannelResult("email", True, False, 1, 0, "smtp_error")
        return ChannelResult("email", True, True, 1, 1)


def _safe_error_code(exc: Exception, fallback: str) -> str:
    """Classify an unexpected exception without serializing its message."""

    if isinstance(exc, TimeoutError):
        return fallback.replace("error", "timeout")
    return fallback
