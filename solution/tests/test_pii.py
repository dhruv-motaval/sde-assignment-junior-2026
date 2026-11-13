import pytest
from cryptography.fernet import InvalidToken

from solution.parsers import parse_phone
from solution.pii import KEY_ENV, Pii, load_master_key

P = Pii(b"master-one")


def test_phone_hash_is_deterministic_and_format_independent():
    forms = ["9876543210", "+91 98765 43210", "09876543210", "91-98765-43210", "(+91) 9876543210"]
    assert len({P.phone_hash(parse_phone(f)) for f in forms}) == 1


def test_phone_hash_differs_per_number_and_per_key():
    a, b = parse_phone("9876543210"), parse_phone("9876543211")
    assert P.phone_hash(a) != P.phone_hash(b)
    assert P.phone_hash(a) != Pii(b"master-two").phone_hash(a)


def test_phone_hash_is_not_a_plain_sha256():
    import hashlib
    a = parse_phone("9876543210")
    assert P.phone_hash(a) != hashlib.sha256(a.encode()).digest()  # a plain hash is precomputable


def test_encrypt_roundtrip_is_randomised_and_key_bound():
    t1, t2 = P.encrypt("Aarav Menon"), P.encrypt("Aarav Menon")
    assert t1 != t2 and "Aarav" not in t1
    assert P.decrypt(t1) == "Aarav Menon" == P.decrypt(t2)
    with pytest.raises(InvalidToken):
        Pii(b"master-two").decrypt(t1)


def test_unicode_name_roundtrip():
    assert P.decrypt(P.encrypt("Kavya Ayyar ₹ ஆ")) == "Kavya Ayyar ₹ ஆ"


def test_last4():
    assert Pii.last4("+919876543210") == "3210"


def test_key_fingerprint_identifies_key_without_revealing_it():
    assert P.key_fingerprint == Pii(b"master-one").key_fingerprint != Pii(b"master-two").key_fingerprint
    assert "master-one".encode().hex() not in P.key_fingerprint


def test_key_file_autocreated_once_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    f = tmp_path / "k"
    k1 = load_master_key(f)
    assert f.exists() and len(k1) == 64
    assert load_master_key(f) == k1                      # stable across calls
    monkeypatch.setenv(KEY_ENV, "from-env")
    assert load_master_key(f) == b"from-env"
