"""Non-secret execution preferences, separate from encrypted API credentials."""
import json
import threading

from model_connection import DEFAULT_MODEL_CONCURRENCY, MAX_MODEL_CONCURRENCY, validate_concurrency


class RuntimeSettings:
    def __init__(self, directory):
        self.path = directory / "settings.json"
        self.lock = threading.RLock()
        self.concurrency = DEFAULT_MODEL_CONCURRENCY
        self.error = None
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError
                self.concurrency = validate_concurrency(data.get("concurrency"))
            except (ValueError, OSError):
                self.error = "本机并发设置无法读取，已使用默认2路；重新选择并发数即可保存新设置。"

    def public(self):
        with self.lock:
            return {"concurrency": self.concurrency, "max_concurrency": MAX_MODEL_CONCURRENCY}

    def save(self, concurrency):
        value = validate_concurrency(concurrency)
        with self.lock:
            pending = self.path.with_suffix(".pending")
            try:
                pending.write_text(json.dumps({"concurrency": value}), encoding="utf-8")
                pending.replace(self.path)
            except OSError:
                raise ValueError("并发设置未能保存，请确认本地数据目录可写后重新选择。") from None
            self.concurrency, self.error = value, None
            return self.public()
