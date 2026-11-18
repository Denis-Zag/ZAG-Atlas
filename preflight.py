"""Проверка живой модели без вывода ключа."""
import asyncio
import json
from app.ai import answer
from app import storage


async def main():
    result, error, meta = await answer(
        "Что уточняет менеджер при создании заявки?",
        "При создании заявки менеджер уточняет адрес и вид работ.")
    print(json.dumps({"model_result": result.model_dump(), "error": error,
                      "meta": meta}, ensure_ascii=False, indent=2))
    if error or result.needs_review:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
