"""Optional, explicit connection persistence using current-user Windows DPAPI."""
import json
import os
import sys
import threading
from pathlib import Path

from model_connection import ModelConfig, profile

PREFIX = b"YJ-DPAPI-1\x00"


def protect(raw: bytes) -> bytes:
    if sys.platform != "win32":
        raise ValueError("安全保存目前需要Windows用户加密服务。")
    try:
        import win32crypt
        return win32crypt.CryptProtectData(raw, "页间模型连接", None, None, None, 1)
    except Exception:
        raise ValueError("当前服务进程无法使用Windows用户加密服务。请在自己的PowerShell启动本项目后再保存；本次没有写入明文Key。") from None


def unprotect(raw: bytes) -> bytes:
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(raw, None, None, None, 1)[1]
    except Exception:
        raise ValueError("无法解密保存的连接。请使用原Windows用户在自己的PowerShell启动服务，或清除后重新配置。") from None


class CredentialStore:
    def __init__(self, directory: Path):
        self.path = directory / "connections.dpapi"
        self.lock = threading.RLock()

    def read(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {}
            encrypted = self.path.read_bytes()
            if not encrypted.startswith(PREFIX) or len(encrypted) > 32_000:
                raise ValueError("保存的连接文件格式无效，请清除后重新配置。")
            try:
                data = json.loads(unprotect(encrypted[len(PREFIX):]))
                for kind in ("text", "vision"):
                    if data.get(kind) is not None:
                        profile(data[kind])
                return data
            except ValueError:
                raise
            except Exception:
                raise ValueError("保存的连接文件无法读取，请清除后重新配置。") from None

    def public(self) -> dict:
        data = self.read()
        return {kind: (profile(data[kind]).public() | {"has_saved_key": True}) if data.get(kind) else None
                for kind in ("text", "vision")} | {"reuse_text_connection": data.get("reuse_text_connection", False)}

    def resolve(self, data: dict, kind: str, *, fallback_key="", require_model=True) -> ModelConfig:
        candidate = profile(data, require_key=False, require_model=require_model)
        if candidate.api_key:
            return candidate
        saved = self.read().get(kind)
        # Binding includes the full API root path; no key borrowing between services.
        if saved and saved["base_url"] == candidate.base_url:
            return ModelConfig(candidate.base_url, candidate.model, saved["api_key"], candidate.json_mode)
        return profile(data, fallback_key=fallback_key, require_model=require_model)

    def save(self, text: ModelConfig, vision: ModelConfig | None, reuse_text=False):
        data = {"text": text.public() | {"api_key": text.api_key},
                "vision": (vision.public() | {"api_key": vision.api_key}) if vision else None,
                "reuse_text_connection": bool(reuse_text)}
        # Encrypt before opening any file. Failure must not fall back to plaintext.
        encrypted = PREFIX + protect(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        with self.lock:
            pending = self.path.with_suffix(".pending")
            try:
                pending.write_bytes(encrypted)
                os.replace(pending, self.path)
            finally:
                pending.unlink(missing_ok=True)

    def clear(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
