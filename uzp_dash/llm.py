"""LLM-абстракция. Единый интерфейс complete() поверх двух backend'ов.

- open   -> DeepSeek (OpenAI-совместимый API)
- closed -> корпоративный REST (glm-5.1 / Qwen3.5-397b) как в примере пользователя

Код дэшей вызывает только complete() и не знает, в каком он контуре.

Диагностика: метаданные последнего вызова (finish_reason, usage, длины content и
reasoning_content, кусок сырого тела) складываются в LAST_META — по ним видно,
почему модель вернула пустой ответ (обрезка по max_tokens / ушла в размышления).
"""
from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- #
# Настройки вызова (управляются из тетрадки: generate_dashboard(llm_opts={...}))
# --------------------------------------------------------------------------- #
OPTIONS: dict = {
    # Явный потолок ответа: без него шлюз применяет свой дефолт и режет длинный JSON.
    "max_tokens": int(os.environ.get("UZP_LLM_MAX_TOKENS", "4000")),
    # Доп. поля payload как есть — напр. отключение «размышлений» у glm:
    #   {"thinking": {"type": "disabled"}}
    #   {"chat_template_kwargs": {"enable_thinking": False}}
    "extra": {},
    # (connect, read). Генерация длинного JSON у думающей модели > 30 с.
    "timeout": (10, int(os.environ.get("UZP_LLM_READ_TIMEOUT", "120"))),
    # Модель. None -> берётся из .env по контуру (см. _model_for). Задаётся из
    # тетрадки: generate_dashboard(llm_opts={"model": "glm-5.1"}).
    "model": None,
}

# Метаданные последнего вызова — для логов в тетрадке.
LAST_META: dict = {}


def configure(max_tokens: int | None = None, extra: dict | None = None,
              timeout: tuple | int | None = None, model: str | None = None) -> dict:
    """Настроить вызовы LLM из тетрадки. Возвращает актуальные опции."""
    if max_tokens is not None:
        OPTIONS["max_tokens"] = int(max_tokens)
    if extra is not None:
        OPTIONS["extra"] = dict(extra)
    if timeout is not None:
        OPTIONS["timeout"] = timeout
    if model is not None:
        OPTIONS["model"] = str(model).strip() or None
    return dict(OPTIONS)


def _model_for(contour: str, model: str | None) -> str:
    """Какая модель поедет в запрос.

    Приоритет: явный аргумент вызова → опция из тетрадки (llm_opts["model"]) →
    переменная окружения контура → дефолт. Так тетрадка может переопределить .env,
    не трогая окружение.
    """
    if model:
        return model
    if OPTIONS.get("model"):
        return str(OPTIONS["model"])
    if contour == "closed":
        return os.environ.get("UZP_LLM_MODEL", "glm-5.1")
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


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


def _post_json(url: str, headers: dict, payload: dict, retries: int = 1) -> tuple[dict, dict]:
    """POST через сессию. Возвращает (тело ответа, мета вызова).

    urllib3 ретраит connect/5xx, здесь — внешний фолбэк. При недоступной сети
    быстро поднимаем LLMError, чтобы дэш не висел.
    """
    last = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            resp = _session().post(url, headers=headers, json=payload,
                                   timeout=OPTIONS["timeout"])
            elapsed = time.time() - t0
            if not resp.ok:
                raise LLMError(f"LLM {resp.status_code}: {resp.text[:1000]}")
            try:
                data = resp.json()
            except ValueError:
                raise LLMError(f"LLM вернул не JSON ({len(resp.text)} симв.): "
                               f"{resp.text[:500]}")
            return data, {"http_status": resp.status_code, "elapsed": round(elapsed, 1)}
        except (requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout) as ex:
            last = ex
            time.sleep(1.0 * (attempt + 1))
    raise LLMError(f"Сеть недоступна после ретраев: {last}")


def _extract(data: dict, meta: dict) -> str:
    """Достать текст ответа и наполнить LAST_META.

    Учитываем, что «думающие» модели кладут текст в reasoning_content, а content
    оставляют пустым (обычно вместе с finish_reason='length').
    """
    global LAST_META
    if not isinstance(data, dict):
        raise LLMError(f"Неожиданный формат ответа LLM: {type(data).__name__}")
    if data.get("error"):
        raise LLMError(f"LLM error: {json.dumps(data['error'], ensure_ascii=False)[:500]}")

    choices = data.get("choices") or []
    ch = choices[0] if choices else {}
    msg = ch.get("message") or {}
    content = (msg.get("content") or "") if isinstance(msg, dict) else ""
    reasoning = (msg.get("reasoning_content") or "") if isinstance(msg, dict) else ""
    if not content:
        content = ch.get("text") or ""
    usage = data.get("usage") or {}

    LAST_META = {
        **meta,
        "model": data.get("model"),
        "finish_reason": ch.get("finish_reason"),
        "content_len": len(content or ""),
        "reasoning_len": len(reasoning or ""),
        "usage": {k: usage.get(k) for k in
                  ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage},
        "raw_head": json.dumps(data, ensure_ascii=False)[:800],
        "max_tokens": OPTIONS["max_tokens"],
        "extra": OPTIONS["extra"],
    }
    if not content and not choices:
        raise LLMError(f"В ответе LLM нет choices: {LAST_META['raw_head']}")
    return content or ""


def _payload(model: str, prompt: str, temperature: float) -> dict:
    p = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "n": 1,
    }
    if OPTIONS.get("max_tokens"):
        p["max_tokens"] = int(OPTIONS["max_tokens"])
    p.update(OPTIONS.get("extra") or {})
    return p


def _complete_deepseek(prompt: str, model: str | None, temperature: float) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise LLMError("Не задан DEEPSEEK_API_KEY (.env)")
    url = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com").rstrip("/")
    model = _model_for("open", model)
    data, meta = _post_json(
        f"{url}/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        _payload(model, prompt, temperature),
    )
    return _extract(data, meta)


def _complete_corporate(prompt: str, model: str | None, temperature: float) -> str:
    # Закрытый контур: REST как в примере пользователя.
    token = os.environ.get("JPY_API_TOKEN")
    base = os.environ.get("GIGACHAT_API_URL")
    if not token or not base:
        raise LLMError("Не заданы JPY_API_TOKEN / GIGACHAT_API_URL (закрытый контур)")
    model = _model_for("closed", model)
    data, meta = _post_json(
        f"{base.rstrip('/')}/chat/completions",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        _payload(model, prompt, temperature),
    )
    return _extract(data, meta)


def complete(prompt: str, model: str | None = None, temperature: float = 0.2) -> str:
    """Единый вызов LLM. Backend выбирается по контуру (config.CONTOUR)."""
    if config.CONTOUR == "closed":
        return _complete_corporate(prompt, model, temperature)
    return _complete_deepseek(prompt, model, temperature)


# --------------------------------------------------------------------------- #
def llm_check(prompt: str = 'Ответь строго одной строкой JSON: {"ok": 1}',
              contour: str | None = None, model: str | None = None) -> dict:
    """Диагностика LLM: маленький запрос + печать всего, что вернул шлюз.

    Показывает форму ответа (content / reasoning_content), finish_reason и usage —
    по ним понятно, почему на больших батчах приходит пустой content.
    """
    from . import progress
    if not progress.ENABLED:
        progress.enable(verbose=True)
    config.set_contour(contour)
    print(f"Контур: {config.CONTOUR} · max_tokens={OPTIONS['max_tokens']} · "
          f"extra={OPTIONS['extra']} · timeout={OPTIONS['timeout']}")
    try:
        resp = complete(prompt, model=model, temperature=0.0)
    except Exception as ex:
        print(f"ОШИБКА: {type(ex).__name__}: {ex}")
        return {"error": f"{type(ex).__name__}: {ex}", **LAST_META}
    meta = dict(LAST_META)
    print(f"HTTP {meta.get('http_status')} · {meta.get('elapsed')}s · "
          f"model={meta.get('model')} · finish_reason={meta.get('finish_reason')}")
    print(f"content {meta.get('content_len')} симв. · "
          f"reasoning_content {meta.get('reasoning_len')} симв. · usage={meta.get('usage')}")
    print(f"Ответ: {resp[:500]!r}")
    print(f"Сырое тело (первые 800 симв.): {meta.get('raw_head')}")
    path = progress.llm_dump("llm_check", prompt, resp, meta)
    if path:
        print(f"Полный лог: {path}")
    return {"response": resp, **meta}
