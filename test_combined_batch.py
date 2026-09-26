import asyncio
import sys
import os
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(__file__))

from bot import moderation

SAMPLES = [
    ("RU", "иди ты нахуй, мразь", True),
    ("EN", "you are a fucking pathetic piece of trash", True),
    ("FR", "espèce de connard, va te faire foutre", True),
    ("DE", "du verdammtes arschloch, halt die fresse", True),
    ("ES", "qué pendejo eres, vete a la mierda", True),
    ("IT", "testa di cazzo, vaffanculo", True),
    ("PT", "seu idiota de merda, vai se foder", True),
    ("KO", "씨발놈아 이 개새끼야", True),
    ("ZH", "你他妈的就是个废物", True),
    ("TR", "seni gibin oruspu çocuğu", True),
    ("AR", "يا ابن الكلب الحقير", True),
    ("RU", "привет как дела", False),
    ("EN", "hello, how are you today", False),
    ("KO", "안녕하세요 오늘 날씨 좋네요", False),
]

async def main():
    ok = bad = skipped = 0
    for lang, text, expect in SAMPLES:
        res, trans = await moderation.llm_check_with_translation(text, None, target_lang="ru")
        if res is None:
            # короткие/отказ/гейт — считаем «пропуском» только для негатива
            verdict = "SKIP"
        else:
            verdict = "DETECTED" if res.detected() else "CLEAN"
        if verdict == "SKIP":
            skipped += 1
            print(f"{lang:4} {text[:38]!r:40} -> SKIP")
            continue
        hit = (verdict == "DETECTED") == expect
        # для негативов проверяем, что перевод не появился
        if not expect and trans:
            print(f"{lang:4} {text[:38]!r:40} -> {verdict} trans={trans!r}  <-- перевод без нарушения!")
            bad += 1
            continue
        ok += int(hit)
        bad += int(not hit)
        print(f"{lang:4} {text[:38]!r:40} -> {verdict} {'OK' if hit else 'FAIL'}"
              + (f" reason={res.reason[:40]!r} trans={trans[:30]!r}" if trans else ""))
    print(f"\nИтог: OK {ok} / FAIL {bad} / SKIP {skipped}")

if __name__ == "__main__":
    asyncio.run(main())
