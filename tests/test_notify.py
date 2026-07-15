from __future__ import annotations

import smtplib
import unittest
from email.message import EmailMessage
from typing import Any
from urllib import parse

from crypto_alerts.config import DeliveryConfig
from crypto_alerts.notify import (
    TELEGRAM_MESSAGE_LIMIT,
    NotificationConfigError,
    Notifier,
    split_telegram_text,
)

TELEGRAM_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd",  # noqa: S105
    "TELEGRAM_CHAT_ID": "-123456789",
}

EMAIL_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "alerts@example.com",
    "SMTP_FROM": "alerts@example.com",
    "SMTP_TO": "recipient@example.com",
    "SMTP_PASSWORD": "email-super-secret",  # noqa: S105
    "SMTP_SECURITY": "starttls",
}


def delivery(*, telegram: bool = False, email: bool = False) -> DeliveryConfig:
    return DeliveryConfig(
        send_empty_digest=False,
        telegram_enabled=telegram,
        email_enabled=email,
    )


class FakeHttpResponse:
    def __init__(self, payload: bytes = b'{"ok":true}', status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class RecordingUrlOpen:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, float]] = []

    def __call__(self, outgoing: Any, *, timeout: float) -> FakeHttpResponse:
        self.calls.append((outgoing, timeout))
        return FakeHttpResponse()


class FakeSmtp:
    def __init__(self, *, authentication_error: bool = False) -> None:
        self.authentication_error = authentication_error
        self.calls: list[Any] = []
        self.message: EmailMessage | None = None

    def __enter__(self) -> FakeSmtp:
        self.calls.append("enter")
        return self

    def __exit__(self, *_args: object) -> None:
        self.calls.append("exit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, *, context: object) -> None:
        self.calls.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))
        if self.authentication_error:
            raise smtplib.SMTPAuthenticationError(535, b"credential rejected")

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send_message")
        self.message = message


class RecordingSmtpFactory:
    def __init__(self, smtp: FakeSmtp) -> None:
        self.smtp = smtp
        self.calls: list[tuple[str, int, float, bool]] = []

    def __call__(self, host: str, port: int, timeout: float, use_ssl: bool) -> FakeSmtp:
        self.calls.append((host, port, timeout, use_ssl))
        return self.smtp


class NotificationTests(unittest.TestCase):
    def test_disabled_channels_need_no_environment_or_network(self) -> None:
        def forbidden_network(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("network must not be called")

        notifier = Notifier.from_environment(
            delivery(),
            environment={},
            urlopen=forbidden_network,
            smtp_factory=forbidden_network,
        )

        result = notifier.send("Daily digest", "No material events.")

        self.assertTrue(result.success)
        self.assertFalse(result.telegram.enabled)
        self.assertFalse(result.email.enabled)
        self.assertEqual(result.telegram.attempted, 0)
        self.assertEqual(result.email.attempted, 0)

    def test_enabled_channel_validates_required_environment(self) -> None:
        with self.assertRaisesRegex(NotificationConfigError, "TELEGRAM_BOT_TOKEN"):
            Notifier.from_environment(delivery(telegram=True), environment={})
        with self.assertRaisesRegex(NotificationConfigError, "SMTP_PASSWORD"):
            Notifier.from_environment(
                delivery(email=True),
                environment={
                    key: value for key, value in EMAIL_ENV.items() if key != "SMTP_PASSWORD"
                },
            )

    def test_telegram_uses_https_plain_text_and_timeout(self) -> None:
        recorder = RecordingUrlOpen()
        notifier = Notifier.from_environment(
            delivery(telegram=True),
            environment=TELEGRAM_ENV,
            timeout_seconds=7,
            urlopen=recorder,
        )
        dangerous_text = "*[not Markdown](https://malicious.invalid) <b>not HTML</b>"

        result = notifier.send("Alert <unsafe>", dangerous_text)

        self.assertTrue(result.success)
        self.assertEqual(result.telegram.delivered, 1)
        outgoing, timeout = recorder.calls[0]
        self.assertTrue(outgoing.full_url.startswith("https://api.telegram.org/bot"))
        self.assertEqual(timeout, 7.0)
        fields = parse.parse_qs(outgoing.data.decode("utf-8"), strict_parsing=True)
        self.assertEqual(fields["text"], [f"Alert <unsafe>\n\n{dangerous_text}"])
        self.assertNotIn("parse_mode", fields)

    def test_telegram_messages_are_split_below_hard_limit(self) -> None:
        recorder = RecordingUrlOpen()
        notifier = Notifier.from_environment(
            delivery(telegram=True),
            environment=TELEGRAM_ENV,
            urlopen=recorder,
        )

        result = notifier.send("Long digest", "x" * 9_000)

        self.assertTrue(result.telegram.success)
        self.assertEqual(result.telegram.attempted, 3)
        sent_texts = [
            parse.parse_qs(outgoing.data.decode("utf-8"))["text"][0]
            for outgoing, _timeout in recorder.calls
        ]
        self.assertTrue(all(len(text) <= TELEGRAM_MESSAGE_LIMIT for text in sent_texts))
        self.assertEqual("".join(sent_texts), "Long digest\n\n" + "x" * 9_000)

    def test_transport_exception_does_not_leak_token_or_exception_message(self) -> None:
        token = TELEGRAM_ENV["TELEGRAM_BOT_TOKEN"]

        def failing_urlopen(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError(f"failed URL included secret {token}")

        notifier = Notifier.from_environment(
            delivery(telegram=True),
            environment=TELEGRAM_ENV,
            urlopen=failing_urlopen,
        )

        result = notifier.send("Alert", "Body")

        self.assertFalse(result.success)
        self.assertEqual(result.telegram.error_code, "telegram_error")
        self.assertNotIn(token, repr(result))
        self.assertNotIn("failed URL", repr(result))

    def test_smtp_starttls_delivery_uses_injected_stdlib_compatible_client(self) -> None:
        smtp = FakeSmtp()
        factory = RecordingSmtpFactory(smtp)
        notifier = Notifier.from_environment(
            delivery(email=True),
            environment=EMAIL_ENV,
            timeout_seconds=8,
            smtp_factory=factory,
        )

        result = notifier.send("Daily digest", "Material events: none")

        self.assertTrue(result.email.success)
        self.assertEqual(result.email.delivered, 1)
        self.assertEqual(factory.calls, [("smtp.example.com", 587, 8.0, False)])
        self.assertEqual(smtp.calls.count("ehlo"), 2)
        self.assertTrue(
            any(isinstance(call, tuple) and call[0] == "starttls" for call in smtp.calls)
        )
        self.assertEqual(
            (smtp.message["Subject"], smtp.message["From"], smtp.message["To"]),
            ("Daily digest", "alerts@example.com", "recipient@example.com"),
        )
        self.assertIn("Material events: none", smtp.message.get_content())

    def test_smtp_authentication_failure_is_structured_and_secret_free(self) -> None:
        smtp = FakeSmtp(authentication_error=True)
        notifier = Notifier.from_environment(
            delivery(email=True),
            environment=EMAIL_ENV,
            smtp_factory=RecordingSmtpFactory(smtp),
        )

        result = notifier.send("Daily digest", "Body")

        self.assertFalse(result.success)
        self.assertEqual(result.email.error_code, "smtp_authentication_failed")
        self.assertNotIn(EMAIL_ENV["SMTP_PASSWORD"], repr(result))

    def test_smtp_ssl_mode_does_not_attempt_starttls(self) -> None:
        smtp = FakeSmtp()
        factory = RecordingSmtpFactory(smtp)
        environment = {**EMAIL_ENV, "SMTP_PORT": "465", "SMTP_SECURITY": "ssl"}
        notifier = Notifier.from_environment(
            delivery(email=True),
            environment=environment,
            smtp_factory=factory,
        )

        result = notifier.send("Daily digest", "Body")

        self.assertTrue(result.email.success)
        self.assertEqual(factory.calls, [("smtp.example.com", 465, 12.0, True)])
        self.assertFalse(
            any(isinstance(call, tuple) and call[0] == "starttls" for call in smtp.calls)
        )

    def test_subject_header_injection_is_rejected_before_transport(self) -> None:
        recorder = RecordingUrlOpen()
        notifier = Notifier.from_environment(
            delivery(telegram=True),
            environment=TELEGRAM_ENV,
            urlopen=recorder,
        )

        with self.assertRaises(ValueError):
            notifier.send("Subject\nBcc: victim@example.com", "Body")
        self.assertEqual(recorder.calls, [])

    def test_splitter_preserves_text_and_never_exceeds_limit(self) -> None:
        text = ("alpha beta gamma\n" * 700).rstrip()
        chunks = split_telegram_text(text, limit=128)

        self.assertTrue(all(0 < len(chunk) <= 128 for chunk in chunks))
        # Separators at split points are intentionally removed, but all words survive.
        self.assertEqual(" ".join(" ".join(chunks).split()), " ".join(text.split()))


if __name__ == "__main__":
    unittest.main()
