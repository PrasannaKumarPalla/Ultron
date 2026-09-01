from pathlib import Path

from ultron.redaction import Redactor, dry_run, load_extra_patterns

SECRETS = (
    'DB_PASSWORD="hunter2secret"\n'
    "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
    "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
    "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c\n"
)


def test_redacts_keys_tokens_and_jwts():
    clean, names = Redactor().redact(SECRETS)
    assert "hunter2secret" not in clean
    assert "AKIAIOSFODNN7EXAMPLE" not in clean
    assert not any(token in clean for token in ("ghp_", "eyJhbGciOiJIUzI1NiJ9"))
    assert "[REDACTED:" in clean
    assert names  # pattern names recorded, never content


def test_pem_blocks_are_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----\n"
    clean, names = Redactor().redact(pem)
    assert "PRIVATE KEY" not in clean.replace("REDACTED", "")
    assert "pem_block" in names


def test_clean_text_is_verbatim():
    text = "just a normal prompt about refactoring the login page"
    clean, names = Redactor().redact(text)
    assert clean == text
    assert names == []


def test_extra_patterns_loaded_from_secrets_dir(tmp_path: Path):
    (tmp_path / "extra_patterns.txt").write_text(
        "# custom deny-list\nultron-internal-[a-z]{8}\n", encoding="utf-8")
    redactor = Redactor(secrets_dir=tmp_path)
    clean, names = redactor.redact("id: ultron-internal-abcdwxyz")
    assert "ultron-internal-abcdwxyz" not in clean
    assert names == ["custom:ultron-internal-[a-z]{8}"]


def test_load_extra_patterns_skips_comments_and_bad_regex(tmp_path: Path):
    (tmp_path / "extra_patterns.txt").write_text(
        "# comment\n\n(not-a-regex[\nokpattern\n", encoding="utf-8")
    patterns = load_extra_patterns(tmp_path)
    assert len(patterns) == 1


def test_dry_run_reports_findings_without_sending():
    report = dry_run("connect to postgres://user:s3cret@db.example/x")
    assert report["would_send_verbatim"] is False
    assert report["total"] >= 1
    assert all("s3cret" not in str(finding) for finding in report["findings"])
    assert report["redacted_text"] is not None
