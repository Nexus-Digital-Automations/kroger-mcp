"""Encryption at rest for Kroger OAuth tokens.

Ownership: the only module that holds the token-encryption master key. Used by
``kroger_tokens`` to encrypt/decrypt per-user ``access_token``/``refresh_token``
before they touch the database.

Key management:
- The master key is a Fernet key (32 url-safe base64 bytes → AES-128-CBC + HMAC).
- Source order: ``KROGER_TOKEN_MASTER_KEY`` env var (CI/headless), else the
  macOS Keychain (service ``smart_shopper``, account ``kroger_token_master_key``).
- On a box with neither set, a key is generated once and persisted to the
  Keychain. If it cannot be stored *and* no env key exists, we **raise** —
  never silently fall back to plaintext (that would defeat encryption at rest).

Each machine (the dev Air, the production mini) holds its OWN key, so a leaked
dev key cannot decrypt production tokens.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_KEYCHAIN_SERVICE = "smart_shopper"
_KEYCHAIN_ACCOUNT = "kroger_token_master_key"
_ENV_VAR = "KROGER_TOKEN_MASTER_KEY"

# Per-process cached Fernet built from the resolved master key.
_fernet: Fernet | None = None


class TokenCryptoError(RuntimeError):
    """Raised when the master key cannot be resolved or a token is corrupt."""


def _load_master_key() -> bytes:
    """Resolve the Fernet master key from env or Keychain, generating once.

    Raises:
        TokenCryptoError: if no key is available and none can be persisted.
    """
    env_key = os.environ.get(_ENV_VAR)
    if env_key:
        return env_key.encode()

    try:
        import keyring

        stored = keyring.get_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT)
        if stored:
            return stored.encode()

        # First run on this box: generate and persist to the Keychain.
        new_key = Fernet.generate_key()
        keyring.set_password(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT, new_key.decode())
        logger.info("generated new kroger token master key in keychain")
        return new_key
    except TokenCryptoError:
        raise
    except Exception as exc:
        # Keychain locked/unavailable and no env override → fail loud.
        logger.error("token master key unavailable: %s", exc, exc_info=True)
        raise TokenCryptoError(
            f"No token encryption key: set {_ENV_VAR} or unlock the macOS Keychain"
        ) from exc


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_master_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a token string to url-safe base64 ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt`.

    Raises:
        TokenCryptoError: if the ciphertext is tampered with or was encrypted
            under a different key (e.g. after key rotation) — callers treat
            this as "no usable token" and force re-authentication.
    """
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenCryptoError("token ciphertext is invalid or corrupt") from exc
