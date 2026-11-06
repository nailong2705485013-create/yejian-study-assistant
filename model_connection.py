"""OpenAI-compatible Chat Completions profiles. Secrets never serialize."""

import base64
import io
import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from PIL import Image, ImageDraw, ImageFont

DEFAULT_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MODEL_CONCURRENCY = 2
MAX_MODEL_CONCURRENCY = 8


def structured_parameters(config, *, probe=False):
    """Only send provider extensions to the exact official roots/models we know."""
    if (config.base_url in {DEFAULT_URL, DEFAULT_URL + "/v1"}
            and config.model in {"deepseek-flash", "deepseek-v4-pro"}):
        # These models default to high reasoning. A short JSON task must leave
        # room for the answer as reasoning shares the completion token budget.
        return {"reasoning_effort": "none" if probe else "low"}
    return {}


def validate_concurrency(value):
    if type(value) is not int or not 1 <= value <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"模型并发数应为1至{MAX_MODEL_CONCURRENCY}之间的整数。")
    return value


class RequestLimiter:
    """Change admission capacity without replacing a semaphore with active users."""
    def __init__(self, limit=DEFAULT_MODEL_CONCURRENCY):
        self.limit = validate_concurrency(limit)
        self.active = 0
        self.condition = threading.Condition()

    def set_limit(self, limit):
        value = validate_concurrency(limit)
        with self.condition:
            self.limit = value
            self.condition.notify_all()

    def __enter__(self):
        with self.condition:
            while self.active >= self.limit:
                self.condition.wait()
            self.active += 1
        return self

    def __exit__(self, *exc):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()


# Default for direct SDK/lesson use; each web server owns its own shared limiter.
MODEL_REQUEST_SLOTS = RequestLimiter()


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    json_mode: bool = True

    def public(self):
        return {"base_url": self.base_url, "model": self.model, "json_mode": self.json_mode}


def profile(data: dict, *, fallback_key="", require_key=True, require_model=True) -> ModelConfig:
    if not isinstance(data, dict):
        raise ValueError("模型配置必须是对象。")
    url, model, key = data.get("base_url", DEFAULT_URL), data.get("model", DEFAULT_MODEL if require_model else ""), data.get("api_key", "")
    if not all(isinstance(value, str) for value in (url, model, key)):
        raise ValueError("API地址、模型名和Key必须是文字。")
    url = url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("API地址格式无效。") from None
    if (len(url) > 500 or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or any(char.isspace() for char in url)
            or parts.scheme not in {"https", "http"}
            or (parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("请使用HTTPS API根地址（本机服务可用HTTP），不要包含用户名、密码、查询参数或片段。")
    if parts.path.lower().endswith(("/chat/completions", "/models", "/responses")):
        raise ValueError("请填API根地址，例如 https://api.deepseek.com/v1，不要填 /chat/completions 或 /models。")
    model, key = model.strip(), key.strip()
    if (require_model and not model) or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ValueError("请先选择或填写有效模型名，最多200个字符。")
    # Never forward an official-service environment key to a custom endpoint/path.
    if not key and url in {DEFAULT_URL, DEFAULT_URL + "/v1"}:
        key = fallback_key.strip()
    if len(key) > 4096 or any(ord(c) < 32 for c in key):
        raise ValueError("API Key格式无效。")
    if require_key and not key:
        raise ValueError("请在网页输入此服务的API Key；只有DeepSeek官方地址可读取DEEPSEEK_API_KEY。")
    json_mode = data.get("json_mode", True)
    if type(json_mode) is not bool:
        raise ValueError("JSON模式开关无效。")
    return ModelConfig(url, model, key, json_mode)


def transport():
    # A 3xx is an error; credentials must never follow a redirect to another service.
    return httpx.Client(timeout=90, follow_redirects=False)


def connection_error(exc: Exception) -> str:
    if isinstance(exc, APITimeoutError):
        return "模型请求超时；请检查所填服务的网络连接。已开始的请求可能已产生用量。"
    if isinstance(exc, APIConnectionError):
        return "无法连接所填API地址；请检查地址、网络及本机代理设置。DeepSeek官方接口通常可直连。"
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        detail = {401: "Key无效或未授权", 403: "服务拒绝访问，请检查权限或服务区域限制",
                  404: "地址或模型不存在；检查/v1路径和模型名", 429: "余额、额度或限流问题"}.get(status,
                  "服务返回错误；检查接口协议和参数支持情况")
        if 300 <= status < 400:
            detail = "API返回重定向，已阻止转发Key；请直接填写最终API地址"
        return f"模型服务返回HTTP {status}：{detail}。"
    return "模型接口未能完成请求，请检查模型是否支持Chat Completions协议。"


def list_models(config: ModelConfig, *, limiter=None) -> dict:
    with limiter or MODEL_REQUEST_SLOTS, OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0,
                http_client=transport()) as client:
        page = client.models.list()
        models = sorted({item.id.strip() for item in page.data if isinstance(item.id, str) and
                         0 < len(item.id.strip()) <= 200 and not any(ord(char) < 32 for char in item.id)})[:500]
    return {"models": models, "message": "已读取模型列表；仍需分别测试文字/视觉能力。列表缺少时可手动输入模型名。"}


def probe(config: ModelConfig, kind: str, *, limiter=None) -> dict:
    if kind not in {"text", "vision"}:
        raise ValueError("测试类型应为text或vision。")
    expected = "OK"
    content = "连接测试，请只回复OK。"
    if kind == "vision":
        expected = str(secrets.randbelow(9000) + 1000)
        picture = Image.new("RGB", (260, 110), "white")
        draw = ImageDraw.Draw(picture)
        draw.text((25, 20), expected, font=ImageFont.load_default(size=60), fill="black")
        output = io.BytesIO()
        picture.save(output, format="PNG")
        content = [{"type": "text", "text": "只返回图片里的四位数字，不要解释。"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                    base64.b64encode(output.getvalue()).decode("ascii")}}]
    with limiter or MODEL_REQUEST_SLOTS, OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0,
                http_client=transport()) as client:
        response = client.chat.completions.create(model=config.model,
            messages=[{"role": "user", "content": content}], max_tokens=40,
            **structured_parameters(config, probe=True))
    actual = response.choices[0].message.content.strip() if response.choices else ""
    if actual.strip("` .\n") != expected:
        raise ValueError("测试请求已返回，但未通过内容核验；请检查模型能力或换模型。")
    return {"ok": True, "kind": kind, "message": "文字连接测试通过。" if kind == "text" else
            "视觉测试通过：正确读取测试图片中的随机数字。复杂图表仍需对照原图复核。"}
