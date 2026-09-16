from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings


class MercadoPagoCredentialConfigurationError(RuntimeError):
    pass


class MercadoPagoCredentialDecryptionError(RuntimeError):
    pass


class MercadoPagoCredentialValidationError(ValueError):
    pass


class MercadoPagoCredentialCipher:
    def __init__(self, keys: dict[str, Fernet], active_key_id: str) -> None:
        if not keys:
            raise MercadoPagoCredentialConfigurationError(
                "MERCADO_PAGO_CREDENTIAL_MASTER_KEYS no esta configurado."
            )
        if not active_key_id or active_key_id not in keys:
            raise MercadoPagoCredentialConfigurationError(
                "MERCADO_PAGO_CREDENTIAL_ACTIVE_KEY_ID no identifica una clave disponible."
            )
        self.keys = keys
        self.active_key_id = active_key_id

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "MercadoPagoCredentialCipher":
        settings = settings or get_settings()
        raw = settings.mercado_pago_credential_master_keys
        if not raw:
            raise MercadoPagoCredentialConfigurationError(
                "MERCADO_PAGO_CREDENTIAL_MASTER_KEYS no esta configurado."
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MercadoPagoCredentialConfigurationError(
                "MERCADO_PAGO_CREDENTIAL_MASTER_KEYS debe ser un objeto JSON."
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise MercadoPagoCredentialConfigurationError(
                "MERCADO_PAGO_CREDENTIAL_MASTER_KEYS debe contener al menos una clave."
            )
        try:
            keys = {
                key_id: Fernet(key_value.encode("ascii"))
                for key_id, key_value in parsed.items()
                if isinstance(key_id, str) and isinstance(key_value, str)
            }
        except (TypeError, ValueError) as exc:
            raise MercadoPagoCredentialConfigurationError(
                "El keyring de Mercado Pago contiene una clave Fernet invalida."
            ) from exc
        if len(keys) != len(parsed):
            raise MercadoPagoCredentialConfigurationError(
                "El keyring de Mercado Pago debe mapear identificadores a claves Fernet."
            )
        return cls(keys, settings.mercado_pago_credential_active_key_id or "")

    def encrypt(self, value: str) -> str:
        if not value or not value.strip():
            raise MercadoPagoCredentialValidationError(
                "Los secretos de Mercado Pago no pueden estar vacios."
            )
        return self.keys[self.active_key_id].encrypt(value.strip().encode("utf-8")).decode("ascii")

    def decrypt(self, value: str, key_id: str) -> str:
        cipher = self.keys.get(key_id)
        if cipher is None:
            raise MercadoPagoCredentialDecryptionError(
                "La clave requerida para descifrar Mercado Pago no esta disponible."
            )
        try:
            return cipher.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise MercadoPagoCredentialDecryptionError(
                "No fue posible descifrar las credenciales de Mercado Pago."
            ) from exc
