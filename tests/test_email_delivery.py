from outreach.email_delivery import (
    EmailDeliveryConfig,
    EmailDeliveryResult,
    SmtpEmailSender,
    deliver_email_drafts,
)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, *, recipient: str, subject: str, body: str) -> EmailDeliveryResult:
        self.sent.append((recipient, subject, body))
        return EmailDeliveryResult(recipient, subject, "sent")


def test_delivery_is_preview_only_without_execute() -> None:
    results = deliver_email_drafts(
        [{"email": "person@example.com", "subject": "Hello", "body": "Specific note"}],
        sender=None,
        execute=False,
        limit=5,
    )

    assert results[0]["delivery_status"] == "ready"


def test_delivery_executes_bounded_unique_batch() -> None:
    sender = FakeSender()
    draft = {"email": "person@example.com", "subject": "Hello", "body": "Specific note"}
    results = deliver_email_drafts([draft, draft], sender=sender, execute=True, limit=5)

    assert [item["delivery_status"] for item in results] == ["sent", "duplicate"]
    assert sender.sent == [("person@example.com", "Hello", "Specific note")]


def test_delivery_never_sends_two_subjects_to_the_same_recipient() -> None:
    sender = FakeSender()
    results = deliver_email_drafts(
        [
            {"email": "person@example.com", "subject": "First", "body": "One"},
            {"email": "person@example.com", "subject": "Second", "body": "Two"},
        ],
        sender=sender,
        execute=True,
        limit=5,
    )

    assert [item["delivery_status"] for item in results] == ["sent", "duplicate"]
    assert len(sender.sent) == 1


def test_starttls_smtp_uses_tls_after_connecting() -> None:
    calls: list[object] = []

    class FakeSmtp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self, *, context):
            calls.append(("starttls", context))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"]))

    def factory(host, port):
        calls.append(("connect", host, port))
        return FakeSmtp()

    sender = SmtpEmailSender(
        EmailDeliveryConfig(
            host="smtp.example.com",
            port=587,
            from_email="sender@example.com",
            username="user",
            password="secret",
            starttls=True,
        ),
        smtp_factory=factory,
    )

    result = sender.send(
        recipient="person@example.com",
        subject="Specific subject",
        body="Specific body",
    )

    assert result.status == "sent"
    assert calls[0] == ("connect", "smtp.example.com", 587)
    assert calls[1][0] == "starttls"
    assert calls[2:] == [
        ("login", "user", "secret"),
        ("send", "person@example.com", "Specific subject"),
    ]


def test_smtp_config_reads_repo_local_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "SMTP_HOST",
        "SMTP_FROM_EMAIL",
        "SMTP_PORT",
        "SMTP_STARTTLS",
        "SMTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "SMTP_HOST=smtp.local\n"
        "SMTP_FROM_EMAIL=sender@example.com\n"
        "SMTP_PORT=2525\n"
        "SMTP_STARTTLS=false\n"
        "SMTP_TIMEOUT_SECONDS=12\n",
        encoding="utf-8",
    )

    config = EmailDeliveryConfig.from_env()

    assert config.host == "smtp.local"
    assert config.from_email == "sender@example.com"
    assert config.port == 2525
    assert config.starttls is False
    assert config.timeout_seconds == 12
