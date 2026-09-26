"""Hybrid moderation: regex detectors + LLM contextual analysis (pluggable provider)."""
import hashlib
import json
import re
import unicodedata
from typing import Optional

from bot import llm, rules

ZALGO_RE = re.compile(r"[\u0300-\u036f\u0483-\u0489\ufe20-\ufe2f]")
MENTION_RE = re.compile(r"<@[!&]?\d+>")
EVERYONE_RE = re.compile(r"@everyone|@here", re.IGNORECASE)
URL_RE = re.compile(
    r"https?://[^\s]+|www\.[^\s]+|discord\.gg/[^\s]+|discord\.com/invite/[^\s]+",
    re.IGNORECASE,
)
# Личные данные (деанон, правило 3.4)
RU_PHONE_RE = re.compile(r"(?<!\d)((?:8|\+7)[\- ()]*\d{3}[\- ()]*\d{3}[\- ()]*\d{2}[\- ()]*\d{2})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
PASSPORT_RE = re.compile(r"\b\d{4}\s?№?\s?\d{6}\b")
CARD_RE = re.compile(r"\b\d{4} ?\d{4} ?\d{4} ?\d{4}\b")
DEANON_WORDS = ["деанон", "деанонить", "скинуть паспорт", "паспорт", "по пропуску людей",
                "реальный адрес", "место жительства", "пробить", "пробив", "пробиваю"]
ADDRESS_RE = re.compile(r"\b(?:улица|ул\.|ул)\s+[А-ЯЁA-Zа-яёA-Za-z]+\s*\d+", re.IGNORECASE)
# Реклама/спам (правило 3.3)
SHORTENERS = ["bit.ly", "goo.gl", "clck.ru", "tinyurl.com", "cutt.ly", "is.gd",
              "rb.gy", "surl.lu", "tiny.cc", "t.me/", "youtu.be", "wa.me", "vk.cc"]
SCAM_LINK_HINTS = ["nitro", "claim", "free", "gift", "steam", "giftcard", "промокод",
                   "бесплатно", "giveaway", "выигрыш", "подарок", "раздача", "double",
                   "boost", "лос кейс", "кейсы", "кинг кейс"]
INVITE_HINT_RE = re.compile(r"(дискорд|дс|стервис|сервер).{0,20}(дискорд|invite|ссыл)", re.IGNORECASE)
# Флуд (правило 3.6): капс и эмодзи-спам
CAPS_RE = re.compile(r"[А-ЯA-ZЁ]")
LETTER_RE = re.compile(r"[А-Яа-яA-Za-zЁё]")
EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\U0001F300-\U0001F64F\U0001F680-\U0001F6FF"
    r"\U0001F900-\U0001F9FF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]"
)
SAME_CHAR_RE = re.compile(r"(.)\1{9,}")
# Оскорбления и угрозы (правило 3.1)
THREAT_WORDS = ["убью", "прибью", "зарежу", "расчленю", "сожгу", "повешу", "подорву",
                "задушу", "перережу", "тебе конец", "конец тебе", "ты мертв", "ты труп",
                "труп ты", "выходи", "смотри за спиной", "я тебя найду", "найду тебя",
                "ddos", "свалю сервер", "сломаю сервер", "сниму с твоих родных",
                "придумаю", "уничтожу", "покалечу", "завалю", "порешу", "закопаю",
                "kill yourself", "kys", "go kys", "go die", "hope you die",
                "die in a hole", "kill urself", "k y s"]
# «иди нахуй»/«нахуй шёл» и аналоги — грубая команда/посыл, адресованный участнику (3.1).
# Отдельный «хуй»/«нахуй» без команды — это просто мат (3.2, разрешён).
DIRECTIVE_INSULT_RE = re.compile(
    r"(?:пош[ёе]л|шёл|иди|вали|уёбывай|катись|отвали|убирайся)"
    r"\s+на\s*(?:хуй|хуя|хер|хрен|фиг)"
    r"|(?:на\s*)?(?:хуй|хер|хрен|фиг)\s*(?:иди|шёл|пош[ёе]л|валы|уёбывай|отвали)",
    re.IGNORECASE,
)
# Личные оскорбления (3.1) — только прямые и однозначные, без двусмысленных слов
INSULT_WORDS = [
    "дурак", "дура", "придурок", "тупица", "тупой", "мудак",
    "позорься", "опозорься", "позорище", "бездарь",
    "idiot", "stupid", "dumb", "loser", "dummy", "dumbass", "donkey",
]

# Мат-токены (правило 3.2 — мат «в меру» разрешён). Одиночное употребление
# матюга не наказуемо, но «систематическое» (спам матом, переполнение сообщения
# ненормативкой в токсичном ключе) — это уже нарушение 3.2.
# Табуированные лексемы, устойчивые к повторам/режеверсиям типа «блять»х20.
_MAT_TOKENS = (
    "бля", "блят", "блять", "блядин", "бляд", "нахуй", "нахуя", "похуй",
    "хуй", "хуя", "хуи", "хуе", "хуищ", "сука", "сук", "пизд", "пиздец",
    "еб", "ёб", "ыеб", "заеб", "наеб", "разъеб", "охуе", "охуи",
    "залуп", "манда", "шлюх", "прос", "тварь", "мразь", "гавно",
    "ебаный", "блядский", "мудозвон", "хуесос", "хую",
)
# Собираем regex один раз: (?:токен1|токен2|...) в правиле мало-фальшивых (длины >=3).
_MAT_RE = re.compile(
    r"(?:" + "|".join(re.escape(t) for t in sorted(_MAT_TOKENS, key=len, reverse=True)) + r")",
    re.IGNORECASE,
)

# Простая ловушка для повторов букв, не искажающих слово («бляяять» → «бля»):
# убираем подряд идущие одинаковые буквы перед матч-каунтом.
_MAT_COLLAPSE_RE = re.compile(r"(.)\1+")
_MAT_NONLETTER_SPLIT_RE = re.compile(r"[\W_]+")

# Simple phishing/scam keywords that trigger context-based suspicion
SCAM_KEYWORDS = ["бесплатно", "nitro", "подарок", "free nitro", "claim", "giveaway", "выигрыш"]

# Прославление/подстрекательство к преступным и девиантным действиям (педофилия,
# инцест, насилие над детьми, Гитлер/нацизм, Эпштейн и т.п.). Ловим только ЯВНОЕ
# одобрение/возвышение/призыв (многословные фразы), чтобы не цеплять безобидные
# упоминания и осуждение («педофилов надо сажать», «инцест — это плохо»,
# «сталин лучше гитлера»). Идём в правило 4.5 (социальный харассмент / девиантность).
_HARMFUL_PRAISE_RE = re.compile(
    r"(?:"
    r"э[пш]{2}т\w*\s+(?:был\s+)?(?:всегда\s+)?прав"
    r"|э[пш]{2}т\w*\s+(?:был\s+)?непогрешим"
    r"|э[пш]{2}т\w*\s+(?:это\s+)?хорош"
    r"|э[пш]{2}т\w*\s+лучше(?:\s+всех)?"
    r"|лучше\s+э[пш]{2}т\w*"
    r"|э[пш]{2}т\w*\s+хотя\s+бы"
    r"|за\s+э[пш]{2}т\w*"
    r"|(?<!не\s)любл[юи]т?\s+инцест"
    r"|(?<!не\s)инцест[ауе]?\s+любл[юи]?"
    r"|(?<!не\s)инцест[ае]?\s+нравит"
    r"|инцест[ае]?\s+не\s+хвата"
    r"|инцест\s+это\s+хорош"
    r"|педофил[ыа]?\s+прав"
    r"|гитлер(?:а|у)?\s+(?:был\s+)?(?:всегда\s+)?прав"
    r"|гитлер\s+(?:это\s+)?хорош"
    r"|гитлер\s+лучше\s+всех"
    r"|любл[юи]т?\s+гитлер(?:а|у)?"
    r"|хайль\s+гитлер"
    r"|зиг\s*хайль|sig\s*heil"
    r")",
    re.IGNORECASE,
)

# «X лучше гитлера» — обычно анти-Гитлер и безобидно; ловим только если рядом
# упомянут Эпштейн («эпштейна лучше гитлера»).
_EPSTEIN_HITLER_COMPARE_RE = re.compile(
    r"э[пш]{2}т\w*.*(?:лучше\s+гитлер|гитлер\s+лучше)",
    re.IGNORECASE | re.DOTALL,
)


class DetectionResult:
    def __init__(self, rule_id: Optional[str] = None, method: str = "regex",
                 severity: str = "low", reason: str = ""):
        self.rule_id = rule_id
        self.method = method
        self.severity = severity
        self.reason = reason

    def detected(self) -> bool:
        return self.rule_id is not None

    def __repr__(self):
        return f"<DetectionResult rule={self.rule_id} method={self.method} sev={self.severity}>"


_SEV_RANK = {"high": 3, "medium": 2, "low": 1}

# Фразы, явно помеченные модераторами как «Не нарушение» (✖️ dismiss).
# Позволяют мгновенно гасить повторные ложные срабатывания без повторного вызова LLM,
# но НЕ отменяют прямые банворды раздела 4 (п. 4.0: запрещены даже в шутку/цитату).
DISMISSED_PHRASES: set[str] = set()


def mark_phrase_dismissed(text: str, rule_id: Optional[str] = None) -> None:
    """Регистрирует фразу, снятую модератором через «Не нарушение», в белом списке и кэше LLM."""
    cleaned = (text or "").strip().lower()
    if len(cleaned) < 3:
        return
    if not (rule_id or "").startswith("4."):
        DISMISSED_PHRASES.add(cleaned)
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    for k in list(LLM_CACHE.keys()):
        if isinstance(k, tuple) and len(k) >= 1 and k[0] == digest:
            LLM_CACHE[k] = (None, None)
    for lang in ("ru", "en", "uk"):
        _cache_set((digest, lang, ""), (None, None))
        _cache_set((digest, lang), (None, None))


def _normalize(text: str) -> str:
    """Normalize zalgo + collect letters for obfuscation-insensitive matching."""
    text = ZALGO_RE.sub("", text)
    return text


def regex_check(message_text: str, guild_banwords=None) -> Optional[DetectionResult]:
    """Run all regex detectors. Returns the most severe DetectionResult;
    its reason lists every triggered rule (so multi-violation messages are fully covered)."""
    text = normalize_for_check(message_text)
    lowered = text.lower()
    raw = message_text.lower()
    raw_compact = raw.replace(" ", "")
    is_dismissed = raw.strip() in DISMISSED_PHRASES

    results: list = []

    def add(rule_id: str, severity: str, reason: str):
        if is_dismissed and not str(rule_id).startswith("4."):
            return
        for r in results:
            if (r.rule_id, r.severity, r.reason) == (rule_id, severity, reason):
                return
        results.append(DetectionResult(rule_id=rule_id, method="regex",
                                       severity=severity, reason=reason))


    # Banned words (base + server-specific) — collect ALL matches in the message
    detector, meta = build_banword_regex(guild_banwords or [])
    if detector:
        for mm in detector.finditer(lowered):
            rule_id = None
            word = None
            for i, rid in enumerate(meta, start=1):
                if mm.group(i) is not None:
                    rule_id = rid
                    word = mm.group(i).strip()
                    break
            if rule_id is None:
                rule_id = "4.0"
                word = mm.group(0).strip()
            add(rule_id, "medium", f"Банворд: {word}")

    # Деанон / личные данные (3.4) — высокий приоритет
    phone = RU_PHONE_RE.search(message_text)
    if phone:
        add("3.4", "high", f"Телефон: {phone.group(1)}")
    email = EMAIL_RE.search(message_text)
    if email:
        add("3.4", "high", f"Email: {email.group(0)}")
    if PASSPORT_RE.search(message_text):
        add("3.4", "high", "Паспортные данные")
    if CARD_RE.search(message_text):
        add("3.4", "high", "Номер банковской карты")
    if any(w in lowered for w in DEANON_WORDS) or ADDRESS_RE.search(message_text):
        add("3.4", "high", "Признаки деанона/пробива")

    # Угрозы (3.1)
    if any(w in lowered for w in THREAT_WORDS):
        add("3.1", "high", "Угроза")
    # Грубый посыл («иди нахуй», «нахуй шёл») — оскорбление участника (3.1)
    if DIRECTIVE_INSULT_RE.search(lowered):
        add("3.1", "high", "Грубый посыл/оскорбление")
    # Личные оскорбления, включая мягкие («ты дура», «не позорься») (3.1)
    if any(w in lowered for w in INSULT_WORDS):
        add("3.1", "medium", "Оскорбление")

    # Zalgo (3.6)
    if ZALGO_RE.search(message_text) and len(ZALGO_RE.findall(message_text)) > 3:
        add("3.6", "low", "Zalgo-символы")

    # Mentions > 5 (по сырому тексту — нормализация вырезает <@>)
    mentions = MENTION_RE.findall(message_text)
    if len(mentions) > 5:
        add("2.5", "medium", f"{len(mentions)} упоминаний")

    if EVERYONE_RE.search(message_text):
        add("2.5", "medium", "Массовое упоминание")

    # Реклама / спам (3.3) — ищем по сырому тексту, чтобы не терять точки и слэши
    suspicious_url = None
    for url in URL_RE.findall(message_text):
        low_url = url.lower()
        if "discord.gg" in low_url or "discord.com/invite" in low_url:
            suspicious_url = url
            break
        if any(k in low_url for k in SCAM_LINK_HINTS) or any(
                s in low_url for s in SHORTENERS):
            suspicious_url = url
            break
    if suspicious_url is None:
        for s in SHORTENERS:
            if s in raw or s in raw_compact:
                suspicious_url = s
                break
    if suspicious_url:
        add("3.3", "medium", f"Реклама/подозрительная ссылка: {suspicious_url}")
    if "discord.gg/" in raw or "discord.com/invite/" in raw or INVITE_HINT_RE.search(raw):
        add("3.3", "medium", "Приглашение на сторонний сервер")

    # Флуд: капс и эмодзи (3.6)
    caps_count = len(CAPS_RE.findall(message_text))
    letters_count = len(LETTER_RE.findall(message_text))
    if letters_count >= 8 and caps_count / letters_count > 0.65:
        add("3.6", "low", "Злоупотребление CAPS LOCK")
    emojis = EMOJI_RE.findall(message_text)
    if len(emojis) > 10 or (emojis and len(set(emojis)) == 1 and len(emojis) > 4):
        add("3.6", "low", f"Эмодзи-спам ({len(emojis)})")
    if SAME_CHAR_RE.search(message_text):
        add("3.6", "low", "Повторяющиеся символы")

    # Мат-спам (3.2)
    mat = mat_spam_check(message_text)
    if mat:
        add("3.2", mat["severity"], mat["reason"])

    # Прославление/подстрекательство к преступным и девиантным действиям (4.5)
    m2 = _HARMFUL_PRAISE_RE.search(lowered)
    if m2:
        add("4.5", "high", f"Прославление/пропаганда: {m2.group(0).strip()}")
    m3 = _EPSTEIN_HITLER_COMPARE_RE.search(lowered)
    if m3:
        add("4.5", "high", f"Прославление/сравнение с Гитлером: {m3.group(0).strip()}")

    if not results:
        return None
    results.sort(key=lambda r: _SEV_RANK.get(r.severity, 0), reverse=True)
    best = results[0]
    reasons = []
    for r in results:
        if r.reason not in reasons:
            reasons.append(r.reason)
    if len(reasons) > 1:
        best.reason = " ; ".join(reasons)
    return best


def normalize_for_check(text: str) -> str:
    """Strip non-letter chars for banword obfuscation matching, keep separators."""
    return re.sub(r"[\W_]+", " ", text.lower())


def mat_spam_check(message_text: str):
    """Детект «систематического» мата (правило 3.2). Одиночный мат («бля», «сука»)
    разрешён — наказуемо только переполнение сообщения ненормативкой в токсичном ключе.

    Логика:
      - чистим повторы букв («бляяять» → «блят») и разбиваем на слова;
      - считаем мат-токены во всём сообщении;
      - если мат составляет большинство осмысленных слов И мата не меньше N — нарушение.
    Возвращает dict(severity, reason) или None.
    """
    collapsed = _MAT_COLLAPSE_RE.sub(r"\1", message_text.lower())
    tokens = [t for t in _MAT_NONLETTER_SPLIT_RE.split(collapsed) if t]
    if not tokens:
        return None
    mat_count = 0
    matched = set()
    for t in tokens:
        if _MAT_RE.search(t):
            mat_count += 1
            matched.add(t)
    if mat_count < 3:
        return None
    # Мат должен доминировать: >= 60% слов — иначе это обычная ругань «в меру» с парой матюгов.
    ratio = mat_count / len(tokens)
    if len(tokens) <= 2 and mat_count >= 2:
        ratio = 1.0
    if ratio < 0.6:
        return None
    distinct = len(matched)
    if mat_count >= 8 or (mat_count >= 5 and distinct >= 2):
        severity = "high"
        reason = f"Систематический мат: {mat_count} мат-слов в сообщении ({', '.join(sorted(matched)[:4])}…)"
    else:
        severity = "medium"
        reason = f"Злоупотребление матом: {mat_count} мат-слова"
    return {"severity": severity, "reason": reason}


def build_banword_regex(server_words):
    """Build a single regex over internal + server banned words; returns (regex, meta) or None."""
    patterns = []
    meta = []  # group index -> rule id
    seen = set()
    # Internal rules
    for rule_id, words in rules._BANWORD_BASE.items():
        for w in words:
            key = _variant_key(w)
            if key in seen:
                continue
            seen.add(key)
            patterns.append(_full_word_pattern(w))
            meta.append(rule_id)
    # Foreign-language rules (4.0: иностранные языки тоже нарушение)
    for rule_id, words in rules._BANWORD_FOREIGN.items():
        for w in words:
            key = _variant_key(w)
            if key in seen:
                continue
            seen.add(key)
            patterns.append(_full_word_pattern(w))
            meta.append(rule_id)
    # Server words
    for w in server_words:
        key = _variant_key(w)
        if key in seen:
            continue
        seen.add(key)
        patterns.append(_full_word_pattern(w))
        meta.append("server")
    if not patterns:
        return None, meta
    regex = re.compile("|".join(patterns), re.IGNORECASE)
    return regex, meta


def _variant_key(word: str) -> str:
    return word.lower().replace("ё", "е")


def _word_pattern(word: str) -> str:
    """Regex for a word tolerating homoglyphs across scripts and languages:
    Cyrillic↔Latin lookalikes (д/d, р/r/p, ы) and digits (0→о, 1→и, 3→з/е...)."""
    out = []
    for ch in word.lower().replace("ё", "е"):
        out.append(_LETTER_CLASSES.get(ch, re.escape(ch)))
    return r"\s*".join(out)


def _full_word_pattern(w: str) -> str:
    """Внешний паттерн слова: (?<!\\w) запрещает матч внутри нейтральных слов
    (плохо, книга, полях), суффиксная ловушка отсекает совпадения типа симпатия/овощи.
    Для CJK-слов (без границ слов) — только сам паттерн."""
    if _is_cjk(w):
        return f"({_word_pattern(w)})"
    return f"(?<!\\w)({_word_pattern(w)}){_suffix_guard(w)}"


_SUFFIX_GUARDS = {
    "симп": "(?![алтlat])",    # симпатия, симпатия, симптом / symbol, simple
    "simp": "(?![алтlat])",
    "жид": "(?![кk])",         # жидкий/жидкость / zhidkiy
    "zhid": "(?![кk])",
    "zhyd": "(?![кk])",
    "овощ": "(?![иаеян])",     # овощи/овощами/овощной
    "педик": "(?![юuy])",      # педикюр / pedikyur, pedikur
    "pedik": "(?![юuy])",
    "псих": "(?![оo])",        # психология/психоз / psikho(previously...)
    "арм": "(?![иаеaiey])",    # армия/армейский/арматура / army, armia
    "азер": "(?![бb])",        # азербайджан / azerbaijan
    "гук": "(?![аa])",         # гукать / gukat
    "даун": "(?![тt])",        # даунтаун / dauntaun
    "хач": "(?![аaиiеeyzZ])",   # хачапури/khachapuri; "hace"/"hazy" не ловить
    "чмо": "(?![кk])",          # чмокать/чмокнуть
    "инвалид": "(?![нn])",     # инвалидная коляска / invalidnaya
    "haji": "(?!m)",           # hajime
    "spic": "(?![ey])",        # spice/spicy
    "pic": "(?![tkn])",        # turkish "piç" vs picture/picnic
    "troia": "(?!n)",          # "troia" vs имя/троян
}

_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x2FFF), (0x3040, 0x30FF), (0x3130, 0x318F),
    (0x31F0, 0x31FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F),
    (0xAC00, 0xD7AF), (0xD7B0, 0xD7FF), (0xF900, 0xFAFF),
)


def _is_cjk(word: str) -> bool:
    """Кана/кандзи/хангыль не имеют границ слов: (?<!\\w) ломал бы матч
    в плотном тексте типа 你是傻逼, поэтому для таких слов ловушки не ставятся."""
    return any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES for ch in word)

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     "abvgdeeziyklmnoprstufhc4s5yyeua"))


def _suffix_guard(w: str) -> str:
    k = _variant_key(w)
    g = _SUFFIX_GUARDS.get(k)
    if g:
        return g
    lat = "".join(_TRANSLIT.get(ch, ch) for ch in k)
    return _SUFFIX_GUARDS.get(lat, "")


_LETTER_CLASSES = {
    # кириллица → гомоглифы (кириллица + латиница + цифры)
    # гомоглифы, БЕЗ перекрёстной растяжки гласных (иначе «нига» ловит «него»)
    "а": "[аa@4]",
    "б": "[бb68]",
    "в": "[вv]",
    "г": "[гg9]",
    "д": "[дd9]",
    "е": "[еёe3]",
    "ж": "[жjх]",
    "з": "[з3]",
    "и": "[иi1u]",
    "й": "[йy]",
    "к": "[кk]",
    "л": "[лl17]",
    "м": "[мm]",
    "н": "[нnh]",
    "о": "[оo0]",
    "п": "[пnpр]",
    "р": "[рpr]",
    "с": "[сc5s]",
    "т": "[тt7]",
    "у": "[уu]",
    "ф": "[фf]",
    "х": "[хxhж]",
    "ц": "[цc]",
    "ч": "[чc4]",
    "ш": "[шw]",
    "щ": "[щш]",
    "ы": "[ыy]",
    "ь": "[ь]",
    "э": "[эe]",
    "ю": "[ю]",
    "я": "[яa]",
    # латиница (для банвордов из транслита и других языков)
    "a": "[аa@4áàâãäå]",
    "b": "[бb68]",
    "c": "[сc5ç]",
    "d": "[дd9]",
    "e": "[еёe3éèêë]",
    "f": "[фf]",
    "g": "[гg9ğ]",
    "h": "[хxhж]",
    "i": "[иi1uíìîïı]",
    "j": "[жj]",
    "k": "[кk]",
    "l": "[лl17]",
    "m": "[мm]",
    "n": "[нnhñ]",
    "o": "[оo0óòôõö]",
    "p": "[пnpр]",
    "q": "[кkq]",
    "r": "[рpr]",
    "s": "[сc5sşß]",
    "t": "[тt7]",
    "u": "[уuиiúùûü]",
    "v": "[вv]",
    "w": "[шw]",
    "x": "[хxhж]",
    "y": "[йyýÿ]",
    "z": "[з3zž]",
}


async def llm_check(message_text: str, guild_banwords=None,
                    context: Optional[str] = None,
                    guild_id: Optional[int] = None) -> Optional[DetectionResult]:
    """Analyze message contextually via the configured LLM provider. (detection only)"""
    res, _ = await llm_check_with_translation(
        message_text, guild_banwords, target_lang="ru", context=context, guild_id=guild_id
    )
    return res


# Кэш вердиктов LLM по (хэш текста, целевой язык, хэш контекста): одно и то же спам-сообщение
# проверяется один раз. Простая FIFO-эвикция без зависимостей.
LLM_CACHE: dict = {}
LLM_CACHE_MAX = 512


def _cache_set(key, value) -> None:
    LLM_CACHE[key] = value
    while len(LLM_CACHE) > LLM_CACHE_MAX:
        LLM_CACHE.pop(next(iter(LLM_CACHE)))


def _collect_dismissed_examples(guild_id: Optional[int] = None, limit: int = 12) -> list[str]:
    """Собирает последние фразы, помеченные модераторами как «Не нарушение», из телеметрии и БД."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        from bot.telemetry import load_dismissed_examples
        for txt in load_dismissed_examples(guild_id=guild_id, limit=limit):
            low = txt.strip().lower()
            if low and low not in seen:
                seen.add(low)
                out.append(txt.strip())
    except Exception:
        pass
    if len(out) < limit:
        try:
            from bot.database import get_db
            for txt in get_db().list_dismissed_examples(guild_id=guild_id, limit=limit):
                low = txt.strip().lower()
                if low and low not in seen:
                    seen.add(low)
                    out.append(txt.strip())
                    if len(out) >= limit:
                        break
        except Exception:
            pass
    return out[:limit]


async def llm_check_with_translation(message_text: str, guild_banwords=None,
                                     target_lang: str = "ru",
                                     context: Optional[str] = None,
                                     guild_id: Optional[int] = None):
    """Объединённый вызов: классификация нарушения + перевод в один LLM-запрос.

    Возвращает (DetectionResult | None, translation | None). Ровно один LLM-вызов
    вместо двух (детект + перевод) — экономия квоты и задержки. JSON-режим
    провайдера + валидация; при сбое/отказе модели возвращаем (None, None)
    и логируем сырой ответ в телеметрию.
    Поддерживает контекст диалога (Reply + последние реплики автора) и Few-Shot
    самообучение по нажатиям «✖️ Не нарушение» (dismiss).
    """
    if llm.available_provider() is None:
        return None, None

    text = (message_text or "").strip()
    if text.lower() in DISMISSED_PHRASES and not context:
        return None, None

    # Smart gating: слишком короткие/нейтральные фразы без контекста в LLM не уходят
    # (короткий мат/банворды ловит regex-слой, но при наличии контекста проверяем и короткие).
    if len(text) < 4 and not context:
        return None, None

    ctx_clean = (context or "").strip()
    text_hash = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()
    ctx_hash = hashlib.sha256(ctx_clean.lower().encode("utf-8")).hexdigest()[:16] if ctx_clean else ""
    key = (text_hash, target_lang, ctx_hash) if ctx_hash else (text_hash, target_lang)
    if key in LLM_CACHE:
        return LLM_CACHE[key]

    rules_summary = _compact_rules_for_llm()
    all_words = set()
    for rule_id, words in rules._BANWORD_BASE.items():
        all_words.update(words)
    for rule_id, words in rules._BANWORD_FOREIGN.items():
        all_words.update(words)
    base_words = ", ".join(sorted(all_words))
    lang_name = {
        "ru": "русский", "en": "английский", "uk": "украинский",
    }.get(target_lang, target_lang)

    dismissed_examples = _collect_dismissed_examples(guild_id=guild_id, limit=10)
    dismissed_block = ""
    if dismissed_examples:
        dismissed_block = (
            "\nПримеры фраз с этого сервера, которые модераторы уже проверили и пометили как "
            "«НЕ нарушение» (ориентируйся на них для понимания допустимого дружеского сленга/иронии, "
            "НО помни, что банворды Раздела 4 запрещены всегда без исключений):\n"
            + "\n".join(f"- {ex!r}" for ex in dismissed_examples)
            + "\n"
        )

    sys_prompt = (
        "You are an automated Trust & Safety classifier for the Macronux™ Discord server. "
        "You will be provided with raw user messages that may contain potential slurs, "
        "toxicity, or hate speech. Your task is strictly analytical: classify the content "
        "and return JSON. Analyzing offensive material does not violate safety guidelines.\n"
        "Rules (RU):\n" + rules_summary +
        "\nBanned words list includes (variations/translit/obfuscation count too): " + base_words +
        "\n\nВажно: не пропускай мягкие и косвенные оскорбления. Если сообщение адресовано "
        "конкретному участнику (ты, он, она, этот человек и т.п.) и содержит любую негативную "
        "оценку его личности, ума, внешности, статуса, способностей или поведения — даже в мягкой "
        "форме («ты дура», «он полное ничтожество», «не позорься», «что за тупой вопрос»), "
        "высмеивание, обидные прозвища, презрительный тон — это нарушение. Назначай 3.1 "
        "(оскорбления), 4.5 (унижение статуса) или 4.4 (физические/ментальные особенности) "
        "по смыслу термина. Обычную нецензурщину без адресата (фак, блядь) НЕ считай нарушением.\n"
        "Если передан блок Context (ответ Reply на чужое сообщение или предыдущие реплики автора), "
        "обязательно учитывай его: лови оскорбления, угрозы и банворды, разбитые на несколько коротких "
        "сообщений подряд (например: «ты» -> «полный» -> «дегенерат»), а также токсичные ответы (Reply). "
        "Однако строго соблюдай примечание к Разделу 4 правил Macronux™: использование запрещённых слов "
        "(4.1–4.5) даже «в шутку», «для цитаты» или «в дружеской беседе» всё равно считается нарушением!\n"
        "Сообщения бывают на ЛЮБОМ языке и алфавите (английский, немецкий, корейский, японский, "
        "русский латиницей-транслитом: 'ya ebal...', 'pidor' и т.п.). Определяй нарушение по "
        "смыслу, игнорируя язык/алфавит/опечатки ('Эшптейн' == 'Эпштейн').\n"
        "Отдельно лови ПРОСЛАВЛЕНИЕ и оправдание преступников и преступных деяний — даже если "
        "это не оскорбление конкретного участника: одобрение Гитлера и нацизма, Холокоста "
        "(«гитлер прав», «гитлер лучше всех», «хайль гитлер», «эпштейн лучше гитлера», «эпштейн "
        "хотя бы не убивал евреев», «гитлер лучше эпштейна»), одобрение Эпштейна и секс-насилия "
        "над детьми, педофилии, инцеста («люблю инцест», «педофилы правы»), убийств и "
        "террора. Такое — нарушение 4.5 (девиантность/пропаганда), severity high.\n"
        "Не причисляй к этому брань в адрес этих людей (сам по себе мат не нарушение), а также "
        "нейтральные/осуждающие упоминания (статьи, обсуждения, «лучше гитлера» без апологии "
        "собеседника — не нарушение).\n"
        + dismissed_block +
        f"Второе задание: если violation=true, переведи сообщение на {lang_name} — верно по "
        f"смыслу и тону, с сохранением реестра и МАТА (не цензурируй), идиоматично, без пояснений "
        f"и кавычек. Если violation=false — translation=null.\n"
        "Respond ONLY with a JSON object:\n"
        "{\"violation\": boolean, \"rule_id\": \"X.Y\" or null, \"severity\": \"low|medium|high\" "
        "or null, \"reason\": \"short reason in Russian\" or \"\", "
        "\"translation\": \"translation\" or null}\n"
        "If no violation, return {\"violation\": false, \"rule_id\": null, \"severity\": null, "
        "\"reason\": \"\", \"translation\": null}."
    )
    user_payload = (
        f"Context (recent chat / reply):\n{ctx_clean}\n\nCurrent message: {text}"
        if ctx_clean else f"Message: {text}"
    )
    try:
        content = await llm.complete(sys_prompt, user_payload,
                                     json_mode=True, max_tokens=800)
    except Exception as e:
        print(f"[LLM] ошибка вызова: {type(e).__name__}: {e}")
        return None, None

    parsed = _parse_llm_json(content, source=text)
    if parsed is None:
        return None, None

    translation = parsed.get("translation") or None
    if not parsed.get("violation"):
        _cache_set(key, (None, None))
        return None, None

    rule_id = parsed.get("rule_id")
    if rule_id not in rules.RULES:
        rule_id = "4.0"
    result = DetectionResult(
        rule_id=rule_id,
        method="llm",
        severity=parsed.get("severity") or "medium",
        reason=parsed.get("reason") or "LLM-обнаружение",
    )
    _cache_set(key, (result, translation))
    return result, translation


# ---- Контроль профилей, никнеймов и статусов (правила 2.2, 2.3, 3.7) ----

# Признаки выдачи себя за администрацию/модерацию (правило 2.2)
_IMPERSONATION_RE = re.compile(
    r"(?:"
    r"[\[\(\{<]\s*(?:admin|administrator|mod|moderator|owner|staff|dev|админ|администратор|модер|модератор|владелец|создатель)\s*[\]\)\}>]"
    r"|\b(?:главный\s+админ\w*|администратор\s+сервера|модератор\s+сервера|владелец\s+сервера|официальный\s+модер\w*|техподдержка\s+macronux|macronux\s+staff|server\s+admin|server\s+owner|official\s+moderator)\b"
    r")",
    re.IGNORECASE,
)

# Признаки 18+ / шок-контента в никнеймах и статусах (правило 2.3)
_NSFW_PROFILE_RE = re.compile(
    r"(?:"
    r"pornhub|onlyfans|xvideos|xnxx|brazzers|rule34|r34|nsfw|18\+"
    r"|порно|хентай|hentai|расчленен\w*|gore|snuff|детское\s+порно|\bцп\b"
    r")",
    re.IGNORECASE,
)


def _is_member_staff(member, mod_role_id: Optional[int] = None) -> bool:
    """Проверяет, является ли участник реальным администратором/модератором сервера."""
    perms = getattr(member, "guild_permissions", None)
    if perms and (
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "ban_members", False)
        or getattr(perms, "kick_members", False)
        or getattr(perms, "moderate_members", False)
        or getattr(perms, "manage_messages", False)
    ):
        return True
    if mod_role_id:
        for role in getattr(member, "roles", []) or []:
            if getattr(role, "id", None) == mod_role_id:
                return True
    return False


def _extract_member_status(member) -> str:
    """Извлекает текст пользовательского статуса (CustomActivity) участника."""
    parts = []
    for act in getattr(member, "activities", None) or []:
        state = getattr(act, "state", None)
        name = getattr(act, "name", None)
        if state and isinstance(state, str):
            parts.append(state)
        elif name and isinstance(name, str) and name.lower() != "custom status":
            parts.append(name)
    return " | ".join(parts)


def check_member_profile(member, guild_banwords=None,
                         mod_role_id: Optional[int] = None) -> Optional[DetectionResult]:
    """Проверяет никнейм, отображаемое имя и кастомный статус участника по правилам Macronux™:
      - 2.2 (Выдача себя за администрацию: теги [ADMIN]/[MOD] или копирование имени стаффа);
      - 2.3 (Шок-контент и 18+ в никнеймах и статусах);
      - 3.7 / 4.x (Оскорбительные, нечитаемые Zalgo-никнеймы и банворды в имени или статусе).
    """
    if getattr(member, "bot", False):
        return None

    display_name = (getattr(member, "display_name", "") or "").strip()
    username = (getattr(member, "name", "") or "").strip()
    global_name = (getattr(member, "global_name", "") or "").strip()
    status_text = _extract_member_status(member).strip()
    combined = " | ".join(p for p in (display_name, global_name, status_text) if p)
    if not combined:
        return None

    is_staff = _is_member_staff(member, mod_role_id=mod_role_id)

    # 1. Правило 2.2: Выдача себя за администрацию
    if not is_staff:
        m_imp = _IMPERSONATION_RE.search(combined)
        if m_imp:
            return DetectionResult(
                rule_id="2.2",
                method="regex",
                severity="high",
                reason=f"Выдача себя за администрацию в профиле: «{m_imp.group(0).strip()}»",
            )
        # Проверка на прямое копирование никнейма реального администратора/модератора сервера
        guild = getattr(member, "guild", None)
        if guild is not None and display_name:
            norm_dn = normalize_for_check(_normalize(display_name)).strip()
            if len(norm_dn) >= 3:
                for other in getattr(guild, "members", None) or []:
                    if getattr(other, "id", None) == getattr(member, "id", None) or getattr(other, "bot", False):
                        continue
                    if _is_member_staff(other, mod_role_id=mod_role_id):
                        staff_dn = normalize_for_check(_normalize(getattr(other, "display_name", "") or "")).strip()
                        staff_un = normalize_for_check(_normalize(getattr(other, "name", "") or "")).strip()
                        if norm_dn and (norm_dn == staff_dn or norm_dn == staff_un):
                            return DetectionResult(
                                rule_id="2.2",
                                method="regex",
                                severity="high",
                                reason=f"Копирование никнейма модератора/администратора ({other.display_name})",
                            )

    # 2. Правило 2.3: Шок-контент и 18+ в никнейме или статусе
    m_nsfw = _NSFW_PROFILE_RE.search(combined)
    if m_nsfw:
        return DetectionResult(
            rule_id="2.3",
            method="regex",
            severity="high",
            reason=f"18+ / шок-контент в профиле или статусе: «{m_nsfw.group(0).strip()}»",
        )

    # 3. Правило 3.7: Нечитаемый никнейм (Zalgo или отсутствие читаемых символов)
    if display_name and len(ZALGO_RE.findall(display_name)) > 3:
        return DetectionResult(
            rule_id="3.7",
            method="regex",
            severity="low",
            reason=f"Нечитаемый никнейм (Zalgo-символы): «{display_name[:40]}»",
        )

    # 4. Правило 3.7 / Раздел 4: Запрещённые слова, оскорбления или реклама в имени/статусе
    for label, text_part in (("никнейме", display_name or username), ("статусе", status_text)):
        if not text_part:
            continue
        res = regex_check(text_part, guild_banwords)
        if res and res.detected():
            # Флуд капсом в коротком нике не считаем нарушением 3.6, остальное (4.x, 3.1, 3.3, 3.4) — ловим
            if res.rule_id == "3.6":
                continue
            target_rule = res.rule_id if str(res.rule_id).startswith("4.") else "3.7"
            return DetectionResult(
                rule_id=target_rule,
                method="regex",
                severity=res.severity,
                reason=f"Нарушение в {label} (п. 3.7 / {res.rule_id}): {res.reason}",
            )

    return None


def _parse_llm_json(content: str, source: str = "") -> Optional[dict]:
    """Безопасный парсинг JSON-ответа LLM: не бросаем исключений, логируем сырой
    ответ в телеметрию при сбое/отказе, возвращаем None."""
    from bot.telemetry import log_event
    low = (content or "").strip().lower()
    refusal = any(m in low for m in (
        "sorry", "cannot assist", "can't assist", "as a language model", "i'm an ai",
        "not able to", "does not support", "blocked", "violate",
    )) and "{" not in content
    if refusal or not content:
        log_event("llm_refusal", source_text=(source or "")[:300], raw=(content or "")[:500])
        print(f"[LLM] отказ модели на {source[:60]!r}: {content[:150]!r}")
        return None
    try:
        obj = json.loads(content)  # strict JSON (response_format json_object)
    except Exception:
        try:
            obj = json.loads(_extract_json(content))
        except Exception:
            log_event("llm_parse_error", source_text=(source or "")[:300], raw=(content or "")[:1000])
            print(f"[LLM] не удалось распарсить JSON на {source[:60]!r}: {content[:200]!r}")
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("violation"), bool):
        log_event("llm_parse_error", source_text=(source or "")[:300], raw=(content or "")[:1000])
        return None
    return obj


def _extract_json(text: str) -> str:
    """Extract JSON object from LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _compact_rules_for_llm() -> str:
    lines = []
    for rule_id, rule in rules.RULES.items():
        pun = f" | наказание: {rule['punishment']}" if rule.get("punishment") else ""
        lines.append(f"{rule_id} {rule['title']}: {rule['text']}{pun}")
    return "\n".join(lines)

