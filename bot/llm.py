"""Pluggable LLM provider layer. Supports deepseek / gemini / groq / openrouter.

Select provider via env LLM_PROVIDER. All providers degrade gracefully:
if no key configured, an exception is raised and callers fall back to regex/None.

Один общий httpx.AsyncClient переиспользуется между всеми вызовами (пул соединений,
без пересоздания TCP/TLS на каждый запрос). Клиент создаётся лениво при первом
запросе и закрывается через aclose() при завершении бота.
"""
import asyncio
import time

import httpx

import config

# Максимум одновременных LLM-вызовов (мод-воркеры). Нужен, чтобы не упираться в
# rate limits провайдеров (429) простым «сервером»: пока один LLM-запрос висит,
# остальные ждут своей очереди, а не лезут в API параллельно.
# Единый семафор объявлен ниже рядом с use-точкой (complete); дублировать его
# здесь не надо — второй экземпляр был бы мёртвым кодом.
LLM_SEMAPHORE_MAX = 2

OPENAI_COMPAT = ("deepseek", "groq", "openrouter")

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()

# Safety-фильтры Gemini отключаем: бот обязан анализировать мат/угрозы, а не цензуриться.
_GEMINI_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)


async def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=5.0),
                    headers={"User-Agent": "DAP-ModBot/1.0"},
                )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


def available_providers() -> list[str]:
    """Available providers (API key set), in preference order.
    Первым идёт провайдер из config.LLM_PROVIDER (если ключ задан), затем
    остальные с настроенными ключами — это и есть цепочка автофейловера."""
    p = config.LLM_PROVIDER
    has_key = lambda x: bool(getattr(config, f"{x.upper()}_API_KEY", ""))
    ordered = [p] if has_key(p) else []
    ordered += [x for x in ("deepseek", "groq", "openrouter", "gemini")
                if x != p and has_key(x)]
    return ordered


def available_provider() -> str | None:
    """Первый настроенный (приоритетный) провайдер — совместимая обёртка для
    moderation/translator, которые проверяют «есть ли хоть один LLM». Реальная
    ротация с фейловером — в available_providers()/complete()."""
    ps = available_providers()
    return ps[0] if ps else None


# Circuit breaker: N подряд неудач (403/429/сеть) временно выводят провайдера из
# ротации, чтобы не долбить API параллельно и не спамить в логи. Коулдаун задан
# в секундах; по истечении провайдер снова доступен (плановый возврат).
LLM_CIRCUIT_MAX_FAILS = 3
LLM_CIRCUIT_COOLDOWN = 60.0
_llm_circuit: dict[str, tuple[int, float | None]] = {}
_llm_circuit_lock = asyncio.Lock()


def _in_cooldown(p: str) -> bool:
    fails, until = _llm_circuit.get(p, (0, None))
    return until is not None and until > time.monotonic()


async def _note_failure(p: str):
    async with _llm_circuit_lock:
        fails, _ = _llm_circuit.get(p, (0, None))
        fails += 1
        until = None
        if fails >= LLM_CIRCUIT_MAX_FAILS:
            until = time.monotonic() + LLM_CIRCUIT_COOLDOWN
            print(f"[LLM] {p}: {fails} сбоев подряд — временно из ротации "
                  f"на {LLM_CIRCUIT_COOLDOWN:.0f} с")
        _llm_circuit[p] = (fails, until)


async def _note_success(p: str):
    async with _llm_circuit_lock:
        _llm_circuit.pop(p, None)


# Ограничитель: максимум 2 параллельных LLM-запроса на весь бот. Детект/перевод/
# панель — редкие и дорогие; семафор удерживает burst-пики от 429/блокировок
# event loop (это и есть «асинхронная очередь модерации» без рефакторинга client).
LLM_SEMAPHORE = asyncio.Semaphore(2)


async def complete(system: str, user: str, json_mode: bool = False,
                   temperature: float = 0.1, max_tokens: int = 500) -> str:
    """Send a chat completion. Returns raw text content, raises on failure."""
    async with LLM_SEMAPHORE:
        resp = await _complete_locked(system, user, json_mode, temperature, max_tokens)
    return resp


async def _complete_locked(system: str, user: str, json_mode: bool = False,
                           temperature: float = 0.1, max_tokens: int = 500) -> str:
    last_err: Exception | None = None
    for p in available_providers():
        if _in_cooldown(p):
            print(f"[LLM] {p} в коулдауне circuit breaker — пропускаем")
            continue
        try:
            if p == "gemini":
                out = await _gemini(system, user, json_mode, temperature, max_tokens)
            else:
                out = await _openai_compat(p, system, user, json_mode,
                                          temperature, max_tokens)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (403, 429) or code is None or code >= 500:
                await _note_failure(p)
            print(f"[LLM] {p} → {type(e).__name__} code={code}: {e}")
            last_err = e
            continue
        except (KeyError, IndexError, ValueError, TypeError) as e:
            print(f"[LLM] {p} → некорректный/пустой ответ API ({type(e).__name__}: {e})")
            last_err = e
            continue
        await _note_success(p)
        return out
    if last_err is not None:
        raise RuntimeError(f"Все LLM-провайдеры отказали: {last_err}") from last_err
    if not available_providers():
        raise RuntimeError("LLM провайдеры не настроены (нет API-ключей)")
    raise RuntimeError("Все LLM-провайдеры временно в коулдауне circuit breaker")


async def _openai_compat(p: str, system: str, user: str, json_mode: bool,
                         temperature: float, max_tokens: int) -> str:
    if p == "deepseek":
        key, base, model = (config.DEEPSEEK_API_KEY, config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL)
    elif p == "groq":
        key, base, model = (config.GROQ_API_KEY, config.GROQ_BASE_URL, config.GROQ_MODEL)
    else:
        key, base, model = (config.OPENROUTER_API_KEY, config.OPENROUTER_BASE_URL,
                            config.OPENROUTER_MODEL)

    headers = {"Authorization": f"Bearer {key}"}
    if p == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/modbot"
        headers["X-Title"] = "DAP Moderation Bot"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    client = await _http()
    resp = await client.post(
        f"{base.rstrip('/')}/chat/completions", headers=headers, json=payload
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def _gemini(system: str, user: str, json_mode: bool,
                  temperature: float, max_tokens: int) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"}
            for c in _GEMINI_SAFETY_CATEGORIES
        ],
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    client = await _http()
    resp = await client.post(url, json=payload, headers={
        "x-goog-api-client": "DAP-ModBot/1.0",
    })
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()