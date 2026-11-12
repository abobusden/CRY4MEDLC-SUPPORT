import os

# ============================================
#  НАСТРОЙКИ БОТА
#  На Render задаются в Environment:
#    BOT_TOKEN  = токен от @BotFather
#    OWNER_IDS  = 8592927529,6236795210
# ============================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")

_owners_raw = os.environ.get("OWNER_IDS", "8592927529,6236795210")
OWNER_IDS = [int(x.strip()) for x in _owners_raw.split(",") if x.strip().isdigit()]

# Автоудаление закрытых тикетов (в днях)
TICKET_TTL_DAYS = 30

# Названия файлов данных
MODS_FILE = "mods.json"
USERS_FILE = "users.json"
TICKETS_FILE = "tickets.json"
PUNISH_FILE = "punishments.json"
STATS_FILE = "stats.json"
