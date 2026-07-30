"""Redaction of each credential shape named in the brief."""
import pytest

from obskit.redaction import REDACTED, scrub, scrub_text


@pytest.mark.parametrize("raw,must_keep,must_drop", [
    # connection strings: scheme + user + host survive, password does not
    ("mongodb://app:s3cretP4ss@db1.internal:27017/legion?authSource=admin",
     ["mongodb://", "app:", "db1.internal:27017", "authSource=admin"], "s3cretP4ss"),
    ("mongodb+srv://admin:Changeme001@oci-p.mdbdemo.in/?replicaSet=rs0",
     ["mongodb+srv://", "admin:", "oci-p.mdbdemo.in", "replicaSet=rs0"], "Changeme001"),
    ("postgres://svc_user:hunter2@pg.internal:5432/app",
     ["postgres://", "svc_user:", "pg.internal:5432"], "hunter2"),
    ("postgresql://svc_user:hunter2@pg.internal:5432/app",
     ["postgresql://", "pg.internal:5432"], "hunter2"),
    ("mysql://root:toor@mysql.internal:3306/db",
     ["mysql://", "root:", "mysql.internal:3306"], "toor"),
    ("redis://default:myredissecret@127.0.0.1:6379/0",
     ["redis://", "default:", "127.0.0.1:6379"], "myredissecret"),
])
def test_connection_strings(raw, must_keep, must_drop):
    out = scrub_text(raw)
    assert must_drop not in out
    assert REDACTED in out
    for keep in must_keep:
        assert keep in out, f"lost debugging context {keep!r} from {out!r}"


def test_bearer_token_keeps_scheme():
    out = scrub_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghij.signature")
    assert "Bearer" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert REDACTED in out


def test_basic_auth_header():
    out = scrub_text("Authorization: Basic YWRtaW46Y2hhbmdlbWUwMDE=")
    assert "Basic" in out and "YWRtaW46Y2hhbmdlbWUwMDE=" not in out


@pytest.mark.parametrize("key,prefix", [
    ("sk-lf-0123456789abcdef0123", "sk-lf-"),
    ("pk-lf-fedcba98765432100000", "pk-lf-"),
    ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "ghp_"),
    ("xoxb-1111111111-2222222222-abcdefghijkl", "xoxb-"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIA"),
    ("glpat-ABCDEFGHIJKLMNOPQRST", "glpat-"),
])
def test_vendor_api_keys_keep_prefix(key, prefix):
    out = scrub_text(f"key={key}")
    assert prefix in out, f"vendor prefix lost: {out!r}"
    assert key not in out
    assert REDACTED in out


def test_private_key_block():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAx7Vd9v1Q\nsecretsecretsecret\n"
           "-----END RSA PRIVATE KEY-----")
    out = scrub_text(f"key material: {pem}")
    assert "secretsecretsecret" not in out
    assert "MIIEowIBAAKCAQEAx7Vd9v1Q" not in out
    assert "PRIVATE KEY BLOCK" in out


def test_truncated_private_key_header():
    out = scrub_text("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk")
    assert "PRIVATE KEY BLOCK" in out


def test_named_secret_kv():
    out = scrub_text('{"password": "hunter2", "host": "db1"}')
    assert "hunter2" not in out and "db1" in out


def test_nested_containers_are_walked():
    payload = {"db": {"uri": "mongodb://u:p4ssw0rd@h:27017/x"},
               "list": ["Bearer abcdefghijklmnop"]}
    out = scrub(payload)
    assert "p4ssw0rd" not in str(out)
    assert "abcdefghijklmnop" not in str(out)
    assert "mongodb://" in out["db"]["uri"]


def test_benign_values_untouched():
    for v in ["qwen3:32b", "chat", 42, 3.14, True, None,
              "http://legion:11434/api/chat", "lab.retrieval.order_tau"]:
        assert scrub(v) == v


def test_scrub_never_raises_on_odd_input():
    class Boom:
        def __repr__(self):
            raise RuntimeError("nope")
    assert scrub(Boom()) is not None or True  # must not propagate
