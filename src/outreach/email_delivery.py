from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import os
import smtplib
import ssl
from types import TracebackType
from typing import Callable, Protocol, Self

from dotenv import dotenv_values


@dataclass(frozen=True)
class EmailDeliveryConfig:
    host: str
    port: int
    from_email: str
    username: str = ""
    password: str = ""
    use_ssl: bool = False
    starttls: bool = True
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "EmailDeliveryConfig":
        file_values = dotenv_values(".env")

        def value(name: str, default: str = "") -> str:
            raw = os.getenv(name)
            if raw is None:
                raw = file_values.get(name) or default
            return str(raw).strip()

        host = value("SMTP_HOST")
        from_email = value("SMTP_FROM_EMAIL")
        if not host or not from_email:
            raise ValueError("SMTP_HOST and SMTP_FROM_EMAIL are required for live email delivery.")
        use_ssl = _env_bool("SMTP_USE_SSL", False, file_values=file_values)
        return cls(
            host=host,
            port=int(value("SMTP_PORT", "465" if use_ssl else "587")),
            from_email=from_email,
            username=value("SMTP_USERNAME"),
            password=value("SMTP_PASSWORD"),
            use_ssl=use_ssl,
            starttls=_env_bool("SMTP_STARTTLS", not use_ssl, file_values=file_values),
            timeout_seconds=float(value("SMTP_TIMEOUT_SECONDS", "30")),
        )


@dataclass(frozen=True)
class EmailDeliveryResult:
    recipient: str
    subject: str
    status: str
    detail: str = ""


class EmailSender(Protocol):
    def send(self, *, recipient: str, subject: str, body: str) -> EmailDeliveryResult: ...


class SmtpClient(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def starttls(self, *, context: ssl.SSLContext) -> object: ...

    def login(self, user: str, password: str) -> object: ...

    def send_message(self, message: EmailMessage) -> object: ...


class SmtpEmailSender:
    """Small SMTP adapter used only after an explicit CLI execute flag."""

    def __init__(
        self,
        config: EmailDeliveryConfig,
        *,
        smtp_factory: Callable[..., SmtpClient] | None = None,
    ) -> None:
        self.config = config
        self.smtp_factory = smtp_factory

    def send(self, *, recipient: str, subject: str, body: str) -> EmailDeliveryResult:
        try:
            message = EmailMessage()
            message["From"] = self.config.from_email
            message["To"] = recipient
            message["Subject"] = subject
            message.set_content(body)
            context = ssl.create_default_context()
            if self.smtp_factory is not None:
                factory = self.smtp_factory
                client = (
                    factory(self.config.host, self.config.port, context=context)
                    if self.config.use_ssl
                    else factory(self.config.host, self.config.port)
                )
            elif self.config.use_ssl:
                client = smtplib.SMTP_SSL(
                    self.config.host,
                    self.config.port,
                    context=context,
                    timeout=self.config.timeout_seconds,
                )
            else:
                client = smtplib.SMTP(
                    self.config.host,
                    self.config.port,
                    timeout=self.config.timeout_seconds,
                )
            with client:
                if self.config.starttls and not self.config.use_ssl:
                    client.starttls(context=context)
                if self.config.username:
                    client.login(self.config.username, self.config.password)
                client.send_message(message)
        except Exception as exc:
            return EmailDeliveryResult(recipient, subject, "failed", str(exc))
        return EmailDeliveryResult(recipient, subject, "sent")


def deliver_email_drafts(
    drafts: list[dict[str, object]],
    *,
    sender: EmailSender | None,
    execute: bool,
    limit: int,
    before_send: Callable[[dict[str, object]], None] | None = None,
    after_send: Callable[[dict[str, object], EmailDeliveryResult], None] | None = None,
) -> list[dict[str, object]]:
    """Preview or deliver a bounded set of already-reviewed draft records."""
    results: list[dict[str, object]] = []
    seen_recipients: set[str] = set()
    for draft in drafts:
        if len(results) >= limit:
            break
        recipient = str(draft.get("email") or "").strip().lower()
        subject = str(draft.get("subject") or "").strip()
        body = str(draft.get("body") or "").strip()
        if not recipient or "@" not in recipient or not subject or not body:
            results.append({**draft, "delivery_status": "invalid", "delivery_detail": "email, subject, and body are required"})
            continue
        if recipient in seen_recipients:
            results.append({**draft, "delivery_status": "duplicate", "delivery_detail": "duplicate recipient in batch"})
            continue
        seen_recipients.add(recipient)
        if not execute:
            results.append({**draft, "delivery_status": "ready", "delivery_detail": "preview only"})
            continue
        if sender is None:
            raise ValueError("A sender is required when execute=True.")
        if before_send is not None:
            before_send(draft)
        outcome = sender.send(recipient=recipient, subject=subject, body=body)
        if after_send is not None:
            after_send(draft, outcome)
        results.append({**draft, "delivery_status": outcome.status, "delivery_detail": outcome.detail})
    return results


def _env_bool(
    name: str,
    default: bool,
    *,
    file_values: dict[str, str | None] | None = None,
) -> bool:
    value = os.getenv(name)
    if value is None and file_values is not None:
        value = file_values.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
