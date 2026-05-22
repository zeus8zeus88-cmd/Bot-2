from typing import List
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import re
import discord
from discord import app_commands
# تم إضافة DEFAULT_ALLOWED_CHANNELS هنا لحل مشكلة الانهيار
from config import DEFAULT_SPECIALTIES, DEFAULT_ALLOWED_CHANNELS 
from database import collection, settings_collection, audit_collection, stats_collection

SETTINGS = {}
PRICES = {}

def is_admin(interaction: discord.Interaction) -> bool:
    """Check if the user has administrator permissions in the guild."""
    if not interaction.guild:
        return False
    return interaction.user.guild_permissions.administrator

def format_member_display(guild: discord.Guild, user_id: int, username_hint: str = None) -> str:
    """Return a nice display string for a user: @username if known, otherwise the ID."""
    member = guild.get_member(user_id)
    if member:
        return f"@{member.name}"
    if username_hint:
        return f"@{username_hint}"
    return str(user_id)

# ----------------------------------------------------------------------
# Works list helpers (stored inside the unified collection)
# ----------------------------------------------------------------------
async def load_works() -> list:
    """Load the list of approved works from the unified collection."""
    doc = await collection.find_one({"_id": "works"})
    if doc and "data" in doc:
        return doc["data"]
    return []

async def save_works(works: list):
    """Save the works list to the unified collection."""
    await collection.update_one(
        {"_id": "works"},
        {"$set": {"data": works}},
        upsert=True
    )

async def get_work(work_name: str) -> dict | None:
    """Find a work by name (case‑sensitive)."""
    works = await load_works()
    for w in works:
        if w["name"] == work_name:
            return w
    return None

def filter_paid_chapters(work: dict, chapters_list: List[str]):
    """Returns (paid_chapters, free_count) based on work's paid_start."""
    if work.get("paid_start") is None:
        return chapters_list, 0
    paid_start = work["paid_start"]
    paid = []
    free = 0
    for ch in chapters_list:
        try:
            ch_num = int(ch)
        except ValueError:
            paid.append(ch)
            continue
        if ch_num >= paid_start:
            paid.append(ch)
        else:
            free += 1
    return paid, free

async def delete_all_records_of_work(work_name: str) -> int:
    """Delete every record that belongs to a specific work (across all users)."""
    records = await load_records()
    removed_total = 0
    users_to_delete = []
    for user_id, entries in records.items():
        new_entries = [e for e in entries if e.get("work_name") != work_name]
        removed = len(entries) - len(new_entries)
        if removed > 0:
            removed_total += removed
            if new_entries:
                records[user_id] = new_entries
            else:
                users_to_delete.append(user_id)
    for uid in users_to_delete:
        del records[uid]
    if removed_total > 0:
        await save_records(records)
        await update_stats()
    return removed_total

# ----------------------------------------------------------------------
# Core helpers (unchanged logic)
# ----------------------------------------------------------------------
async def load_records():
    """Load records from MongoDB."""
    try:
        doc = await collection.find_one({"_id": "records"})
        if doc and "data" in doc:
            return doc["data"]
        return {}
    except Exception as e:
        print(f"[ERROR] load_records() - {e}")
        return {}

async def save_records(records):
    """Save records to MongoDB."""
    try:
        await collection.update_one(
            {"_id": "records"},
            {"$set": {"data": records}},
            upsert=True
        )
    except Exception as e:
        print(f"[ERROR] save_records() - {e}")

async def load_settings():
    """Load settings from MongoDB."""
    try:
        doc = await settings_collection.find_one({"_id": "settings"})
        if doc:
            if "specialties" not in doc:
                doc["specialties"] = DEFAULT_SPECIALTIES.copy()
            if "payment_day" not in doc:
                doc["payment_day"] = None
                doc["payment_hour"] = 0
                doc["payment_reminder_24h_sent"] = False
                doc["payment_day_sent"] = False
            return doc
        return {
            "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
            "currency": "$",
            "notify_channel_id": None,
            "daily_backup_channel_id": None,
            "alert_threshold": 10.0,
            "specialties": DEFAULT_SPECIALTIES.copy(),
            "payment_day": None,
            "payment_hour": 0,
            "payment_reminder_24h_sent": False,
            "payment_day_sent": False
        }
    except Exception as e:
        print(f"[ERROR] load_settings() - {e}")
        return {
            "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
            "currency": "$",
            "notify_channel_id": None,
            "daily_backup_channel_id": None,
            "alert_threshold": 10.0,
            "specialties": DEFAULT_SPECIALTIES.copy(),
            "payment_day": None,
            "payment_hour": 0,
            "payment_reminder_24h_sent": False,
            "payment_day_sent": False
        }

async def save_settings(settings):
    """Save settings to MongoDB."""
    try:
        await settings_collection.update_one(
            {"_id": "settings"},
            {"$set": settings},
            upsert=True
        )
    except Exception as e:
        print(f"[ERROR] save_settings() - {e}")

def rebuild_prices():
    """Rebuild global PRICES dict from active specialties in SETTINGS."""
    specialties = SETTINGS.get("specialties", DEFAULT_SPECIALTIES)
    PRICES.clear()
    PRICES.update({k: v["price"] for k, v in specialties.items() if v.get("active", True)})

async def log_audit(action, moderator_id, target_id, details):
    """Log an admin action."""
    log_entry = {
        "action": action,
        "moderator_id": str(moderator_id),
        "target_id": str(target_id) if target_id else None,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await audit_collection.insert_one(log_entry)

async def log_unauthorized(user_id, command_name):
    """Log an unauthorized attempt."""
    await log_audit("محاولة_غير_مصرح_بها", user_id, None,
                    f"محاولة استخدام الأمر {command_name} بدون صلاحية")

async def update_stats():
    """Update comprehensive stats."""
    records = await load_records()
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = 0
    type_counts = {}
    member_stats = {}

    for user_id, entries in records.items():
        member_total = 0
        member_counts = {}
        for entry in entries:
            amount = entry.get("total", 0)
            total_amount += amount
            member_total += amount
            wtype = entry.get("work_type")
            type_counts[wtype] = type_counts.get(wtype, 0) + 1
            member_counts[wtype] = member_counts.get(wtype, 0) + 1
        member_stats[user_id] = {
            "total_amount": member_total,
            "total_entries": len(entries),
            "type_counts": member_counts
        }

    top_members = sorted(member_stats.items(),
                         key=lambda x: x[1]["total_amount"], reverse=True)[:5]
    top_members_data = [(uid, stats) for uid, stats in top_members]

    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    daily_entries = 0
    daily_amount = 0
    weekly_entries = 0
    weekly_amount = 0
    monthly_entries = 0
    monthly_amount = 0

    for user_id, entries in records.items():
        for entry in entries:
            ts = entry.get("timestamp")
            if ts:
                try:
                    entry_date = datetime.fromisoformat(ts).date()
                    if entry_date == today:
                        daily_entries += 1
                        daily_amount += entry.get("total", 0)
                    if entry_date >= week_start:
                        weekly_entries += 1
                        weekly_amount += entry.get("total", 0)
                    if entry_date >= month_start:
                        monthly_entries += 1
                        monthly_amount += entry.get("total", 0)
                except:
                    pass

    stat_doc = {
        "total_entries": total_entries,
        "total_amount": total_amount,
        "type_counts": type_counts,
        "member_stats": member_stats,
        "top_members": top_members_data,
        "daily": {"entries": daily_entries, "amount": daily_amount},
        "weekly": {"entries": weekly_entries, "amount": weekly_amount},
        "monthly": {"entries": monthly_entries, "amount": monthly_amount},
        "last_updated": datetime.now(timezone.utc).isoformat()
    }
    await stats_collection.update_one(
        {"_id": "stats"},
        {"$set": stat_doc},
        upsert=True
    )

def parse_fields(text):
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        fields[key] = value
    return fields

def parse_chapter_range(range_str):
    range_str = range_str.strip()
    chapters = []
    if '-' in range_str:
        parts = range_str.split('-')
        if len(parts) == 2:
            try:
                start = int(parts[0])
                end = int(parts[1])
                for i in range(start, end+1):
                    chapters.append(str(i))
            except:
                pass
    elif ',' in range_str:
        for part in range_str.split(','):
            part = part.strip()
            if part.isdigit():
                chapters.append(part)
    else:
        if range_str.isdigit():
            chapters.append(range_str)
    return chapters

def parse_mixed_types(types_input, chapters_count):
    types_input = types_input.strip()
    if '-' in types_input:
        parts = types_input.split('-')
        if len(parts) == chapters_count:
            return [p.strip() for p in parts]
        elif len(parts) == 2:
            first = parts[0].strip()
            rest = parts[1].strip()
            return [first] + [rest] * (chapters_count - 1)
        else:
            if ',' in types_input:
                return parse_mixed_types(types_input.replace('-', ','), chapters_count)
            return None
    elif ',' in types_input:
        parts = [p.strip() for p in types_input.split(',')]
        if len(parts) == chapters_count:
            return parts
        elif len(parts) == 1:
            return [parts[0]] * chapters_count
        else:
            return None
    else:
        return [types_input] * chapters_count

def map_type(t):
    """Convert user-friendly type (with spaces) to internal key (underscores)."""
    return t.strip().replace(' ', '_')

def is_duplicate(records, user_id, work_name, chapter, work_type):
    """Check if the same (work, chapter, type) already exists for this user."""
    user_entries = records.get(str(user_id), [])
    for e in user_entries:
        if (e.get("work_name") == work_name and 
            e.get("chapter") == chapter and 
            e.get("work_type") == work_type):
            return True
    return False

# ----------------------------------------------------------------------
# Unified UI helpers
# ----------------------------------------------------------------------
EMBED_COLORS = {
    "success": discord.Color.green(),
    "danger": discord.Color.red(),
    "warning": discord.Color.orange(),
    "info": discord.Color.blue(),
    "admin": discord.Color.purple(),
    "finance": discord.Color.gold(),
    "muted": discord.Color.light_grey(),
}

def make_embed(kind: str, title: str, description: str = "", interaction: discord.Interaction | None = None, member: discord.Member | None = None):
    emb = discord.Embed(title=title, description=description, color=EMBED_COLORS.get(kind, discord.Color.blurple()))
    if member:
        emb.set_thumbnail(url=member.display_avatar.url)
    footer = "Work Bot • واجهة موحدة"
    if interaction:
        footer += f" • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    emb.set_footer(text=footer)
    return emb

class ConfirmActionView(discord.ui.View):
    def __init__(self, on_confirm, on_preview=None, timeout=60):
        super().__init__(timeout=timeout)
        self._on_confirm = on_confirm
        self._on_preview = on_preview

    @discord.ui.button(label="🗑️ تأكيد الحذف", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._on_confirm(interaction)

    @discord.ui.button(label="ℹ️ عرض التفاصيل", style=discord.ButtonStyle.primary)
    async def preview_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._on_preview:
            await self._on_preview(interaction)
        else:
            await interaction.response.send_message("لا توجد تفاصيل إضافية.", ephemeral=True)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="تم إلغاء العملية.", view=None)
