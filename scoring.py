"""Оценка «подозрительности» подписчика. Чистая функция — легко тестировать."""
import re

URL_RE = re.compile(r"(https?://|t\.me/|@\w{4,}|www\.|\.(com|ru|xyz|top|click)\b)", re.I)

# Баллы за каждый признак
POINTS = {
    "deleted": 100,     # удалённый аккаунт
    "fake": 100,        # флаг Telegram «fake»
    "scam": 100,        # флаг Telegram «scam»
    "no_photo": 2,      # нет аватарки
    "no_username": 1,   # нет @username
    "never_seen": 2,    # статус «давно/никогда не был в сети»
    "spam_name": 3,     # ссылка/реклама в имени
    "no_last_name": 0,  # справочно, не учитывается
}

LABELS = {
    "deleted": "удалённый аккаунт",
    "fake": "помечен Telegram как fake",
    "scam": "помечен Telegram как scam",
    "no_photo": "нет аватарки",
    "no_username": "нет username",
    "never_seen": "давно не в сети",
    "spam_name": "ссылка/реклама в имени",
}


def evaluate(user) -> tuple[int, list[str]]:
    """Возвращает (баллы, список_кодов_причин)."""
    reasons: list[str] = []

    if getattr(user, "deleted", False):
        return POINTS["deleted"], ["deleted"]
    if getattr(user, "fake", False):
        reasons.append("fake")
    if getattr(user, "scam", False):
        reasons.append("scam")
    if not getattr(user, "photo", None):
        reasons.append("no_photo")
    if not getattr(user, "username", None):
        reasons.append("no_username")
    status = getattr(user, "status", None)
    if status is None or type(status).__name__ == "UserStatusEmpty":
        reasons.append("never_seen")
    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}"
    if URL_RE.search(name):
        reasons.append("spam_name")

    return sum(POINTS[r] for r in reasons), reasons
