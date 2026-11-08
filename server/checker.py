"""
Умная проверка состава продукта на аллергены.

Алгоритм (его можно рассказать на защите по шагам):

  1. НОРМАЛИЗАЦИЯ: переводим текст в нижний регистр, «ё» -> «е», убираем знаки
     препинания, склеиваем перенесённые слова («мо-\\nлоко» -> «молоко»).
  2. РАЗБИВКА: режем состав на ингредиенты по запятым, скобкам, точкам с запятой.
     Запоминаем, где стоит фраза «может содержать следы» - всё после неё это «следы».
  3. ПОИСК: для каждого аллергена пользователя ищем в каждом ингредиенте
        a) прямые слова из словаря          -> прямое совпадение
        b) связанные слова (производные)    -> возможное совпадение
        c) слова, похожие на прямые на одну
           букву (ошибка OCR)               -> возможное совпадение
  4. ПРАВИЛА БЕЗОПАСНОСТИ:
        - «может содержать следы ...»       -> только возможное совпадение
        - «без молока», «gluten free»       -> только возможное, проверить вручную
          (мы не верим надписям «без», пока человек не проверил)
  5. ИТОГ: прямое > возможное > нет совпадений. Если данных мало - «недостаточно
     информации», а не «безопасно».
"""

import re

from allergen_dictionary import ALLERGENS

# ---------- Статусы результата ----------

STATUS_FOUND = "ALLERGEN_FOUND"
STATUS_POSSIBLE = "POSSIBLE_MATCH"
STATUS_NONE = "NO_MATCH"
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"

STATUS_TEXT = {
    STATUS_FOUND: "Обнаружен аллерген",
    STATUS_POSSIBLE: "Возможное совпадение",
    STATUS_NONE: "Совпадений не найдено в проверенном составе",
    STATUS_INSUFFICIENT: "Недостаточно информации для проверки",
}

DISCLAIMER = (
    "AllScaner - информационный помощник, а не медицинское устройство. "
    "Результат не гарантирует безопасность продукта: всегда читайте этикетку "
    "и при необходимости консультируйтесь с врачом."
)

# Фразы, после которых перечисляются «следы» аллергенов.
TRACE_MARKERS = [
    "может содержать", "могут содержать", "следы", "следов", "возможно содержит",
    "производится на", "произведено на", "изготовлено на", "изготавливается на",
    "на оборудовании", "на предприятии", "в цехе",
    "may contain", "traces of", "trace of", "produced in a facility",
    "manufactured in a facility", "processed in a facility", "same facility",
    "produced on equipment", "shared equipment",
]

# Слова-отрицания: «без молока», «lactose free».
NEGATION_TOKENS = {"free", "without", "no"}

MIN_CHARS_FOR_CHECK = 15      # короче - проверять нечего
MIN_CHARS_FOR_FULL = 40       # короче - предупреждаем, что состав, возможно, неполный


# ---------- Шаг 1. Нормализация ----------

def normalize(text: str) -> str:
    """Приводит текст к виду, удобному для поиска."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"-\s*\n\s*", "", t)                       # мо-\nлоко -> молоко
    # E-коды: «Е 322», «е322» (русская е) -> «e322»
    t = re.sub(r"(?<![a-zа-я0-9])[eе]\s?-?(\d{3,4})(?!\d)", r"e\1", t)
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)                  # всё, кроме букв и цифр, -> пробел
    return t.strip()


# ---------- Шаг 1b. Простейший "стемминг" (отрезаем окончание) ----------

_RU_ENDINGS = [
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ов", "ев", "ей", "ий",
    "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ом", "ем", "ам",
    "ям", "ах", "ях", "ую", "юю", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь",
]


def stem_word(word: str) -> str:
    """молока -> молок, яйцо -> яйц, peanuts -> peanut (грубо, но просто и понятно)."""
    if re.fullmatch(r"[a-z0-9]+", word):
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word
    for ending in _RU_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


# ---------- Подготовка шаблонов поиска ----------

def _pattern(stem: str) -> "re.Pattern[str]":
    """
    Превращает запись словаря в регулярное выражение.
      "молок"  -> слово начинается с "молок"
      "=сыр"   -> слово ровно "сыр"
      "a b"    -> несколько слов подряд (каждое - начало слова)
    """
    exact = stem.startswith("=")
    words = [re.escape(w) for w in stem.lstrip("=").split()]
    if exact:
        return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)")
    return re.compile(r"(?<!\w)" + r"\s+".join(w + r"\w*" for w in words))


class Profile:
    """Всё, что нужно знать об одном аллергене пользователя."""

    def __init__(self, user_name, group, direct, related, exclude):
        self.user_name = user_name          # как аллерген записал пользователь
        self.group = group                  # название группы словаря или None (свой аллерген)
        self.direct = direct                # [(слово, шаблон)]
        self.related = related              # [(слово, шаблон, пояснение)]
        self.exclude = exclude              # [шаблон]


def _build_group_profile(user_name: str, group_name: str) -> Profile:
    g = ALLERGENS[group_name]
    return Profile(
        user_name,
        group_name,
        [(s, _pattern(s)) for s in g["direct"]],
        [(s, _pattern(s), note) for s, note in g["related"].items()],
        [_pattern(s) for s in g["exclude"]],
    )


def _find_group(norm_name: str):
    """Ищет группу словаря по названию, которое ввёл пользователь."""
    user_stems = [stem_word(w) for w in norm_name.split()]
    whole = " ".join(user_stems)

    # 1) совпадение всей фразы с названием группы или её псевдонимом
    for group_name, g in ALLERGENS.items():
        for alias in [group_name] + g["aliases"]:
            alias_stems = " ".join(stem_word(w) for w in normalize(alias).split())
            if alias_stems == whole:
                return group_name

    # 2) совпало одно слово («куриные яйца» -> «яйц»); берём, только если группа одна
    hits = set()
    for group_name, g in ALLERGENS.items():
        for alias in [group_name] + g["aliases"]:
            a = [stem_word(w) for w in normalize(alias).split()]
            if len(a) == 1 and len(a[0]) >= 4 and a[0] in user_stems:
                hits.add(group_name)
    if len(hits) == 1:
        return hits.pop()
    return None


def build_profile(user_name: str) -> Profile:
    """Находит группу в словаре, а если её нет - ищет просто слово пользователя."""
    norm = normalize(user_name)
    group = _find_group(norm)
    if group:
        return _build_group_profile(user_name, group)

    # Свой аллерген: ищем введённые слова (отрезав окончания).
    words = norm.split()
    if not words:
        return Profile(user_name, None, [], [], [])
    if len(words) == 1 and len(stem_word(words[0])) < 4:
        stem = "=" + words[0]                 # короткое слово ищем целиком
    else:
        # Отрезаем ещё одну букву у длинных слов, чтобы не мешало чередование
        # букв в корне (клубника -> клубничное, орех -> ореховый).
        parts = []
        for w in words:
            s = stem_word(w)
            parts.append(s[:-1] if len(s) >= 7 else s)
        stem = " ".join(parts)
    return Profile(user_name, None, [(stem, _pattern(stem))], [], [])


# ---------- Шаг 2. Разбиение состава ----------

def split_composition(text: str):
    """
    Возвращает список ингредиентов: (исходный_текст, нормализованный, это_след).
    Всё после фразы «может содержать» считается «следами» до конца предложения.
    """
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    # предложения: точка/!/? в конце слова (но не в числе 3.5)
    sentences = re.split(r"[.!?](?=\s|$)", text)
    result = []
    for sentence in sentences:
        in_trace = False
        for part in re.split(r"[,;:()\[\]{}•|*]", sentence):
            raw = part.strip()
            norm = normalize(raw)
            if not norm:
                continue
            padded = " " + norm + " "
            if any(" " + normalize(m) in padded for m in TRACE_MARKERS):
                in_trace = True
            result.append((raw, norm, in_trace))
    return result


# ---------- Шаг 3. Поиск совпадений ----------

def _levenshtein(a: str, b: str) -> int:
    """Сколько букв нужно заменить, вставить или удалить, чтобы получить из a слово b."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_negated(norm: str) -> bool:
    for token in norm.split():
        if token in NEGATION_TOKENS:
            return True
        if token.startswith("без") and token != "безе":   # «безе» - это пирожное
            return True
    return "не содержит" in norm


def _short(text: str, limit: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _match_fragment(profile: Profile, raw: str, norm: str, is_trace: bool):
    """Проверяет один ингредиент на один аллерген. Возвращает словарь или None."""
    text = norm
    for rx in profile.exclude:                       # вырезаем «кокосовое молоко» и т.п.
        text = rx.sub(" ", text)

    kind = None          # "direct" | "related" | "fuzzy"
    word = None
    note = None
    stem_hit = None

    for stem, rx in profile.direct:
        m = rx.search(text)
        if m:
            kind, word, stem_hit = "direct", m.group(0), stem
            break

    if kind is None:
        for stem, rx, why in profile.related:
            m = rx.search(text)
            if m:
                kind, word, note = "related", m.group(0), why
                break

    if kind is None:                                  # возможная ошибка OCR
        for token in text.split():
            if len(token) < 6:
                continue
            for stem, _ in profile.direct:
                s = stem.lstrip("=")
                if stem.startswith("=") or " " in s or len(s) < 6:
                    continue
                if token[: len(s)] != s and _levenshtein(token[: len(s)], s) == 1:
                    kind, word, stem_hit = "fuzzy", token, s
                    break
            if kind:
                break

    if kind is None:
        return None

    name = profile.user_name
    ingredient = _short(raw)
    match_type = "possible"

    if kind == "direct":
        explanation = (f"В ингредиенте «{ingredient}» найдено «{word}» - "
                       f"это прямое совпадение с аллергеном «{name}».")
        match_type = "direct"
    elif kind == "related":
        explanation = (f"В ингредиенте «{ingredient}» найдено «{word}»: {note}. "
                       f"Это возможное совпадение с аллергеном «{name}», "
                       f"точный источник неизвестен.")
    else:
        explanation = (f"Слово «{word}» в ингредиенте «{ingredient}» похоже на «{stem_hit}» "
                       f"(аллерген «{name}»). Возможна ошибка распознавания текста, "
                       f"проверьте этикетку.")

    # Правила безопасности: не объявляем доказанным то, в чём не уверены.
    if is_trace:
        match_type = "possible"
        explanation += (" Этот ингредиент указан среди возможных следов "
                        "(«может содержать»): производитель предупреждает о риске.")
    elif kind == "direct" and _is_negated(norm):
        match_type = "possible"
        explanation += (" Рядом есть пометка «без»/«free», но мы не считаем её "
                        "надёжной: проверьте этикетку вручную.")

    return {
        "allergen": name,
        "ingredient": ingredient,
        "match_type": match_type,
        "explanation": explanation,
    }


# ---------- Шаг 5. Главная функция ----------

def check_composition(allergens, composition: str) -> dict:
    """
    allergens   - список аллергенов пользователя, например ["молоко", "арахис"]
    composition - текст состава (из OCR или Open Food Facts)
    Возвращает словарь: status, status_text, matches, warnings, disclaimer.
    """
    warnings = []
    matches = []

    names = [a.strip() for a in allergens if a and a.strip()]
    composition = composition or ""
    norm_all = normalize(composition)

    if not names:
        warnings.append("Список ваших аллергенов пуст. Добавьте аллергены в разделе "
                        "«Мои аллергены», иначе проверять нечего.")
        return _response(STATUS_INSUFFICIENT, matches, warnings)

    if len(norm_all) < MIN_CHARS_FOR_CHECK:
        warnings.append("Состав пустой или слишком короткий. Попробуйте сфотографировать "
                        "этикетку ещё раз или проверьте состав вручную.")
        return _response(STATUS_INSUFFICIENT, matches, warnings)

    if len(norm_all) < MIN_CHARS_FOR_FULL:
        warnings.append("Состав очень короткий: возможно, он распознан не полностью.")

    fragments = split_composition(composition)
    seen = set()
    for name in names:
        profile = build_profile(name)
        if not profile.direct and not profile.related:
            warnings.append(f"Аллерген «{name}» не удалось разобрать для поиска.")
            continue
        for raw, norm, is_trace in fragments:
            found = _match_fragment(profile, raw, norm, is_trace)
            if not found:
                continue
            key = (found["allergen"], normalize(found["ingredient"]), found["match_type"])
            if key in seen:
                continue
            seen.add(key)
            matches.append(found)

    if any(m["match_type"] == "direct" for m in matches):
        status = STATUS_FOUND
    elif matches:
        status = STATUS_POSSIBLE
    else:
        status = STATUS_NONE
        warnings.append("Проверен только тот текст, который есть в составе. Если состав "
                        "неполный или нечёткий, продукт всё равно может содержать аллерген.")

    return _response(status, matches, warnings)


def _response(status, matches, warnings):
    return {
        "status": status,
        "status_text": STATUS_TEXT[status],
        "matches": matches,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }
