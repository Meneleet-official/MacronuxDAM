"""Language detection and translation (pluggable LLM provider with fallback)."""
import re

from bot import llm

LANG_NAMES = {
    "ru": "русский",
    "en": "английский",
    "uk": "украинский",
    "be": "белорусский",
    "de": "немецкий",
    "fr": "французский",
    "es": "испанский",
    "it": "итальянский",
    "pt": "португальский",
    "pl": "польский",
    "tr": "турецкий",
    "kk": "казахский",
    "ja": "японский",
    "zh": "китайский",
    "ko": "корейский",
    "vi": "вьетнамский",
}

# Языки, которыми можно пренебречь как разными (русский и белорусский льют
# на один алфавит; переводить их друг в друга обычно незачем).
_LANG_EQUIV = {"ru": ("be",), "be": ("ru",)}

# Буквы, почти однозначно определяющие язык даже в коротком сообщении.
_RU_LETTERS = "ыъэё"
_UK_LETTERS = "єіїґ"
_BE_LETTERS = "ў"

_QUOTES = ("«»", "„“", "“”", "\"\"", "''", "``", "『』", "「」")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    for open_q, close_q in _QUOTES:
        if s.startswith(open_q) and s.endswith(close_q):
            return s[len(open_q): -len(close_q)].strip()
    if len(s) >= 2 and s[0] in "\"'`" and s[-1] in "\"'`":
        return s[1:-1].strip()
    return s


def detect_language(text: str) -> str:
    """Определение языка:
    - сильные буквы (украинские єіїґ, белорусское ў) — решают сразу;
    - иначе langdetect; для кириллицы, если он впал в en/ru по культивам,
      возвращаемся к эвристикам; не-русские кириллические языки (kk/sr/bg)
      НЕ схлопываем в 'ru', чтобы перевод всё-таки выполнялся.
    """
    text = text or ""
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    has_ru = any(ch in text for ch in _RU_LETTERS)
    has_uk = any(ch in text for ch in _UK_LETTERS)
    has_be = any(ch in text for ch in _BE_LETTERS)

    base = None
    try:
        from langdetect import detect
        lang = detect(text[:1000])
        base = "zh" if lang.startswith("zh") else (lang if len(lang) == 2 else "en")
    except Exception:
        base = None

    if cyr and has_uk:
        return "uk"
    if cyr and has_be:
        return "be"
    if cyr and has_ru:
        # ъ/э/ё/ы — русский или белорусский; если langdetect уверен в других
        # кириллических (kk/sr/bg/mk), у которых тоже есть ы/э/ё, — верим ему.
        if base in ("kk", "sr", "bg", "mk"):
            return base
        return "ru"

    if cyr:
        # Кириллица без сигнальных букв — сербский/болгарский/македонский.
        # Казахский ловим по әғқңөұү; на остальное доверяем langdetect только на
        # длинном тексте (на коротком он бредит: 'привет как дела' -> mk).
        if any(ch in text for ch in "әғқңөұү"):
            return "kk"
        if base in ("ru", "be"):
            return "ru"
        # короткий кириллический текст без єіїґ — «uk» здесь почти всегда
        # ошибка langdetect на мате/сленге ('блять сука нахуй' -> uk)
        if base == "uk":
            return "ru"
        if len((text or "")) >= 25:
            return base or "ru"
        return "ru"

    # не-кириллица
    if base:
        return base
    return "en"


_REFUSAL_MARKS = (
    "не могу перевести", "не буду переводить", "не стану переводить",
    "не переведу", "отказываюсь", "не могу повторить", "не могу привести",
    "нарушает правила", "неприемлемо", "нецензурн", "оскорбительн",
    "оскорбление", "цензур", "перефразир", "как помощник", "как ии",
    "не могу помочь", "as an ai", "cannot", "can't", "inappropriate",
    "offensive", "refuse", "sorry", "официального перевода",
    # английские отказы, которые часто выдают модели (Gemini/LLaMA/Groq)
    "i'm sorry", "i am sorry", "i'm afraid", "i cannot help", "i can't help",
    "i cannot assist", "i can't assist", "i am unable", "i'm not able",
    "i don't feel comfortable", "not allowed to", "i won't", "won't do",
    "can't help with", "cannot help with", "as a language model",
    "as an ai language model", "i'm an ai", "i am an ai", "i'm just an ai",
    "let me know if", "let me know if i can help", "безопаснее не",
)

# Короткие стартовые фразы-отказы (явные отказы ИИ, без обычных «прости/извини/sorry»).
_REFUSAL_STARTS = (
    "не могу перевести", "не буду переводить", "не стану переводить",
    "я не могу перевести", "я не буду переводить", "я не стану переводить",
    "не могу выполнить", "я не могу выполнить", "не могу помочь", "я не могу помочь",
    "i cannot translate", "i can't translate", "i cannot fulfill", "i can't fulfill",
    "i cannot help", "i can't help", "i cannot assist", "i can't assist",
    "i'm not able to", "i am not able to", "i'm unable to", "i am unable to",
    "i don't feel comfortable",
)

# Извинения в начале — считаются отказом ТОЛЬКО если в ответе также есть маркеры отказа ИИ.
_APOLOGY_STARTS = (
    "прости", "извини", "сожалею", "sorry", "i'm sorry", "i am sorry", "i apologize",
)

# Фразы, которые встречаются в середине ответа и однозначно говорят об отказе.
_REFUSAL_PHRASES = (
    "as a language model", "as an ai", "i'm not able to", "not able to",
    "don't feel comfortable", "cannot assist", "can't assist",
    "blocked", "censored", "safety guidelines", "not acceptable",
    "не могу перевести", "не буду переводить", "нарушает правила",
)


def _is_refusal(s: str) -> bool:
    low = (s or "").strip().lower()
    if not low:
        return False
    if low in ("n/a", "не знаю", "отказ"):
        return True
    if low.startswith(_REFUSAL_STARTS):
        return True
    if any(m in low for m in _REFUSAL_PHRASES):
        return True
    if low.startswith(_APOLOGY_STARTS):
        apology_refusal_hints = (
            "не могу", "не буду", "не стану", "перевести", "перевод", "правила",
            "cannot", "can't", "unable", "translate", "assist", "help with",
            "inappropriate", "offensive", "policy", "guidelines", "ai", "модель",
        )
        if any(h in low for h in apology_refusal_hints):
            return True
    # Полноценный ответ с пометкой «я ассистент/ИИ» и запретом — тоже отказ,
    # но только на достаточно длинном тексте (чтобы не поймать обычный перевод).
    tails = ("я не", "я просто ассистент", "как языковая модель", "я искусственный",
             "i just", "i don't provide", "we are not able")
    return len(s) >= 25 and any(m in low for m in _REFUSAL_MARKS) and any(t in low for t in tails)


def _looks_like_echo(out: str, source: str) -> bool:
    """Перевод почти не отличался от оригинала — считаем провалом."""
    a = re.sub(r"\W+", "", out.lower())
    b = re.sub(r"\W+", "", source.lower())
    if not a or not b:
        return False
    return a == b


def clean_translation(out: str) -> str:
    """Убирает обёртку/предисловие/кавычки, которые модели любят добавлять."""
    s = out.strip()
    s = re.sub(r"^(?:Перевод|Translation|Translate)\s*[::\-–—]\s*", "", s, flags=re.I)
    s = re.sub(r"^[>*]\s*", "", s, flags=re.I)
    s = _strip_quotes(s)
    s = re.sub(r"\s+", " ", s).strip()
    if _is_refusal(s):
        return ""
    return s


async def translate_with_llm(text: str, target_language: str) -> str:
    """Перевод через LLM с валидацией и одной повторной попыткой.
    Возвращает исходный текст, если перевести не удалось/модель отказалась."""
    if not text or not text.strip() or len(text.strip()) < 2:
        return text
    if llm.available_provider() is None:
        return text

    lang_name = LANG_NAMES.get(target_language, target_language)
    words = len(text.split())
    if target_language == "ru":
        idiom_hint = (
            "Вместо дословного перевода используй русский фразеологизм-эквивалент "
            "(например 'son of a bitch' → 'сукин сын', а не 'сын суки'; "
            "'den Sohn einer Schlampe' → 'сукин сын'). "
        )
    else:
        idiom_hint = (
            f"Используй естественные идиомы и разговорные конструкции языка ({lang_name}) "
            "вместо буквальной кальки. "
        )
    system = (
        f"Ты — переводчик для модерации Discord-сервера. Переведи сообщение на {lang_name} так, "
        "как это сказал бы носитель языка: естественно, идиоматично, без кальки. "
        "Сохрани смысл, тон, реестр и нецензурную лексику — НЕ цензурируй. "
        + idiom_hint +
        "Если фразу можно понять как оскорбление в адрес собеседника — передай это "
        "обращение естественно. Никнеймы (@user), упоминания каналов (<#...>), "
        "ссылки и эмодзи копируй без изменений. Не добавляй пояснений, предисловий "
        "и кавычек. Верни только сам перевод."
    )
    prompt = f"Переведи на {lang_name}:\n{text}"

    for attempt in (1, 2):
        try:
            raw = await llm.complete(system, prompt, json_mode=False,
                                     temperature=0.1, max_tokens=max(400, words * 10))
        except Exception:
            return text
        out = clean_translation(raw)
        if _is_refusal(raw):
            from bot.telemetry import log_event
            log_event("translation_refusal", text=text[:500], raw=raw[:800],
                      target_lang=target_language)
            if attempt == 1:
                prompt = (f"Сообщение:\n{text}\n\n"
                          f"Переведи его на {lang_name} естественно и идиоматично, без кальки "
                          f"(передавая смысл и реестр, включая мат). Без пояснений, предисловий "
                          f"и кавычек. Если сообщение уже на этом языке — просто верни оригинал как есть.")
                continue
            return text
        if out and not _looks_like_echo(out, text):
            return out
        if attempt == 1:
            prompt = (f"Сообщение:\n{text}\n\n"
                      f"Переведи его на {lang_name} естественно и идиоматично, без кальки "
                      "(передавая смысл и реестр, включая мат). Без пояснений, предисловий "
                      "и кавычек. Если сообщение уже на этом языке — просто верни оригинал как есть.")
    return text


async def translate(text: str, target_language: str) -> str:
    """Публичная обёртка: пропускаем перевод, если язык уже целевой."""
    if not text or not text.strip():
        return text
    src = detect_language(text)
    if src == target_language:
        return text
    if src in _LANG_EQUIV.get(target_language, ()):
        return text
    return await translate_with_llm(text, target_language)