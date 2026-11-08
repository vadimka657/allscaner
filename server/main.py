"""
FastAPI-сервер AllScaner.

Запуск (из папки server):
    uvicorn main:app --host 0.0.0.0 --port 8000

Проверка в браузере: http://localhost:8000/docs
"""

from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from checker import check_composition

app = FastAPI(title="AllScaner API", version="1.0")


# ---------- Модели данных (Pydantic проверяет, что JSON правильный) ----------

class CheckRequest(BaseModel):
    allergens: List[str] = Field(default_factory=list, max_length=100,
                                 description="Аллергены пользователя")
    composition: str = Field(default="", max_length=20000,
                             description="Текст состава продукта")
    product_name: Optional[str] = Field(default=None, max_length=300,
                                        description="Название продукта, если известно")


class Match(BaseModel):
    allergen: str        # какой аллерген пользователя сработал
    ingredient: str      # в каком ингредиенте найдено
    match_type: str      # "direct" - прямое, "possible" - возможное
    explanation: str     # объяснение для пользователя


class CheckResponse(BaseModel):
    status: str          # ALLERGEN_FOUND / POSSIBLE_MATCH / NO_MATCH / INSUFFICIENT_DATA
    status_text: str     # то же самое по-русски
    matches: List[Match]
    warnings: List[str]
    disclaimer: str


# ---------- Эндпоинты ----------

@app.get("/health")
def health():
    """Android вызывает это, чтобы проверить, что сервер запущен."""
    return {"status": "ok"}


@app.post("/check", response_model=CheckResponse)
def check(request: CheckRequest):
    """Проверяет состав на аллергены пользователя."""
    return check_composition(request.allergens, request.composition)
