import asyncio
from bot import moderation

async def main():
    res, trans = await moderation.llm_check_with_translation(
        "Ты ИДИОТ, придурок тупой", None, target_lang="en")
    print("res:", res)
    print("trans:", trans)

    res2, trans2 = await moderation.llm_check_with_translation(
        "привет как дела", None, target_lang="en")
    print("res2:", res2, "trans2:", trans2)

asyncio.run(main())
