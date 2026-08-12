from incidentlens_control_plane.logs.redaction import redact_message


def test_redacts_token_password_email_ip_and_url_secret() -> None:
    result = redact_message(
        "token=abc123 password=hunter2 user=a@example.com ip=10.1.2.3 "
        "https://api.example.test/callback?secret=s3cr3t"
    )

    assert "abc123" not in result.message_redacted
    assert "hunter2" not in result.message_redacted
    assert "a@example.com" not in result.message_redacted
    assert "10.1.2.3" not in result.message_redacted
    assert "s3cr3t" not in result.message_redacted
    assert result.summary["token"] == 1
    assert result.summary["password"] == 1
    assert result.summary["email"] == 1
    assert result.summary["ip"] == 1
    assert result.summary["url_secret"] == 1


def test_truncates_redacted_message_to_16_kib() -> None:
    result = redact_message("x" * (16 * 1024 + 10))

    assert len(result.message_redacted) <= 16 * 1024
    assert result.truncated is True
    assert result.summary["truncated"] == 1
