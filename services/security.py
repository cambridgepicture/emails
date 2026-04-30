from __future__ import annotations

from cryptography.fernet import Fernet


def get_fernet(key: str) -> Fernet:
    return Fernet(key.encode("utf-8"))


def encrypt_str(fernet: Fernet, value: str) -> bytes:
    return fernet.encrypt(value.encode("utf-8"))


def decrypt_bytes(fernet: Fernet, value: bytes) -> str:
    return fernet.decrypt(value).decode("utf-8")
