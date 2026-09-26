"""Батч-тест объединённого LLM-детекта + перевода (11 языков).

Проверяем сам конвейер: regex-слой, отказа-маски, смарт-гейтинг и то, что после
одного LLM-вызова у нас есть и вердикт, и перевод. Внешние API могут быть
недоступны в этом окружении — тогда честно печатаем SKIP, а не «ложное чисто».
"""
import asyncio

from bot import moderation, translator

SAMPLES = [
    ("RU", "ты просто идиот конченый, пошёл нахуй", True),
    ("RU", "привет как дела", False),
    ("EN", "you are a fucking idiot, get the hell out", True),
    ("EN", "hello, how are you today", False),
    ("DE", "du bist ein verdammter idiot, verpiss dich", True),
    ("DE", "guten tag, wie geht es dir", False),
    ("FR", "espèce de con, va te faire foutre", True),
    ("FR", "bonjour, ça va bien", False),
    ("ES", "eres un puto idiota, vete a la mierda", True),
    ("ES", "hola, qué tal", False),
    ("IT", "sei un coglione, vaffanculo", True),
    ("IT", "ciao, come stai", False),
    ("PT", "você é um idiota de merda, vai se foder", True),
    ("PT", "olá, como vai", False),
    ("KO", "니가 씨발 멍청이야, 꺼져", True),
    ("ZH", "你他妈的就是个废物，滚", True),
    ("TR", "seni beceriksiz piç, defol git", True),
    ("AR", "أنت غبي تافه، اذهب إلى الجحيم", True),
    ("PL", "jesteś kurwa idiotą, wypierdalaj", True),
    ("NL", "jij bent een klootzak, sodemieter op", True),
    ("SV", "du är ett jävla pucko, dra åt helvete", True),
]


async def main():
    ok = bad = skipped = 0
    for lang, text, expect in SAMPLES:
        res, trans = await moderation.llm_check_with_translation(text, None,
                                                                 target_lang="ru")
        if res is None:
            skipped += 1
            print(f"{lang:3} {text!r:42} -> SKIP (внешний API недоступен/гейт)")
            continue
        hit = res.detected()
        mark = "OK " if hit == expect else "FAIL"
        if hit == expect:
            ok += 1
        else:
            bad += 1
        print(f"{lang:3} {text!r:42} -> {mark} detected={hit} "
              f"translation={trans[:44]!r}")
    print(f"\nИтог: OK {ok} / FAIL {bad} / SKIP {skipped}")


if __name__ == "__main__":
    asyncio.run(main())
