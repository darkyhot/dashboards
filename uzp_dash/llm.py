"""LLM-абстракция. Единый интерфейс complete() поверх двух backend'ов.

- open   -> DeepSeek (OpenAI-совместимый API)
- closed -> корпоративный REST (glm-5.1 / Qwen3.5-397b) как в примере пользователя

Код дэшей вызывает только complete() и не знает, в каком он контуре.
"""
from __future__ import annotations

import os
import time

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

from . import config


class LLMError(RuntimeError):
    pass


_SESSION = None


def _session() -> requests.Session:
    """Общая сессия с пулом соединений и ретраями urllib3 (в т.ч. на connect-reset).
    Второй вызов переиспользует тёплое TLS-соединение — «холодный» первый запрос
    больше не роняет весь генератор."""
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        if Retry is not None:
            retry = Retry(total=2, connect=2, read=1, backoff_factor=0.5,
                          status_forcelist=[429, 500, 502, 503, 504],
                          allowed_methods=frozenset(["POST"]))
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        else:
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION = s
    return _SESSION


def _post_json(url: str, headers: dict, payload: dict, retries: int = 1) -> dict:
    """POST через сессию; urllib3 ретраит connect/5xx, здесь — внешний фолбэк.
    Таймауты подобраны так, чтобы генерация всегда быстро завершалась: при живой
    сети — ответ за секунды, при недоступной — быстрый фолбэк, дэш не виснет."""
    last = None
    for attempt in range(retries):
        try:
            resp = _session().post(url, headers=headers, json=payload, timeout=(8, 30))
            if not resp.ok:
                raise LLMError(f"LLM {resp.status_code}: {resp.text}")
            return resp.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout) as ex:
            last = ex
            time.sleep(1.0 * (attempt + 1))
    raise LLMError(f"Сеть недоступна после ретраев: {last}")


def _complete_deepseek(prompt: str, model: str | None, temperature: float) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError("Не задан DEEPSEEK_API_KEY (.env)")
    url = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com").rstrip("/")
    model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    data = _post_json(
        f"{url}/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "n": 1,
        },
    )
    return data["choices"][0]["message"]["content"]


def _complete_corporate(prompt: str, model: str | None, temperature: float) -> str:
    # Закрытый контур: REST как в примере пользователя.
    token = os.environ.get("JPY_API_TOKEN")
    base = os.environ.get("GIGACHAT_API_URL")
    if not token or not base:
        raise LLMError("Не заданы JPY_API_TOKEN / GIGACHAT_API_URL (закрытый контур)")
    model = model or os.environ.get("UZP_LLM_MODEL", "glm-5.1")
    data = _post_json(
        f"{base.rstrip('/')}/chat/completions",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "n": 1,
            "temperature": temperature,
        },
    )
    return data["choices"][0]["message"]["content"]


def complete(prompt: str, model: str | None = None, temperature: float = 0.2) -> str:
    """Единый вызов LLM. Backend выбирается по контуру (config.CONTOUR)."""
    if config.CONTOUR == "closed":
        return _complete_corporate(prompt, model, temperature)
    return _complete_deepseek(prompt, model, temperature)
