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


def test_redacts_json_quoted_secret_keys() -> None:
    result = redact_message('{"password":"hunter2","token":"abc123"}')

    assert "hunter2" not in result.message_redacted
    assert "abc123" not in result.message_redacted
    assert result.summary["password"] == 1
    assert result.summary["token"] == 1


def test_redacts_json_quoted_secret_keys_with_whitespace() -> None:
    result = redact_message('{"password": "hunter2", "token": "abc123"}')

    assert "hunter2" not in result.message_redacted
    assert "abc123" not in result.message_redacted
    assert result.summary["password"] == 1
    assert result.summary["token"] == 1


def test_redacts_single_quoted_json_keys() -> None:
    result = redact_message("{'password': 'hunter2', 'token': 'abc123'}")

    assert "hunter2" not in result.message_redacted
    assert "abc123" not in result.message_redacted
    assert result.summary["password"] == 1
    assert result.summary["token"] == 1


def test_truncates_redacted_message_to_16_kib() -> None:
    result = redact_message("x" * (16 * 1024 + 10))

    assert len(result.message_redacted) <= 16 * 1024
    assert result.truncated is True
    assert result.summary["truncated"] == 1


def test_preserves_clock_time_not_ipv6() -> None:
    result = redact_message("started at 10:11:12")

    assert "10:11:12" in result.message_redacted
    assert "ip" not in result.summary


def test_redacts_compressed_ipv6() -> None:
    result = redact_message("link local fe80::1, loopback ::1, 2001:db8::1")

    assert "fe80::1" not in result.message_redacted
    assert "::1" not in result.message_redacted
    assert "2001:db8::1" not in result.message_redacted
    assert result.summary["ip"] == 3
