"""Channel Cleaner — чистит Telegram-канал от фейковых/пустых подписчиков.

Схема: Bot API не умеет получать список подписчиков, поэтому работают два клиента:
  * user-клиент (ваш аккаунт-админ канала) — читает подписчиков и удаляет их;
  * бот — интерфейс управления (/scan, кнопки подтверждения).
"""
import asyncio
import io
import json
import os
import re
from collections import Counter
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, ChannelParticipantAdmin, ChannelParticipantCreator

from scoring import LABELS, evaluate

_here = Path(__file__).resolve().parent
# .env может не загрузиться на хосте (файлы с точкой часто игнорируются), поэтому есть settings.env
for _name in (".env", "settings.env"):
    load_dotenv(_here / _name)
try:
    import local_config  # noqa: F401  — необязательный локальный файл с запасными значениями (в репозиторий не кладём)
except ImportError:
    pass
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}
DEFAULT_THRESHOLD = int(os.environ.get("THRESHOLD", "4"))
KICK_DELAY = float(os.environ.get("KICK_DELAY", "1.0"))

# На хостах с постоянным диском (Amvera: /data) сессия должна лежать там, иначе слетит при обновлении
DATA = Path(os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else "data"))
DATA.mkdir(parents=True, exist_ok=True)
WL_FILE = DATA / "whitelist.json"

SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
# На хосте нет консоли для ввода кода, поэтому сессия берётся из SESSION_STRING (см. gen_session.py)
user_client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else str(DATA / "user"), API_ID, API_HASH
)
bot = TelegramClient(StringSession(), API_ID, API_HASH)

# последний скан на владельца: {owner_id: {"channel": entity, "ids": [..], "stats": Counter}}
scans: dict[int, dict] = {}
settings = {"threshold": DEFAULT_THRESHOLD, "ban": False}  # ban=True — не пускать обратно
busy: set[int] = set()


def load_wl() -> set[int]:
    try:
        return set(json.loads(WL_FILE.read_text()))
    except Exception:
        return set()


def save_wl(wl: set[int]) -> None:
    WL_FILE.write_text(json.dumps(sorted(wl)))


def owner_only(handler):
    async def wrap(event):
        if event.sender_id not in OWNERS:
            return
        await handler(event)
    return wrap


# ---------- вход в аккаунт прямо из бота ----------
login_state: dict[int, dict] = {}  # {owner_id: {"step": "code"|"password"|"qr", ...}}


async def _qr_flow(chat_id: int, oid: int):
    """Показывает QR, ждёт сканирования; при устаревании обновляет (до 4 раз)."""
    try:
        qr = await user_client.qr_login()
        for _ in range(4):
            buf = io.BytesIO()
            qrcode.make(qr.url).save(buf, format="PNG")
            buf.seek(0)
            buf.name = "qr.png"
            await bot.send_file(
                chat_id, buf,
                caption="Telegram на телефоне → Настройки → Устройства → Подключить устройство → "
                        "наведите камеру на этот QR. Действует ~1 минуту.",
            )
            try:
                await qr.wait(60)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
            except SessionPasswordNeededError:
                login_state[oid] = {"step": "password"}
                return await bot.send_message(chat_id, "🔐 Включена двухэтапная защита. Отправьте пароль (сообщение сразу удалится).")
        else:
            login_state.pop(oid, None)
            return await bot.send_message(chat_id, "QR не отсканировали. Повторите /login.")
        login_state.pop(oid, None)
        me = await user_client.get_me()
        await bot.send_message(chat_id, f"✅ Вошёл как {me.first_name} (id {me.id}). Теперь можно /scan.")
    except Exception as e:
        login_state.pop(oid, None)
        await bot.send_message(chat_id, f"Ошибка входа: {e}")


@bot.on(events.NewMessage(pattern=r"^/login(?:\s+(\+?\d[\d\s\-]{5,}))?\s*$"))
@owner_only
async def cmd_login(event):
    oid = event.sender_id
    if await user_client.is_user_authorized():
        me = await user_client.get_me()
        return await event.respond(f"Уже вошёл как {me.first_name}. Для смены аккаунта: /logout")
    phone = event.pattern_match.group(1)
    if not phone:
        login_state[oid] = {"step": "qr"}
        asyncio.create_task(_qr_flow(event.chat_id, oid))
        return
    phone = "+" + re.sub(r"\D", "", phone)
    try:
        sent = await user_client.send_code_request(phone)
    except Exception as e:
        return await event.respond(f"Не удалось отправить код: {e}\nПопробуйте вход по QR: /login")
    login_state[oid] = {"step": "code", "phone": phone, "hash": sent.phone_code_hash}
    await event.respond(
        "Код отправлен сообщением в приложение Telegram (чат «Telegram»).\n"
        "Отправьте его сюда **с пробелами или дефисами**, например `1 2 3 4 5`: "
        "если написать цифры слитно, Telegram аннулирует код."
    )


@bot.on(events.NewMessage(pattern=r"^/logout$"))
@owner_only
async def cmd_logout(event):
    await user_client.log_out()
    await user_client.connect()
    await event.respond("Вышел из аккаунта. /login — войти снова.")


@bot.on(events.NewMessage(func=lambda e: e.is_private and e.sender_id in login_state
                          and not (e.raw_text or "").startswith("/")))
async def on_login_input(event):
    oid = event.sender_id
    if oid not in OWNERS:
        return
    st = login_state.get(oid)
    text = event.raw_text.strip()
    try:
        if st["step"] == "code":
            code = re.sub(r"\D", "", text)
            await user_client.sign_in(st["phone"], code, phone_code_hash=st["hash"])
        elif st["step"] == "password":
            await user_client.sign_in(password=text)
        else:
            return
    except SessionPasswordNeededError:
        login_state[oid] = {"step": "password"}
        await event.respond("🔐 Включена двухэтапная защита. Отправьте пароль (сообщение сразу удалится).")
        return
    except Exception as e:
        login_state.pop(oid, None)
        await event.respond(f"Ошибка: {e}\nНачните заново: /login")
        return
    finally:
        try:
            await event.delete()  # не оставляем код/пароль в чате
        except Exception:
            pass
    login_state.pop(oid, None)
    me = await user_client.get_me()
    await bot.send_message(event.chat_id, f"✅ Вошёл как {me.first_name} (id {me.id}). Теперь можно /scan.")


@bot.on(events.NewMessage(pattern=r"^/(start|help)"))
@owner_only
async def cmd_start(event):
    authed = await user_client.is_user_authorized()
    await event.respond(
        "🧹 **Channel Cleaner**\n\n"
        + ("" if authed else "⚠️ Сначала войдите в аккаунт-админ канала: /login (по QR) "
                             "или `/login +79991234567`.\n\n")
        + "1. Сделайте аккаунт админом канала с правом «Блокировка пользователей».\n"
        "2. `/scan @channel` — проверить подписчиков (ничего не удаляет).\n"
        "3. Посмотрите отчёт и нажмите кнопку удаления.\n\n"
        f"`/threshold N` — порог баллов (сейчас {settings['threshold']}). Удалённый/fake/scam = сразу, "
        "нет фото +2, нет username +1, давно не в сети +2, реклама в имени +3.\n"
        "`/mode kick|ban` — kick: удалить, но можно вернуться; ban: бан навсегда.\n"
        "`/whitelist add|del <id>` / `/whitelist` — защищённые пользователи."
    )


@bot.on(events.NewMessage(pattern=r"^/threshold\s+(\d+)"))
@owner_only
async def cmd_threshold(event):
    settings["threshold"] = int(event.pattern_match.group(1))
    await event.respond(f"Порог: {settings['threshold']}")


@bot.on(events.NewMessage(pattern=r"^/mode\s+(kick|ban)"))
@owner_only
async def cmd_mode(event):
    settings["ban"] = event.pattern_match.group(1) == "ban"
    await event.respond("Режим: " + ("🚫 бан навсегда" if settings["ban"] else "👢 kick (можно вернуться)"))


@bot.on(events.NewMessage(pattern=r"^/whitelist(?:\s+(add|del)\s+(\d+))?\s*$"))
@owner_only
async def cmd_wl(event):
    wl = load_wl()
    action, uid = event.pattern_match.group(1), event.pattern_match.group(2)
    if action == "add":
        wl.add(int(uid))
        save_wl(wl)
    elif action == "del":
        wl.discard(int(uid))
        save_wl(wl)
    await event.respond("Whitelist: " + (", ".join(map(str, sorted(wl))) or "пусто"))


@bot.on(events.NewMessage(pattern=r"^/scan\s+(\S+)"))
@owner_only
async def cmd_scan(event):
    oid = event.sender_id
    if not await user_client.is_user_authorized():
        return await event.respond("Аккаунт не подключён. Сначала /login.")
    if oid in busy:
        return await event.respond("Уже идёт операция, подождите.")
    busy.add(oid)
    try:
        target = event.pattern_match.group(1)
        target = int(target) if target.lstrip("-").isdigit() else target
        try:
            channel = await user_client.get_entity(target)
        except Exception as e:
            return await event.respond(f"Не нашёл канал: {e}")
        if not isinstance(channel, Channel):
            return await event.respond("Это не канал/супергруппа.")

        status = await event.respond("⏳ Загружаю подписчиков…")
        total = (await user_client.get_participants(channel, limit=0)).total
        wl = load_wl()
        me = await user_client.get_me()

        ids, stats, scanned = [], Counter(), 0
        async for u in user_client.iter_participants(channel, aggressive=total > 10000):
            scanned += 1
            if scanned % 1000 == 0:
                try:
                    await status.edit(f"⏳ Проверено {scanned}/{total}…")
                except Exception:
                    pass
            if u.id in wl or u.id == me.id or u.bot:
                continue
            if isinstance(getattr(u, "participant", None), (ChannelParticipantAdmin, ChannelParticipantCreator)):
                continue
            score, reasons = evaluate(u)
            if score >= settings["threshold"]:
                ids.append(u.id)
                stats.update(reasons)

        scans[oid] = {"channel": channel, "ids": ids, "stats": stats}
        lines = [f"📊 **{channel.title}**", f"Получено: {scanned} из {total}",
                 f"Подозрительных (порог {settings['threshold']}): **{len(ids)}**", ""]
        lines += [f"• {LABELS[k]}: {v}" for k, v in stats.most_common()]
        if scanned < total:
            lines.append("\n⚠️ Telegram отдал не всех — часть списка недоступна (лимит API).")
        buttons = [[Button.inline(f"🗑 Удалить {len(ids)}", b"ask")]] if ids else None
        await status.edit("\n".join(lines), buttons=buttons)
    finally:
        busy.discard(oid)


@bot.on(events.CallbackQuery)
async def on_cb(event):
    oid = event.sender_id
    if oid not in OWNERS:
        return await event.answer()
    sc = scans.get(oid)
    if not sc:
        return await event.answer("Сначала /scan", alert=True)

    if event.data == b"ask":
        mode = "бан навсегда" if settings["ban"] else "kick"
        await event.edit(
            f"Точно удалить **{len(sc['ids'])}** подписчиков из «{sc['channel'].title}»?\n"
            f"Режим: {mode}. Отменить нельзя.",
            buttons=[[Button.inline("✅ Да, удалить", b"go"), Button.inline("❌ Отмена", b"no")]],
        )
    elif event.data == b"no":
        await event.edit("Отменено.")
    elif event.data == b"go":
        if oid in busy:
            return await event.answer("Уже идёт операция", alert=True)
        busy.add(oid)
        try:
            await run_clean(event, sc)
        finally:
            busy.discard(oid)
            scans.pop(oid, None)


async def run_clean(event, sc):
    channel, ids = sc["channel"], sc["ids"]
    ok = fail = 0
    await event.edit(f"🧹 Удаляю… 0/{len(ids)}")
    for i, uid in enumerate(ids, 1):
        try:
            await user_client.edit_permissions(channel, uid, view_messages=False)
            if not settings["ban"]:
                await user_client.edit_permissions(channel, uid, view_messages=True)  # снять бан
            ok += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            fail += 1
        except Exception:
            fail += 1
        if i % 25 == 0:
            try:
                await event.edit(f"🧹 Удаляю… {i}/{len(ids)} (ок: {ok}, ошибок: {fail})")
            except Exception:
                pass
        await asyncio.sleep(KICK_DELAY)
    await event.edit(f"✅ Готово. Удалено: {ok}, ошибок: {fail}.")


async def main():
    await user_client.connect()  # вход — командой /login в боте
    await bot.start(bot_token=BOT_TOKEN)
    print("Channel Cleaner запущен")
    # user_client.run_until_disconnected() не используем: до /login он падает с AuthKeyUnregistered.
    # Аккаунту обновления не нужны — он остаётся подключён и выполняет запросы по требованию.
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
