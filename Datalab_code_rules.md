---
name: datalab-code-rules
description: Правила написания кода для промышленной платформы DataLab AI (закрытый контур без интернета: Jupyter, Greenplum на ядре 9.4, локальный LLM-шлюз). Используй, когда код пишется на личном компьютере, а работать должен на DataLab с ПРОМ-данными — подключение к БД и LLM, структура тетрадки, обязательные фолбэки, обезличивание, логирование, диалект SQL.
---

# Правила написания кода для DataLab AI

Код пишется **снаружи**, на личном компьютере с ИИ-агентом, а исполняется **внутри**
закрытого контура, на промышленной платформе DataLab AI. Агент промышленных данных не
видит и увидеть не может. Поэтому весь код должен быть написан так, чтобы он завёлся
внутри с первого раза — отладить его там будет негде и некогда.

Документ — исполняемый: примеры взяты из работающего проекта, а не придуманы.

---

## Часть 1. Что есть на платформе и чего нет

### Есть

| Ресурс | Что это значит для кода |
|---|---|
| **Jupyter** с предустановленными библиотеками | Запуск только из `.ipynb`; ставить пакеты на лету нельзя |
| **Greenplum, ядро PostgreSQL 9.4** | SQL пишется под 9.4, а не под современный Postgres |
| **Локальный LLM-шлюз** | OpenAI-совместимый REST + `langchain_gigachat`; лимит **1 запрос в 5 секунд** |
| **Внутреннее зеркало PyPI** | `pip install` работает — из корпоративного хранилища, не из интернета (см. 1.1) |

### Нет

- **Интернета.** Ни `curl`, ни CDN в HTML-артефактах, ни обращений к внешним API.
  Единственное исключение — установка пакетов с внутреннего зеркала PyPI.
- **Возможности спокойно отладиться.** Каждый прогон на проме дорог по времени.
- **SSE в режиме stream.** Шлюз в потоковом режиме отдаёт не SSE — не рассчитывайте
  на построчный разбор потока.

### 1.1. Установка библиотек — с внутреннего зеркала

Пакеты ставятся **второй ячейкой тетрадки**, до всех импортов. Индекс — корпоративный,
не публичный PyPI.

```python
token = "указать токен sberosc"

# Токен получить по ссылке:
#   https://sberosc.ca.sbrf.ru/dashboard/login/?next=/dashboard/profile/
# Логин — цифры, пароль — от DataLab. Сам токен лежит в разделе «Профиль».

!pip install --index-url https://token:{token}@sberosc.ca.sbrf.ru/repo/pypi/simple -r requirements.txt
```

**`requirements.txt` обязан лежать в одной папке с `.ipynb`** — путь в команде
относительный, из другой папки установка не найдёт файл.

**Токен — секрет.** В git он попасть не должен: либо подставляется руками в ячейку уже
внутри контура, либо читается из `.env`. Ни в коде, ни в коммите его быть не может.

### Два контура и что между ними ездит

```
ЗАКРЫТЫЙ (DataLab AI)                 ОТКРЫТЫЙ (личный компьютер)
  БД с ПРОМ-данными                     агент + docker Postgres
        │                                        ▲
        │ профиль данных                         │
        │ (структура, статистика,                │
        │  чувствительные поля пусты)            │
        └────────────────────────────────────────┘
        ▲                                        │
        │              код (git → почта)         │
        └────────────────────────────────────────┘
```

**Наружу уходит структура, а не данные.** Внутрь приезжает код. Синтетическая база
снаружи повторяет промышленную схему точь-в-точь, поэтому SQL переносится буквально.

---

## Часть 2. Подключение к LLM

### 2.1. Ограничения шлюза

- **1 запрос в 5 секунд.** Дроссель обязателен в любом коде, который делает больше
  одного вызова. Без него батчевая обработка упирается в лимит на первом же прогоне.
- **Stream отдаёт не SSE.**

### 2.2. Список доступных моделей

Показывать его в **третьей ячейке тетрадки** (правило 2), сразу после установки
пакетов: состав моделей меняется, и захардкоженное имя однажды перестанет существовать.

```python
from langchain_gigachat.chat_models import GigaChat
import os

llm = GigaChat(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
    model="GigaChat-2",
)

[model.id_ for model in llm.get_models().data]
```

Ориентировочный состав (**меняется, проверяйте вызовом выше**): `GigaChat`,
`GigaChat-2`, `GigaChat-2-Max`, `GigaChat-2-Pro`, `GigaChat-3-Pro`, `GigaChat-3-Ultra`,
`GigaChat-Max`, `GigaChat-Pro`, `Gemma-4-26b`, `Qwen3.5-397b`, `glm-5.2`,
`Embeddings`, `Embeddings-2`, `EmbeddingsGigaR`, `GigaEmbeddings-3B-2025-09`,
`SaluteEmbeddings`.

### 2.3. Основной путь: langchain с учётом rate limiter

Это рекомендуемый способ: дроссель уже встроен в класс.

```python
from langchain_gigachat.chat_models import GigaChat
from langchain_gigachat.embeddings import GigaChatEmbeddings
from langchain_gigachat.chat_models.gigachat import trim_content_to_stop_sequence
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from time import perf_counter, sleep
from typing import Any, List, Optional

GIGA_DELAY = 6          # с запасом к лимиту «1 запрос в 5 секунд»
GIGA_LAST_INVOKE = 0.0  # общий на процесс момент последнего вызова


def _throttle() -> None:
    """Выдержать паузу между вызовами шлюза.

    Счётчик ОБЩИЙ для всех вызовов процесса: и чат, и эмбеддинги ходят в один шлюз,
    и раздельные счётчики лимит не удержат.
    """
    global GIGA_LAST_INVOKE
    waited = perf_counter() - GIGA_LAST_INVOKE
    if waited < GIGA_DELAY:
        sleep(GIGA_DELAY - waited)
    GIGA_LAST_INVOKE = perf_counter()


class GigaChatDelayed(GigaChat):
    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = None,
        **kwargs: Any,
    ) -> ChatResult:
        _throttle()

        should_stream = stream if stream is not None else self.streaming
        if should_stream:
            stream_iter = self._stream(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            return generate_from_stream(stream_iter)

        payload = self._build_payload(messages, **kwargs)
        response = self._client.chat(payload)
        for choice in response.choices:
            trimmed_content = trim_content_to_stop_sequence(choice.message.content, stop)
            if isinstance(trimmed_content, str):
                choice.message.content = trimmed_content
                break

        return self._create_chat_result(response)


MAX_BATCH_SIZE_CHARS = 1_000_000
MAX_BATCH_SIZE_PARTS = 90


class GigaChatEmbeddingsDelayed(GigaChatEmbeddings):
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        result: List[List[float]] = []
        size = 0
        local_texts: List[str] = []
        embed_kwargs = {}

        _throttle()

        if self.model is not None:
            embed_kwargs["model"] = self.model

        for text in texts:
            local_texts.append(text)
            size += len(text)
            if size > MAX_BATCH_SIZE_CHARS or len(local_texts) > MAX_BATCH_SIZE_PARTS:
                for embedding in self._client.embeddings(
                    texts=local_texts, **embed_kwargs
                ).data:
                    result.append(embedding.embedding)
                size = 0
                local_texts = []

        if local_texts:
            for embedding in self._client.embeddings(
                texts=local_texts, **embed_kwargs
            ).data:
                result.append(embedding.embedding)

        return result
```

Простой вызов:

```python
import os

llm = GigaChatDelayed(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
    model="glm-5.2",
)

print(llm.invoke('Как дела?').content)
```

Векторизация:

```python
import os

embedder = GigaChatEmbeddingsDelayed(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
)

vectors = embedder.embed_documents(['Как дела?'])
print(vectors[0][:5])
```

### 2.4. Структурированный вывод

Надёжнее, чем просить модель «верни JSON» и потом его чинить.

```python
import os
from typing import List, Optional
from pydantic import BaseModel, Field

llm = GigaChatDelayed(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
    model="glm-5.2",
)


class PersonInfo(BaseModel):
    """Модель для извлечения информации о человеке."""
    name: str = Field(..., description="Имя человека")
    age: Optional[int] = Field(None, description="Возраст")
    city: Optional[str] = Field(None, description="Город проживания")
    interests: List[str] = Field(default_factory=list, description="Список увлечений")


structured_llm = llm.with_structured_output(PersonInfo)
result = structured_llm.invoke(
    "Ивану 35 лет, он живёт в Москве и увлекается горными лыжами и программированием."
)
print(result)
```

### 2.5. Агент с инструментами

```python
import os
from pprint import pprint
from langchain.tools import tool
from langgraph.prebuilt import create_react_agent

llm = GigaChatDelayed(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
    model="glm-5.2",
)


@tool
def calculator(expression: str) -> str:
    """Вычисляет математическое выражение, например: '2 + 3 * 4'"""
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"Ошибка вычисления: {e}"


agent_executor = create_react_agent(model=llm, tools=[calculator])
messages = agent_executor.invoke({'messages': ['Сколько будет 52 умножить на 48?']})['messages']
pprint(messages)
print(messages[-1].content)
```

### 2.6. Сырой REST — для диагностики и нестандартных полей payload

Пригождается, когда нужно передать поля, которых нет в обёртке (например, отключить
«размышления» у думающей модели). **Дроссель здесь нужно поставить руками** — в отличие
от `GigaChatDelayed`, класс за вас этого не сделает.

```python
import json
import os
import requests


HEADERS = {
    "Authorization": f"Bearer {os.getenv('JPY_API_TOKEN')}",
    "Content-Type": "application/json",
}


def completions(query: str, model: str = "glm-5.2", timeout=(10, 120)):
    _throttle()                       # обязательно: лимит 1 запрос в 5 секунд
    data = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "n": 1,
        "temperature": 0.01,
    }
    response = requests.post(
        url=os.getenv('GIGACHAT_API_URL') + "/chat/completions",
        headers=HEADERS,
        json=data,
        timeout=timeout,
    )
    if response.ok:
        return json.dumps(response.json(), indent=4, ensure_ascii=False)
    return response.text


print(completions('Как дела?'))
```

### 2.7. Единая точка вызова

Весь прикладной код должен ходить в LLM **через одну функцию**. Тогда смена модели,
контура или добавление логирования — правка в одном месте, а не в тридцати.

```python
OPTIONS = {
    "max_tokens": 4000,   # без явного потолка шлюз режет длинный ответ своим дефолтом
    "extra": {},          # доп. поля payload: {"thinking": {"type": "disabled"}}
    "timeout": (10, 120), # (connect, read); думающая модель отвечает дольше 30 с
    "model": None,        # задаётся из тетрадки
}

LAST_META: dict = {}      # диагностика последнего вызова


def model_for(model: str | None = None) -> str:
    """Приоритет: аргумент вызова → параметр тетрадки → переменная окружения → дефолт."""
    if model:
        return model
    if OPTIONS.get("model"):
        return str(OPTIONS["model"])
    return os.environ.get("DATALAB_LLM_MODEL", "glm-5.2")
```

Обязательно сохранять метаданные ответа — `finish_reason`, `usage`, длину `content` и
`reasoning_content`. Без них пустой ответ невозможно объяснить:

- `finish_reason='length'` → ответ обрезан, поднимите `max_tokens` или уменьшите батч;
- `content` пуст, а `reasoning_content` большой → модель ушла в размышления,
  отключите их через `extra`.

---

## Часть 3. Подключение к БД

### 3.1. Единая точка доступа

```python
from functools import lru_cache

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

SCHEMA = "имя_витринной_схемы"        # задаётся параметром тетрадки
SQL_TIMEOUT_MS = 10 * 60 * 1000       # правило 13: 10 минут


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    """SQLAlchemy engine (кэшируется по URL).

    pool_pre_ping — соединение проверяется перед выдачей: длинная сессия тетрадки
    переживает разрыв на стороне сервера, а не падает на первом же запросе.
    statement_timeout — сервер сам снимет запрос, который висит дольше лимита.
    """
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": f"-c statement_timeout={SQL_TIMEOUT_MS}"},
    )


def read_sql(engine: Engine, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Выполнить SELECT и вернуть DataFrame.

    В SQL используйте плейсхолдер {schema} (подставляется автоматически) и именованные
    параметры :name — безопасная подстановка значений вместо конкатенации строк.
    """
    sql = sql.format(schema=SCHEMA)
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params or {})
    except Exception as ex:
        raise_if_kerberos(ex)
        raise
```

### 3.2. Kerberos: остановиться и сказать, что делать

Протухший тикет — самая частая причина отказа подключения, и по умолчанию она выглядит
как невнятная ошибка драйвера. Программа должна остановиться с понятным текстом
(правило 10).

```python
_KERBEROS_MARKERS = (
    "kerberos", "gssapi", "gss_", "krb5", "ticket expired",
    "credentials cache", "no credentials", "server not found in kerberos database",
)


class KerberosTicketError(RuntimeError):
    pass


def raise_if_kerberos(ex: Exception) -> None:
    """Распознать отказ по Kerberos и остановить работу с инструкцией.

    Останавливаемся намеренно: продолжать бессмысленно — ни один следующий запрос не
    пройдёт, а сыпать одинаковыми ошибками в тетрадку только мешает.
    """
    text_ = f"{type(ex).__name__}: {ex}".lower()
    if any(m in text_ for m in _KERBEROS_MARKERS):
        print("=" * 70)
        print("ОСТАНОВЛЕНО: недействительный или отсутствующий Kerberos ticket.")
        print("Обновите Kerberos ticket — выполните `kinit` в консоли,")
        print("затем перезапустите ячейку.")
        print("=" * 70, flush=True)
        raise KerberosTicketError("Обновите Kerberos ticket: выполните kinit в консоли") from ex


def ping(engine: Engine) -> bool:
    """Проверка соединения — первое, что запускается в тетрадке."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as ex:
        raise_if_kerberos(ex)
        raise
```

### 3.3. Сколько данных можно тянуть в pandas

Оперативной памяти 120 ГБ, и её реально не хватает (правило 12).

```python
MAX_ROWS = 1_000_000          # больше в память не тянем
MAX_TEXT_LEN = 200            # длиннее — не грузим, агрегируем в БД


def guard_rows(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Не дать выборке съесть память ядра.

    Обрезать молча нельзя: тихо усечённая выборка даёт неверные итоги, которые
    выглядят правдоподобно. Поэтому — исключение с указанием, что делать.
    """
    if len(df) > MAX_ROWS:
        raise MemoryError(
            f"{name}: {len(df):,} строк — больше лимита {MAX_ROWS:,}. "
            f"Агрегируйте на стороне БД или сузьте период."
        )
    return df
```

**Правило по текстовым полям:** длинные `text`/`varchar` не выгружаются вместе с
основной выборкой. Если нужен признак «текст есть» — считайте его в SQL:

```sql
bool_or(COALESCE(btrim(comment_field), '') <> '') AS has_text
```

Тексты забираются отдельным узким запросом и только по тем строкам, где они
действительно нужны.

---

## Часть 4. Открытый контур: отладка без промышленных данных

### 4.1. Профиль данных → docker Postgres → синтетика

Правило 8: если дан профиль в JSON — поднимается локальная база и наполняется
синтетикой по этому профилю.

```yaml
# docker-compose.yml
services:
  local_pg:
    image: postgres:16
    container_name: local_pg
    environment:
      POSTGRES_USER: dev
      POSTGRES_PASSWORD: dev
      POSTGRES_DB: dev
    ports:
      - "55433:5432"      # нестандартный порт: 5432 обычно уже занят
    volumes:
      - local_pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dev -d dev"]
      interval: 3s
      timeout: 3s
      retries: 20

volumes:
  local_pg_data:
```

```bash
docker compose up -d          # если docker не установлен — поставить
python3 -m synth              # создать схему и наполнить синтетикой
```

### 4.2. Главное правило синтетики

> **Схема и имена таблиц локально повторяют промышленные точь-в-точь.**
> Тогда SQL переносится без единой правки, и в коде нет ветвлений «если пром».

Из профиля берутся: типы, доли непустых, число уникальных, границы, категории.
Чувствительные поля в профиле замаскированы — значения генерируются свои.

### 4.3. Синтетика должна воспроизводить бизнес-инварианты

Случайные числа проверяют только то, что код не падает. Чтобы проверить логику, в
синтетике должна встретиться **каждая ветка алгоритма**: сезонный клиент, клиент с
оттоком два месяца подряд, клиент, вернувшийся после оттока, частичные данные текущего
месяца, дубль ключа. Ветка, которой нет в синтетике, поедет на пром непроверенной.

### 4.4. Отладка LLM-логики снаружи

Снаружи локального шлюза нет, поэтому используется внешняя модель с
OpenAI-совместимым API (например DeepSeek).

> **Агент обязан спросить об этом пользователя в начале работы**, а не решать сам:
>
> «Будете ли вы тестировать вызовы LLM в открытом контуре? Если да — нужен ключ API
> внешней модели (например DeepSeek); положите его в `.env` как `DEEPSEEK_API_KEY`.
> Если нет — LLM-часть останется непроверенной до переноса на пром, и это будет
> единственный кусок кода, который поедет туда вслепую.»
>
> Вопрос не формальный. Без ключа нельзя проверить ни разбор ответа, ни фолбэки, ни
> деградацию батчей — а это как раз то, что чаще всего ломается на проме. Если
> пользователь отказывается, скажите прямо, что LLM-путь не покрыт проверкой, и
> не изображайте обратное.

Прикладной код о выборе backend не знает — он вызывает ту же `complete()`:

```python
def complete(prompt: str, model: str | None = None, temperature: float = 0.2) -> str:
    """Единый вызов LLM. Backend выбирается по контуру, прикладной код о нём не знает."""
    if CONTOUR == "closed":
        return _complete_datalab(prompt, model, temperature)   # локальный шлюз
    return _complete_external(prompt, model, temperature)      # отладочная модель
```

Различие контуров живёт **только** в этой функции и в строке подключения к БД.

---

## Часть 5. Правила написания кода

Правила 1–17 — обязательные требования платформы и заказчика.
Правила 18–25 — дополнения, собранные из ошибок, которые уже были допущены и стоили
времени; они не отменяют первые семнадцать, а закрывают то, о чём те молчат.

### 1. Запуск — всегда из `.ipynb`

Тетрадка содержит **только запуск и параметры**. Вся логика — в `.py`-модулях рядом.
Код в ячейках невозможно ни отревьюить, ни переиспользовать, ни продиагностировать.

`requirements.txt` лежит **в одной папке с тетрадкой** — иначе установка пакетов
(ячейка 2) не найдёт файл.

### 2. Порядок ячеек фиксирован

| Ячейка | Что в ней | Зачем именно здесь |
|---|---|---|
| 1 | Короткое описание: что считает код и что будет на выходе | Открывший тетрадку понимает её назначение, не читая код |
| 2 | Установка библиотек с внутреннего зеркала (см. 1.1) | До любых импортов — иначе следующая ячейка упадёт на `ImportError` |
| 3 | Список доступных моделей через `llm.get_models()` (см. 2.2) | Состав моделей меняется; захардкоженное имя однажды перестанет существовать, и узнать об этом надо в начале, а не в середине трёхчасового прогона |
| 4 | Параметры и запуск | См. правила 3–4 |

### 3–4. Все параметры — в ячейке запуска

Параметры LLM (модель, timeout, потолок ответа) и параметры подключения к БД задаются
**в тетрадке**, а не в коде. Ни одного значения, которое приходится править в `.py`,
чтобы поменять модель или период.

### 5. Обращение к LLM — всегда с фолбэком

Ни один сбой LLM не должен ронять прогон. Ответ не пришёл, не разобрался, пришёл
пустой — раздел заполняется расчётом по правилам, и в прогресс пишется, что это фолбэк.

```python
def ask(prompt: str, label: str) -> dict:
    """Один вызов LLM. При любой ошибке возвращает пустой результат, а не исключение."""
    try:
        raw = complete(prompt, temperature=0.1)
    except Exception as ex:
        log_error(label, f"{type(ex).__name__}: {ex}")
        dump(label, prompt, f"<ошибка> {ex}", LAST_META)   # правило 11
        return {}

    data = parse_items(raw)
    dump(label, prompt, raw, {**LAST_META, "parsed": len(data)})
    if not data:
        log_error(label, "не удалось разобрать ни одного объекта из ответа")
        if LAST_META.get("finish_reason") == "length":
            log_error(label, "ответ обрезан → поднимите max_tokens или уменьшите батч")
        if LAST_META.get("content_len") == 0 and LAST_META.get("reasoning_len"):
            log_error(label, "модель ответила только размышлениями → отключите thinking")
    return data
```

### 6. Потоковая отправка батчами — с деградацией размера

При неполном покрытии батч дробится и непокрытые элементы отправляются заново меньшими
порциями. Слишком длинный промпт делится **до** вызова, не тратя лимит.

```python
BATCH_STEPS = (30, 10, 3, 1)     # лестница деградации размера
PROMPT_CHAR_LIMIT = 40_000       # больше в один запрос не отправляем
ZERO_STREAK_ABORT = 3            # столько пустых ответов подряд = шлюз недоступен


def split(chunk: list, size: int) -> list[list]:
    return [chunk[i:i + size] for i in range(0, len(chunk), size)]


def next_size(n: int) -> int:
    """Следующий (меньший) размер батча из лестницы деградации."""
    for s in BATCH_STEPS:
        if s < n:
            return s
    return max(1, n // 2)


def process(items: list, batch: int, max_calls: int) -> dict:
    result: dict = {}
    queue = split(items, max(1, batch))
    calls = 0
    zero_streak = 0
    total = len(items)

    while queue:
        chunk = queue.pop(0)

        # слишком объёмный промпт делим ЗАРАНЕЕ, не тратя вызов
        while len(chunk) > 1 and len(build_prompt(chunk)) > PROMPT_CHAR_LIMIT:
            size = next_size(len(chunk))
            parts = split(chunk, size)
            print(f"  промпт великоват → дроблю на {len(parts)} по {size}", flush=True)
            chunk, queue = parts[0], parts[1:] + queue

        if calls >= max_calls:
            left = len(chunk) + sum(len(c) for c in queue)
            print(f"  исчерпан бюджет вызовов ({max_calls}) — "
                  f"остаток {left} уйдёт в фолбэк на правила", flush=True)
            break

        label = f"батч {calls + 1} · {len(chunk)} из {total}"
        got = ask(build_prompt(chunk), label)
        calls += 1
        result.update(got)

        # несколько пустых ответов подряд — проблема системная (шлюз/модель),
        # дробить дальше бессмысленно и дорого по времени
        zero_streak = 0 if got else zero_streak + 1
        if zero_streak >= ZERO_STREAK_ABORT:
            print(f"  {zero_streak} вызова подряд без ответа — прекращаю, "
                  f"остаток уйдёт в фолбэк", flush=True)
            break

        missing = [o for o in chunk if key(o) not in got]
        if missing and len(chunk) > 1:
            size = next_size(len(chunk))
            print(f"  покрыто {len(got)} из {len(chunk)} — "
                  f"повтор {len(missing)} батчами по {size}", flush=True)
            queue = split(missing, size) + queue

    return result
```

### 7. SQL — под Greenplum на ядре PostgreSQL 9.4

Это самое частое место, где код, работающий локально, падает на проме.

**Не работает** (появилось в 9.5 и позже):

```sql
make_interval(months => n)      -- ОШИБКА: column "months" does not exist
```

Ядро 9.4 разбирает `=>` как оператор. Правильно — умножение интервала:

```sql
n * interval '1 month'

-- сдвиг на N месяцев и переход к концу месяца
CAST(base_date + n * interval '1 month'
     + interval '1 month' - interval '1 day' AS date)
```

**Работает и проверено:** `FILTER (WHERE …)` у агрегатов, оконные функции,
`date_trunc('month', dt)`, `CAST(… AS date)`, CTE через `WITH`, `bool_or`,
`count(DISTINCT …)`.

**Правила:**
- сомневаетесь в конструкции — проверьте её отдельным маленьким запросом до того,
  как встроите в большой;
- нашли диалектную ловушку — **запишите причину комментарием прямо в коде**, иначе
  при следующей правке конструкция вернётся;
- агрегируйте на стороне БД: то, что можно свернуть в SQL, не должно ехать в pandas.

### 8. Профиль данных → docker Postgres

См. часть 4. Если docker не установлен — поставить; если установлен, но нет
Postgres — поднять контейнер.

### 9. Прогресс выполнения — в консоль, всегда

Прогон идёт долго, и молчащая программа неотличима от зависшей.

```python
import sys
import time

_T0 = time.time()


def _ts() -> str:
    return f"{time.time() - _T0:5.1f}s"


def step(msg: str) -> None:
    print(f"[{_ts()}] → {msg}", flush=True, file=sys.stdout)


def done(msg: str) -> None:
    print(f"[{_ts()}]   ✓ {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[{_ts()}]   ⚠ {msg}", flush=True)
```

`flush=True` обязателен: без него вывод копится в буфере и появляется в тетрадке
пачкой в самом конце.

Для потоковых запросов к LLM показывать **сколько обработано и сколько осталось**:

```
[ 12.4s]   → LLM [батч 3 · 12 орг из 340]: запрос 18 204 симв.
[ 25.1s]   ✓ LLM [батч 3 · 12 орг из 340]: ответ 1 266 симв. · finish_reason=stop
```

Печатать также итоги шага: сколько строк пришло, сколько отфильтровано, сколько ушло
в фолбэк. Молчаливая потеря строк — худший вид ошибки.

### 10. Kerberos — перехват и остановка

См. 3.2. Ошибка распознаётся, работа останавливается, в тетрадку печатается
«обновите Kerberos ticket, выполните `kinit` в консоли».

### 11. Логировать каждый запрос к LLM в отдельную папку

Без логов инцидент не расследовать: в тетрадке видно только выжимку.

```python
import re
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("output/llm_logs")
_seq = 0


def dump(label: str, prompt: str, response: str, meta: dict | None = None) -> str | None:
    """Записать полный запрос/ответ в файл. Возвращает путь.

    Логи не должны ронять прогон: любая ошибка записи гасится с предупреждением.
    """
    global _seq
    _seq += 1
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w.-]+", "_", label)[:60]
        path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{_seq:03d}_{slug}.txt"
        path.write_text(
            f"=== META ===\n{meta}\n\n"
            f"=== REQUEST ({len(prompt or '')} симв.) ===\n{prompt}\n\n"
            f"=== RESPONSE ({len(response or '')} симв.) ===\n{response}\n",
            encoding="utf-8",
        )
        return str(path)
    except Exception as ex:
        warn(f"не удалось записать лог LLM: {type(ex).__name__}: {ex}")
        return None
```

Логируются и **неудачные** вызовы — именно они интересны при разборе.

### 12. Лимит выгрузки в pandas

Не больше 1 млн строк; длинные `varchar`/`text` не тянуть. См. 3.3.

### 13. Timeout на SQL — 10 минут

Задаётся на уровне engine через `statement_timeout` (см. 3.1), чтобы сервер сам снял
зависший запрос. Полагаться только на клиентский таймаут нельзя: соединение отвалится,
а запрос продолжит занимать ресурсы базы.

### 14. Сложные артефакты — без прямых идентификаторов организаций

Если код делает не `txt`/`csv`/`excel`, а сложный артефакт (например, HTML-дэшборд),
слово-идентификатор организации заменяется на «Орг.» — иначе файл не проходит по почте.

> **Подставьте реальное слово вместо `<СЛОВО>`.** В самом этом документе оно не
> написано намеренно: документ едет по той же почте, и слово-блокатор заблокировало бы
> и его. Это трёхбуквенное сокращение идентификатора налогоплательщика.

```python
import re

# Меняем ТОЛЬКО отдельное слово: иначе пострадают слова, где эти буквы внутри
# («длинный», «старинный» и подобные). Латинские идентификаторы в коде не трогаем.
_WORD = re.compile(r"(?<![А-Яа-яЁёA-Za-z])<СЛОВО>(?![А-Яа-яЁёA-Za-z])", re.IGNORECASE)


def sanitize(text: str) -> str:
    """Убрать из готового текста слово-блокатор (в т.ч. пришедшее из ответа LLM)."""
    return _WORD.sub("Орг.", text or "")
```

Применяется к **готовому** документу целиком, последним шагом — тогда под замену
попадает и то, что пришло из БД, и то, что сочинила модель.

### 15. Обезличивание: защита от ошибки blacklist

Шлюз блокирует запрос, если в промпте встречаются чувствительные слова — например
названия отдельных регионов. Поэтому названия организаций и география **в LLM не
уходят вовсе**: подставляются нейтральные токены, реальные имена возвращаются в ответ
уже на нашей стороне.

```python
import re

_SEP = r"[\s\-–—_]*"     # модель может «переписать» токен: «X-01», «X 1», «X—01»


class Aliases:
    """Двусторонний словарь «настоящее имя ↔ токен» на один вызов LLM."""

    def __init__(self) -> None:
        self._by_name: dict[tuple[str, str], str] = {}
        self._by_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def alias(self, kind: str, name) -> str:
        """Токен для имени. Повторный вызов даёт тот же токен."""
        name = (str(name) if name is not None else "").strip()
        if not name:
            return name
        key = (kind, name)
        if key not in self._by_name:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"{kind}-{self._counters[kind]:02d}"
            self._by_name[key] = token
            self._by_token[token] = name
        return self._by_name[key]

    def restore(self, text: str) -> str:
        """Вернуть настоящие имена в текст ответа LLM."""
        if not text or not self._by_token:
            return text
        # от длинных номеров к коротким: иначе «X-1» съест начало «X-12»
        for token in sorted(self._by_token, key=len, reverse=True):
            kind, _, num = token.rpartition("-")
            pattern = re.compile(rf"{re.escape(kind)}{_SEP}0*{int(num)}(?!\d)")
            name = self._by_token[token]
            text = pattern.sub(lambda _m, n=name: n, text)
        return text
```

При отказе по blacklist — печатать в тетрадку текст запроса, иначе причину не найти:

```python
if "blacklist" in str(ex).lower():
    print(f"Запрос упал с ошибкой blacklist. Текст запроса: {prompt[:2000]}", flush=True)
```

**Проверяйте обезличивание отдельно:** прогоните готовый промпт регуляркой по списку
имён из справочника — ни одно не должно найтись.

### 16. Интернета нет

Ни `curl`, ни `requests` наружу, ни шрифтов и скриптов с CDN в HTML-артефактах.
Артефакт должен открываться на машине без сети: весь CSS и JS — инлайн, картинки —
data-URI.

**Единственное исключение — установка пакетов** с внутреннего зеркала PyPI (см. 1.1),
и только в ячейке 2, до начала работы. Ставить пакеты во время прогона нельзя: это
скрытая зависимость, которой не будет видно при переносе.

### 17. Логика расчётов — в `methodology.md`

Любой нетривиальный расчёт описывается в отдельном документе: формула, SQL, пример с
числами и способ воспроизвести. Отдельным разделом — **известные расхождения и
ограничения**.

> Отчёт, в котором нельзя проверить число, — это презентация, а не аналитика.

---

## Дополнения

### 18. Дроссель обязателен

Лимит 1 запрос в 5 секунд. Счётчик времени последнего вызова — **общий на процесс**
(см. `_throttle` в 2.3): чат и эмбеддинги ходят в один шлюз, раздельные счётчики лимит
не удержат. Класс `GigaChatDelayed` закрывает это сам; сырой REST — нет.

### 19. Паритет контуров

Ни одного `if контур == …` в коде запросов и расчётов. Различие живёт только в строке
подключения к БД и в выборе backend LLM. Иначе каждая правка требует двух проверок, а
вторую, на проме, сделать негде.

### 20. Числа проверяются скриптом, а не по памяти

Реальный случай: в методологию попали «точные» значения, взятые из округлённого вывода
консоли — цифры после запятой были попросту выдуманы. Любое число в документации
получается запуском кода и сверяется скриптом.

### 21. Ловушки pandas, на которых уже падали

```python
# ЛОВУШКА 1: у отсутствующей колонки .get() возвращает СКАЛЯР nan,
# и следующий .fillna() падает с AttributeError
def num(df, col, default=0.0):
    """Числовая колонка или колонка из значений по умолчанию — всегда Series."""
    if col in df:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


# ЛОВУШКА 2: колонка-список после merge даёт NaN, а `nan or []` ИСТИННО —
# проверка на пустоту его пропускает, и list(nan) падает уже дальше по коду
df["items"] = [v if isinstance(v, list) else [] for v in df.get("items", pd.Series(dtype=object))]
```

Обе стоили этому проекту падений уже после того, как код «работал».

### 22. Отчётный период — явный параметр

Автовыбор «последний месяц в витрине» тихо сдвигает отчёт: запуск 3-го числа возьмёт
уже начавшийся месяц вместо предыдущего. Месяц отчёта и текущая дата задаются
параметрами тетрадки; значение по умолчанию сопровождается предупреждением в комментарии.

Различайте **две разные меры времени**: «сколько данных мы уже наблюдаем» (из витрины)
и «сколько календарных дней осталось» (из реальной даты). Их легко перепутать, и
ошибка не видна в результате.

### 23. Размер артефакта — проектное ограничение

HTML-отчёт с десятками тысяч строк в JSON вырастает до десятков мегабайт. Что помогает:

- **порог материальности** — не класть в файл то, что никто не откроет;
- **словарная упаковка** вместо списка объектов: повторяющиеся строковые значения
  выносятся в словарь, строка становится массивом индексов. На реальном блоке это дало
  −64% без потери функциональности;
- отсечение сопровождается **честной подписью**: «показано N из M».

### 24. Бюджет вызовов LLM — на единицу разбора

Потолок вызовов задаётся на каждую единицу (подразделение, регион, отчёт), а не на весь
прогон. Иначе первая же единица съедает лимит, и остальные остаются без обработки.
Счётчик должен обнуляться на каждой единице — проверьте это явно.

Отдельно: если шлюз не ответил **ни разу** за целую единицу — это его отказ, а не
свойство данных. Ставьте защёлку и пропускайте LLM у следующих единиц, иначе каждая
потратит свои неудачные вызовы с полным таймаутом.

### 25. Сходимость проверяется в коде

Сумма частей обязана совпадать с итогом; группы обязаны покрывать блок целиком.
Проверка стоит в коде, а расхождение печатается в прогресс — не прячется.

```python
residual = base - outflow + inflow + pipeline - forecast
if abs(residual) > 0.5:
    warn(f"водопад не сходится: невязка {residual:+.2f} — часть эффекта потеряна")
```

---

## Часть 6. Чек-лист переноса на пром

**До отправки (снаружи):**

- [ ] код прогнан на синтетике, все ветки алгоритма задействованы;
- [ ] уточнено у пользователя, тестируются ли вызовы LLM (см. 4.4); если да — они
      проверены на внешней модели, если нет — это зафиксировано как непокрытая часть;
- [ ] в коде нет ветвлений «если пром»;
- [ ] SQL проверен на конструкции 9.5+ (`=>` и прочее);
- [ ] нет обращений в интернет и внешних CDN в артефактах;
- [ ] **`requirements.txt` лежит рядом с тетрадкой** и содержит всё, что импортируется;
- [ ] секреты не в коде и не в тетрадке — только в `.env`;
- [ ] `methodology.md` обновлён, числа сверены скриптом.

**Первый запуск (внутри):**

- [ ] `.env` заполнен: `GIGACHAT_API_URL`, `JPY_API_TOKEN`, строка подключения к БД;
- [ ] ячейка 2 отработала: пакеты поставились с внутреннего зеркала, токен подставлен;
- [ ] `kinit` выполнен;
- [ ] `ping(engine)` — соединение с БД живо;
- [ ] ячейка 3 показала список моделей, выбранная модель в нём есть;
- [ ] диагностический вызов LLM вернул ответ (проверить `finish_reason` и `usage`);
- [ ] прогон на **одной** единице разбора — и только потом на всех;
- [ ] проверить `output/llm_logs/` — логи пишутся;
- [ ] проверить размер артефакта и что он открывается без сети.

---

## Часть 7. Шаблон тетрадки

### Ячейка 1 — что делает код

```python
# ==================== ЧТО ЭТО ====================
# <Одно-два предложения: что считает код и что получается на выходе.>
#
# Источники: <таблицы БД>
# Результат:  <файл/файлы в output/>
# Время прогона: <ориентир>
```

### Ячейка 2 — установка библиотек

```python
# ============ БИБЛИОТЕКИ (внутреннее зеркало PyPI) ============
# requirements.txt должен лежать В ОДНОЙ ПАПКЕ с этой тетрадкой.
# Токен: https://sberosc.ca.sbrf.ru/dashboard/login/?next=/dashboard/profile/
#        логин — цифры, пароль — от DataLab, токен в разделе «Профиль».
# Токен НЕ коммитить: подставить здесь руками или прочитать из .env.
token = "указать токен sberosc"

!pip install --index-url https://token:{token}@sberosc.ca.sbrf.ru/repo/pypi/simple -r requirements.txt
```

### Ячейка 3 — доступные модели

```python
# ============ ДОСТУПНЫЕ МОДЕЛИ LLM ============
# Состав моделей меняется. Проверяем ДО запуска: захардкоженное имя
# однажды перестанет существовать, и лучше узнать об этом сейчас.
from langchain_gigachat.chat_models import GigaChat
import os

_probe = GigaChat(
    base_url=os.getenv('GIGACHAT_API_URL'),
    access_token=os.getenv('JPY_API_TOKEN'),
    model="GigaChat-2",
)
for _m in _probe.get_models().data:
    print(_m.id_)
```

### Ячейка 4 — параметры и запуск

```python
# ==================== ПАРАМЕТРЫ И ЗАПУСК ====================
from myproject import run

# --- Подключение к БД ---
CONN = "postgresql+psycopg2://USER:PWD@HOST:5432/DB"
SCHEMA = "имя_витринной_схемы"

# --- LLM ---
LLM_MODEL = "glm-5.2"      # из списка выше; пусто — возьмётся из .env
LLM_TIMEOUT = (10, 120)    # (connect, read), сек
LLM_MAX_TOKENS = 4000      # при finish_reason='length' — увеличить
LLM_MAX_CALLS = 80         # потолок вызовов НА КАЖДУЮ единицу разбора
LLM_BATCH = 12             # элементов в одном запросе (при сбое дробится сам)

# --- Отчётный период ---
REPORT_MONTH = "2026-07"   # ЯВНО: пусто → последний месяц витрины, а это уже
                           # начавшийся месяц, если запускаете в начале следующего
TODAY = ""                 # пусто → системная дата; задаётся для перепроверки задним числом

path = run(
    conn=CONN,
    schema=SCHEMA,
    params={
        "report_month": REPORT_MONTH,
        "today": TODAY,
    },
    llm_opts={
        "model": LLM_MODEL,
        "timeout": LLM_TIMEOUT,
        "max_tokens": LLM_MAX_TOKENS,
        "max_calls": LLM_MAX_CALLS,
        "batch": LLM_BATCH,
        "extra": {},        # напр. отключить размышления: {"thinking": {"type": "disabled"}}
    },
    verbose=True,           # прогресс в консоль — обязательно
)
print("Готово:", path)
```

---

## Коротко: что проверить перед тем, как считать код готовым

1. Запускается из тетрадки, логика — в `.py`; ячейки идут в порядке
   описание → установка пакетов → список моделей → параметры и запуск.
2. `requirements.txt` рядом с тетрадкой, токен зеркала не в коммите.
3. Параметры LLM и БД — в тетрадке, не в коде.
4. Каждый вызов LLM: дроссель, фолбэк, лог в файл.
5. Батчи деградируют при сбое, бюджет — на единицу разбора.
6. SQL — под ядро 9.4, тяжёлое агрегируется в БД.
7. Kerberos перехвачен, timeout на запрос стоит.
8. Чувствительные имена в LLM не уходят, в артефакте заменены.
9. Прогресс печатается, сходимость проверяется, расхождения видны.
10. Расчёты описаны в `methodology.md`, числа сверены запуском.
11. В интернет не ходим — кроме установки пакетов с внутреннего зеркала.
