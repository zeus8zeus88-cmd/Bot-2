import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
import re
from collections import defaultdict
from typing import List, Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import motor.motor_asyncio
import pandas as pd

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    raise ValueError("DISCORD_TOKEN is missing from .env file")

MONGODB_URI = os.getenv("MONGODB_URI")
if MONGODB_URI is None:
    raise ValueError("MONGODB_URI is missing from .env file")

# Default allowed channels (will be loaded from DB)
DEFAULT_ALLOWED_CHANNELS = ["تسجيــــــــل-اعمال〢💵"]

# Prices in USD (supports currency change later)
PRICES = {
    "تحرير": 0.50,
    "ترجمة_كوري": 0.75,
    "ترجمة_انجليزي": 0.60,
    "تبييض": 0.25,
}

# Currency symbol (can be changed by admin)
CURRENCY = "$"

# MongoDB setup
print("[LOG] Creating MongoDB client...")
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["work_bot"]
collection = db["records"]          # unified collection (records + works)
settings_collection = db["settings"]
audit_collection = db["audit_log"]
stats_collection = db["stats"]

# ----------------------------------------------------------------------
# Helper: display a member as @username (or fallback to ID)
# ----------------------------------------------------------------------
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
            return doc
        return {
            "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
            "currency": "$",
            "notify_channel_id": None,
            "daily_backup_channel_id": None,
            "alert_threshold": 10.0
        }
    except Exception as e:
        print(f"[ERROR] load_settings() - {e}")
        return {
            "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
            "currency": "$",
            "notify_channel_id": None,
            "daily_backup_channel_id": None,
            "alert_threshold": 10.0
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

async def log_audit(action, moderator_id, target_id, details):
    """Log an admin action."""
    log_entry = {
        "action": action,
        "moderator_id": str(moderator_id),
        "target_id": str(target_id) if target_id else None,
        "details": details,
        "timestamp": datetime.utcnow().isoformat()
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

    today = datetime.utcnow().date()
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
        "last_updated": datetime.utcnow().isoformat()
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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")  # disable default help

SETTINGS = {}

# ----------------------------------------------------------------------
# Events & checks
# ----------------------------------------------------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ ما عندك صلاحية تستخدم هذا الأمر.")
        return
    await ctx.send(f"⚠️ صار خطأ: `{error}`")

@bot.event
async def on_ready():
    global SETTINGS
    print(f"[LOG] Logged in as {bot.user}")
    SETTINGS = await load_settings()
    print(f"[LOG] Settings loaded: allowed_channels={SETTINGS.get('allowed_channels')}, "
          f"currency={SETTINGS.get('currency')}")
    try:
        await mongo_client.admin.command('ping')
        print("[LOG] MongoDB connection successful!")
    except Exception as e:
        print(f"[ERROR] MongoDB connection failed: {e}")
    await bot.tree.sync()
    print("[LOG] Slash commands synced")
    await update_stats()
    daily_backup.start()
    update_stats_task.start()

@bot.check
async def only_allowed_channel(ctx):
    if ctx.author.bot:
        return False
    if ctx.channel.name in SETTINGS.get("allowed_channels", []):
        return True
    channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
    await ctx.send(f"❌ استخدم أوامر البوت فقط في أحد الرومات: {channels_str}.")
    return False

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_messages

# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------
@tasks.loop(hours=24)
async def daily_backup():
    backup_channel_id = SETTINGS.get("daily_backup_channel_id")
    if not backup_channel_id:
        return
    channel = bot.get_channel(backup_channel_id)
    if not channel:
        return
    records = await load_records()
    data = json.dumps(records, ensure_ascii=False, indent=2)
    file = discord.File(BytesIO(data.encode('utf-8')), filename=f"backup_{datetime.utcnow().date()}.json")
    await channel.send(f"📦 نسخة احتياطية يومية - {datetime.utcnow().date()}", file=file)

@tasks.loop(hours=1)
async def update_stats_task():
    await update_stats()

# ----------------------------------------------------------------------
# Autocomplete helper (must be defined before any command that uses it)
# ----------------------------------------------------------------------
async def work_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    works = await load_works()
    choices = []
    for w in works:
        if current.lower() in w["name"].lower():
            choices.append(app_commands.Choice(name=w["name"][:100], value=w["name"]))
    return choices[:25]

# ----------------------------------------------------------------------
# Command: تحديد_قنوات
# ----------------------------------------------------------------------
@bot.tree.command(name="تحديد_قنوات", description="تحديد القنوات المسموحة (قناتين كحد أقصى) - للإدارة فقط")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def set_allowed_channels_slash(interaction: discord.Interaction,
                                     channel1: discord.TextChannel,
                                     channel2: discord.TextChannel = None):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تحديد_قنوات")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    channels = [channel1.name]
    if channel2:
        channels.append(channel2.name)
    channels = list(dict.fromkeys(channels))[:2]
    SETTINGS["allowed_channels"] = channels
    await save_settings(SETTINGS)
    channels_str = ", ".join([f"#{ch}" for ch in SETTINGS["allowed_channels"]])
    await interaction.response.send_message(f"✅ تم تحديث القنوات المسموحة إلى: {channels_str}", ephemeral=True)
    await log_audit("تحديد_قنوات", interaction.user.id, None, f"القنوات الجديدة: {channels_str}")

@bot.command(name="تحديد_قنوات")
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def set_allowed_channels_text(ctx, channel1: str, channel2: str = None):
    def extract_channel_name(input_str):
        if input_str.startswith('<#') and input_str.endswith('>'):
            channel_id = int(input_str[2:-1])
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                return channel.name
        elif input_str.isdigit():
            channel = ctx.guild.get_channel(int(input_str))
            if channel:
                return channel.name
        else:
            for ch in ctx.guild.channels:
                if ch.name == input_str:
                    return ch.name
        return input_str
    ch1_name = extract_channel_name(channel1)
    ch2_name = extract_channel_name(channel2) if channel2 else None
    channels = [ch1_name]
    if ch2_name:
        channels.append(ch2_name)
    channels = list(dict.fromkeys(channels))[:2]
    SETTINGS["allowed_channels"] = channels
    await save_settings(SETTINGS)
    channels_str = ", ".join([f"#{ch}" for ch in SETTINGS["allowed_channels"]])
    await ctx.send(f"✅ تم تحديث القنوات المسموحة إلى: {channels_str}")
    await log_audit("تحديد_قنوات", ctx.author.id, None, f"القنوات الجديدة: {channels_str}")

# ----------------------------------------------------------------------
# Command: رفع_البيانات (now also handles works)
# ----------------------------------------------------------------------
@bot.tree.command(name="رفع_البيانات", description="رفع ملف JSON لاستعادة السجلات والأعمال إلى MongoDB")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.user.id, i.command.qualified_name))
async def upload_records(interaction: discord.Interaction, file: discord.Attachment):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "رفع_البيانات")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not file.filename.endswith('.json'):
        await interaction.followup.send("❌ الملف يجب أن يكون بصيغة JSON.", ephemeral=True)
        return
    try:
        content = await file.read()
        data = json.loads(content.decode('utf-8'))

        # Support both old format (plain records dict) and new format with "records" and optional "works"
        if isinstance(data, dict):
            records_data = data.get("records", data)  # fallback to whole dict
            works_data = data.get("works", None)
        else:
            await interaction.followup.send("❌ الملف غير صالح.", ephemeral=True)
            return

        # Update records
        if not isinstance(records_data, dict):
            await interaction.followup.send("❌ قسم records غير صالح.", ephemeral=True)
            return
        await collection.update_one({"_id": "records"}, {"$set": {"data": records_data}}, upsert=True)
        total_users = len(records_data)
        total_entries = sum(len(entries) for entries in records_data.values() if isinstance(entries, list))

        # --- NEW: Auto-extract works from records ---
        works_from_records = set()
        for user_entries in records_data.values():
            if isinstance(user_entries, list):
                for entry in user_entries:
                    if isinstance(entry, dict) and "work_name" in entry:
                        works_from_records.add(entry["work_name"])

        if works_from_records:
            current_works = await load_works()
            existing_names = {w["name"] for w in current_works}
            added_works_count = 0
            for name in works_from_records:
                if name not in existing_names:
                    current_works.append({"name": name, "paid_start": None, "active": True})
                    existing_names.add(name)
                    added_works_count += 1
            if added_works_count > 0:
                await save_works(current_works)
        else:
            added_works_count = 0
        # --- End of auto-extract ---

        # Update works from file if present (overwrites if the user provided a "works" section)
        if works_data is not None:
            if isinstance(works_data, list):
                await save_works(works_data)
                added_works_count = len(works_data)  # override count with explicit works data
            else:
                await interaction.followup.send("⚠️ تم تحديث السجلات لكن قسم works غير صالح (تم تجاهله).", ephemeral=True)
                await log_audit("رفع_البيانات", interaction.user.id, None,
                                f"تم رفع {total_entries} سجل (works غير محدثة)")
                await update_stats()
                await interaction.followup.send(
                    f"✅ تم استعادة السجلات بنجاح!\nعدد المستخدمين: {total_users}\nإجمالي السجلات: {total_entries}",
                    ephemeral=True)
                return

        await log_audit("رفع_البيانات", interaction.user.id, None,
                        f"تم رفع {total_entries} سجل" + (f" و {added_works_count} عمل جديد" if added_works_count else ""))
        await update_stats()
        msg = f"✅ تم استعادة البيانات بنجاح!\nعدد المستخدمين: {total_users}\nإجمالي السجلات: {total_entries}"
        if added_works_count:
            msg += f"\nأعمال جديدة مضافة من السجلات: {added_works_count}"
        if works_data is not None:
            msg += f"\nالأعمال المحدثة من الملف: {len(works_data)}"
        await interaction.followup.send(msg, ephemeral=True)
    except json.JSONDecodeError:
        await interaction.followup.send("❌ الملف ليس بصيغة JSON صحيحة.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Slash command restore failed: {e}")
        await interaction.followup.send(f"❌ حدث خطأ: {str(e)}", ephemeral=True)

# ----------------------------------------------------------------------
# Command: اوامر (help)
# ----------------------------------------------------------------------
@bot.tree.command(name="اوامر", description="عرض قائمة بجميع أوامر البوت")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📌 **أوامر البوت**", color=discord.Color.purple())
    embed.add_field(name="**▸ تسجيل شغل جديد**",
                    value="`!تحليل` أو `/تسجيل`\n*يدعم فصلاً واحداً أو عدة فصول، وأنماط أنواع مثل `ترجمة كوري-تحرير`*",
                    inline=False)
    embed.add_field(name="**▸ عرض أعمالي**", value="`!أعمالي` أو `/أعمالي`", inline=False)
    embed.add_field(name="**▸ عرض شغل عضو**", value="`!شغل @member` أو `/شغل`", inline=False)
    embed.add_field(name="**▸ عرض الأسعار**", value="`!اسعار` أو `/اسعار`", inline=False)
    embed.add_field(name="**▸ تعديل السعر (للمشرفين)**", value="`/تعديل_سعر`", inline=False)
    embed.add_field(name="**▸ حذف (للمشرفين)**",
                    value="`/حذف` (يدعم حذف كل السجلات، أو عمل كامل، أو فصل محدد)", inline=False)
    embed.add_field(name="**▸ حذف كل السجلات (للمشرفين)**", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="**▸ تسجيل شغل لعضو (للمشرفين)**", value="`/تسجيل_للغير`", inline=False)
    embed.add_field(name="**▸ حذف كل الأعمال (للمشرفين)**", value="`/حذف_كل_الأعمال`", inline=False)
    embed.add_field(name="**▸ تحديد القنوات (للمشرفين)**", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="**▸ لوحة التحكم (للمشرفين)**", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="**▸ الإحصائيات**", value="`/احصائيات`", inline=False)
    embed.add_field(name="**▸ سجل العمليات (للمشرفين)**", value="`/سجل`", inline=False)
    embed.add_field(name="**▸ تقريري الأسبوعي**", value="`/تقريري`", inline=False)
    embed.add_field(name="**▸ تعديل آخر سجل**", value="`/تعديل`", inline=False)
    embed.add_field(name="**▸ تصدير Excel (للمشرفين)**", value="`/تصدير`", inline=False)
    embed.add_field(name="**▸ إعدادات العملة والإشعارات (للمشرفين)**", value="`/اعدادات`", inline=False)
    embed.add_field(name="**▸ قائمة الأعمال**", value="`/الأعمال`", inline=False)
    embed.add_field(name="**▸ إدارة الأعمال (للمشرفين)**",
                    value="`/اضافة_عمل` `/حذف_عمل` `/تعديل_عمل` `/عرض_الاعمال`", inline=False)
    embed.add_field(name="**▸ مكافأة وخصم (للمشرفين)**",
                    value="`/مكافأة` و `/خصم`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await interaction.response.send_message(embed=embed)

@bot.command(name="اوامر")
@commands.cooldown(1, 5, commands.BucketType.user)
async def help_commands(ctx):
    embed = discord.Embed(title="📌 **أوامر البوت**", color=discord.Color.purple())
    embed.add_field(name="**▸ تسجيل شغل جديد**",
                    value="`!تحليل` أو `/تسجيل`\n*يدعم فصلاً واحداً أو عدة فصول، وأنماط أنواع مثل `ترجمة كوري-تحرير`*",
                    inline=False)
    embed.add_field(name="**▸ عرض أعمالي**", value="`!أعمالي` أو `/أعمالي`", inline=False)
    embed.add_field(name="**▸ عرض شغل عضو**", value="`!شغل @member` أو `/شغل`", inline=False)
    embed.add_field(name="**▸ عرض الأسعار**", value="`!اسعار` أو `/اسعار`", inline=False)
    embed.add_field(name="**▸ تعديل السعر (للمشرفين)**", value="`/تعديل_سعر`", inline=False)
    embed.add_field(name="**▸ حذف (للمشرفين)**", value="`/حذف` (خيارات متعددة)", inline=False)
    embed.add_field(name="**▸ حذف كل السجلات (للمشرفين)**", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="**▸ تسجيل شغل لعضو (للمشرفين)**", value="`/تسجيل_للغير`", inline=False)
    embed.add_field(name="**▸ حذف كل الأعمال (للمشرفين)**", value="`/حذف_كل_الأعمال`", inline=False)
    embed.add_field(name="**▸ تحديد القنوات (للمشرفين)**", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="**▸ لوحة التحكم (للمشرفين)**", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="**▸ الإحصائيات**", value="`/احصائيات`", inline=False)
    embed.add_field(name="**▸ سجل العمليات (للمشرفين)**", value="`/سجل`", inline=False)
    embed.add_field(name="**▸ تقريري الأسبوعي**", value="`/تقريري`", inline=False)
    embed.add_field(name="**▸ تعديل آخر سجل**", value="`/تعديل`", inline=False)
    embed.add_field(name="**▸ تصدير Excel (للمشرفين)**", value="`/تصدير`", inline=False)
    embed.add_field(name="**▸ إعدادات العملة والإشعارات (للمشرفين)**", value="`/اعدادات`", inline=False)
    embed.add_field(name="**▸ قائمة الأعمال**", value="`/الأعمال`", inline=False)
    embed.add_field(name="**▸ إدارة الأعمال (للمشرفين)**",
                    value="`/اضافة_عمل` `/حذف_عمل` `/تعديل_عمل` `/عرض_الاعمال`", inline=False)
    embed.add_field(name="**▸ مكافأة وخصم (للمشرفين)**",
                    value="`/مكافأة` و `/خصم`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await ctx.send(embed=embed)

# ----------------------------------------------------------------------
# Command: اسعار
# ----------------------------------------------------------------------
@bot.tree.command(name="اسعار", description="عرض أسعار أنواع العمل الحالية")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def prices_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="💰 **قائمة الأسعار**", color=discord.Color.gold())
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"**{display_name}**", value=f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.command(name="اسعار")
@commands.cooldown(1, 5, commands.BucketType.user)
async def prices_text(ctx):
    embed = discord.Embed(title="💰 **قائمة الأسعار**", color=discord.Color.gold())
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"**{display_name}**", value=f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await ctx.send(embed=embed)

# ----------------------------------------------------------------------
# Command: تعديل_سعر
# ----------------------------------------------------------------------
@bot.tree.command(name="تعديل_سعر", description="تعديل سعر نوع عمل (للمشرفين)")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def edit_price_slash(interaction: discord.Interaction, النوع: str, السعر: float):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تعديل_سعر")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    # Normalize: replace spaces with underscores to match keys
    norm_type = النوع.strip().replace(' ', '_')
    matched = None
    for key in PRICES.keys():
        if key.replace('_', ' ').lower() == norm_type.replace('_', ' ').lower() or key.lower() == norm_type.lower():
            matched = key
            break
    if matched is None:
        await interaction.response.send_message(f"❌ النوع `{النوع}` غير موجود. الأنواع المتاحة: {', '.join(PRICES.keys())}", ephemeral=True)
        return
    PRICES[matched] = السعر
    settings = await load_settings()
    settings["prices"] = PRICES
    await save_settings(settings)
    await log_audit("تعديل_سعر", interaction.user.id, None, f"تغيير سعر {matched} إلى {السعر}")
    await interaction.response.send_message(f"✅ تم تحديث سعر `{matched}` إلى {SETTINGS.get('currency', '$')}{السعر:.2f}", ephemeral=True)

# ----------------------------------------------------------------------
# Slash command: تسجيل (now without modal, using autocomplete)
# ----------------------------------------------------------------------
@bot.tree.command(name="تسجيل", description="تسجيل شغل جديد (يدعم الفلترة حسب الأعمال المدفوعة)")
@app_commands.autocomplete(العمل=work_autocomplete)
@app_commands.describe(
    العمل="اسم العمل (اختر من القائمة)",
    الفصول="نطاق الفصول مثل 1-5 أو 1,3,5",
    الانواع="الأنواع مثل ترجمة كوري-تحرير-تبييض (بدون شرطة سفلية)",
    ملاحظات="ملاحظات اختيارية"
)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def register_slash(interaction: discord.Interaction, العمل: str, الفصول: str, الانواع: str, ملاحظات: str = None):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return

    # التحقق من وجود العمل ونشاطه
    work = await get_work(العمل)
    if not work:
        await interaction.response.send_message(f"❌ العمل `{العمل}` غير موجود في قائمة الأعمال المدفوعة. تواصل مع الإدارة.", ephemeral=True)
        return
    if not work.get("active", True):
        await interaction.response.send_message(f"❌ العمل `{العمل}` معطل حالياً.", ephemeral=True)
        return

    chapters_list = parse_chapter_range(الفصول)
    if not chapters_list:
        await interaction.response.send_message("❌ نطاق الفصول غير صالح.", ephemeral=True)
        return

    paid_chapters, free_count = filter_paid_chapters(work, chapters_list)
    if not paid_chapters:
        await interaction.response.send_message("⚠️ جميع الفصول المدخلة مجانية ولم تُسجّل.", ephemeral=True)
        return

    # تحليل الأنواع مع إمكانية استخدام المسافات بدلاً من الشَرطات السفلية
    original_types = parse_mixed_types(الانواع, len(chapters_list))
    if original_types is None:
        await interaction.response.send_message(f"❌ عدد الأنواع لا يتطابق مع عدد الفصول ({len(chapters_list)}).", ephemeral=True)
        return

    # تحويل المسافات إلى شرطات سفلية لتطابق القاموس
    def map_type(t):
        return t.strip().replace(' ', '_')
    mapped_types = [map_type(t) for t in original_types]

    # فلترة الأنواع للفصول المدفوعة
    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    # التحقق من صحة الأنواع
    for t in filtered_types:
        if t not in PRICES:
            await interaction.response.send_message(f"❌ النوع `{t}` غير صحيح. الأنواع المتاحة: {', '.join(PRICES.keys())}", ephemeral=True)
            return

    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = interaction.user.name
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        total = PRICES[work_type]
        records[user_id].append({
            "work_name": العمل,
            "chapter": ch,
            "work_type": work_type,
            "total": total,
            "notes": ملاحظات or "",
            "timestamp": datetime.utcnow().isoformat(),
            "username": username
        })
        added += 1

    await save_records(records)
    await update_stats()

    embed = discord.Embed(title="✅ **تم حفظ الشغل بنجاح**", color=discord.Color.green())
    embed.add_field(name="**📖 العمل**", value=العمل, inline=True)
    embed.add_field(name="**🔢 عدد الفصول المدفوعة المسجلة**", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية لم تُسجّل", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ النوع**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل الأنواع**", value=types_summary, inline=False)
    embed.add_field(name="**💰 المبلغ الإجمالي**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=False)
    if ملاحظات:
        embed.add_field(name="**📝 ملاحظات**", value=ملاحظات, inline=False)

    await interaction.response.send_message(embed=embed)

    notify_channel_id = SETTINGS.get("notify_channel_id")
    if notify_channel_id:
        channel = interaction.guild.get_channel(notify_channel_id)
        if channel:
            await channel.send(f"📢 {interaction.user.mention} أضاف {added} فصول مدفوعة في عمل `{العمل}`")

    total_user_amount = sum(item.get("total", 0) for item in records[user_id])
    threshold = SETTINGS.get("alert_threshold", 10.0)
    if total_user_amount >= threshold:
        try:
            await interaction.user.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}.")
        except:
            pass

# ----------------------------------------------------------------------
# NEW: /تسجيل_للغير (Admin registers for a member)
# ----------------------------------------------------------------------
@bot.tree.command(name="تسجيل_للغير", description="تسجيل شغل لعضو معين (للمشرفين فقط)")
@app_commands.autocomplete(العمل=work_autocomplete)
@app_commands.describe(
    عضو="العضو الذي تريد تسجيل الشغل له",
    العمل="اسم العمل (يجب أن يكون موجوداً في القائمة)",
    الفصول="نطاق الفصول مثل 1-5 أو 1,3,5",
    الانواع="الأنواع مثل ترجمة كوري-تحرير-تبييض",
    ملاحظات="ملاحظات اختيارية"
)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def register_for_member(
    interaction: discord.Interaction,
    عضو: discord.Member,
    العمل: str,
    الفصول: str,
    الانواع: str,
    ملاحظات: str = None
):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تسجيل_للغير")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return

    # التحقق من وجود العمل ونشاطه
    work = await get_work(العمل)
    if not work:
        await interaction.response.send_message(f"❌ العمل `{العمل}` غير موجود في قائمة الأعمال المدفوعة.", ephemeral=True)
        return
    if not work.get("active", True):
        await interaction.response.send_message(f"❌ العمل `{العمل}` معطل حالياً ولا يمكن إضافة فصول إليه.", ephemeral=True)
        return

    # تحليل الفصول
    chapters_list = parse_chapter_range(الفصول)
    if not chapters_list:
        await interaction.response.send_message("❌ نطاق الفصول غير صالح. استخدم مثلاً `5` أو `1-5` أو `1,3,5`.", ephemeral=True)
        return

    # فلترة الفصول المدفوعة
    paid_chapters, free_count = filter_paid_chapters(work, chapters_list)
    if not paid_chapters:
        await interaction.response.send_message("⚠️ جميع الفصول المدخلة مجانية ولم تُسجّل.", ephemeral=True)
        return

    # تحليل الأنواع
    types_list = parse_mixed_types(الانواع, len(chapters_list))
    if types_list is None:
        await interaction.response.send_message(f"❌ عدد الأنواع لا يتطابق مع عدد الفصول ({len(chapters_list)}).", ephemeral=True)
        return

    # تعيين الأنواع إلى المفاتيح الفعلية
    def map_type(t):
        return t.strip().replace(' ', '_')
    mapped_types = [map_type(t) for t in types_list]

    # مطابقة الأنواع للفصول المدفوعة فقط
    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    # التحقق من صحة الأنواع
    for t in filtered_types:
        if t not in PRICES:
            await interaction.response.send_message(f"❌ النوع `{t}` غير صحيح. الأنواع المسموحة: {', '.join(PRICES.keys())}", ephemeral=True)
            return

    # جلب السجلات وحفظها
    records = await load_records()
    user_id = str(عضو.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = عضو.name  # حفظ اسم العضو
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        total = PRICES[work_type]
        records[user_id].append({
            "work_name": العمل,
            "chapter": ch,
            "work_type": work_type,
            "total": total,
            "notes": ملاحظات or "",
            "timestamp": datetime.utcnow().isoformat(),
            "username": username,
            "added_by": str(interaction.user.id)  # من قام بالإضافة
        })
        added += 1

    await save_records(records)
    await update_stats()

    # بناء التضمين
    embed = discord.Embed(title="✅ **تم حفظ الشغل بنجاح**", color=discord.Color.green())
    embed.add_field(name="**👤 العضو**", value=عضو.mention, inline=True)
    embed.add_field(name="**📖 العمل**", value=العمل, inline=True)
    embed.add_field(name="**🔢 عدد الفصول المدفوعة المسجلة**", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية لم تُسجّل", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ النوع**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل الأنواع**", value=types_summary, inline=False)
    embed.add_field(name="**💰 المبلغ الإجمالي**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=False)
    embed.add_field(name="**🛡️ أضيف بواسطة**", value=interaction.user.mention, inline=True)
    if ملاحظات:
        embed.add_field(name="**📝 ملاحظات**", value=ملاحظات, inline=False)

    await interaction.response.send_message(embed=embed)

    # إشعار قناة الإشعارات
    notify_channel_id = SETTINGS.get("notify_channel_id")
    if notify_channel_id:
        channel = interaction.guild.get_channel(notify_channel_id)
        if channel:
            await channel.send(f"📢 {interaction.user.mention} أضاف {added} فصول مدفوعة للعضو {عضو.mention} في عمل `{العمل}`")

    # سجل التدقيق
    await log_audit("تسجيل_للغير", interaction.user.id, عضو.id,
                    f"أضاف {added} فصل لـ {العمل} (الأنواع: {','.join(filtered_types)})")

    # إشعار خاص للعضو (اختياري)
    try:
        await عضو.send(f"📬 تم تسجيل {added} فصول مدفوعة لك في عمل `{العمل}` بواسطة {interaction.user.mention}.")
    except:
        pass

# ----------------------------------------------------------------------
# Text command: تحليل
# ----------------------------------------------------------------------
@bot.command(name="تحليل")
@commands.cooldown(1, 5, commands.BucketType.user)
async def analysis(ctx, *, text=None):
    if not text:
        await ctx.send(
            "**📝 الصيغة:**\n"
            "```text\n"
            "!تحليل\n"
            "العمل: اسم العمل\n"
            "الفصل: رقم الفصل  أو  نطاق الفصول (مثل 1-5)\n"
            "النوع: نوع واحد  أو  أنواع مفصولة بـ - (مثل ترجمة كوري-تحرير)\n"
            "ملاحظات: اختياري\n"
            "```\n"
            "**الأنواع المتاحة:** " + "، ".join(PRICES.keys())
        )
        return

    fields = parse_fields(text)
    work_name = fields.get("العمل") or fields.get("اسم العمل")
    chapter_str = fields.get("الفصل") or fields.get("رقم الفصل")
    types_str = fields.get("النوع") or fields.get("الشغل")
    notes = fields.get("ملاحظات", "")

    if not work_name or not chapter_str or not types_str:
        await ctx.send("❌ فيه بيانات ناقصة. لازم تكتب: `العمل`، `الفصل`، `النوع`")
        return

    work = await get_work(work_name)
    if not work:
        await ctx.send(f"❌ العمل `{work_name}` غير موجود في قائمة الأعمال المدفوعة.")
        return
    if not work.get("active", True):
        await ctx.send(f"❌ العمل `{work_name}` معطل حالياً.")
        return

    chapters_list = parse_chapter_range(chapter_str)
    if not chapters_list:
        await ctx.send("❌ نطاق الفصول غير صالح.")
        return

    paid_chapters, free_count = filter_paid_chapters(work, chapters_list)
    if not paid_chapters:
        await ctx.send("⚠️ جميع الفصول المدخلة مجانية ولم تُسجّل.")
        return

    original_types = parse_mixed_types(types_str, len(chapters_list))
    if original_types is None:
        await ctx.send(f"❌ عدد الأنواع لا يتطابق مع عدد الفصول ({len(chapters_list)}).")
        return

    # تحويل المسافات إلى شرطات سفلية
    def map_type(t):
        return t.strip().replace(' ', '_')
    mapped_types = [map_type(t) for t in original_types]

    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    for t in filtered_types:
        if t not in PRICES:
            await ctx.send(f"❌ النوع `{t}` غير صحيح. الأنواع: {', '.join(PRICES.keys())}")
            return

    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = ctx.author.name
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        total = PRICES[work_type]
        records[user_id].append({
            "work_name": work_name,
            "chapter": ch,
            "work_type": work_type,
            "total": total,
            "notes": notes,
            "timestamp": datetime.utcnow().isoformat(),
            "username": username
        })
        added += 1

    await save_records(records)
    await update_stats()

    embed = discord.Embed(title="✅ **تم حفظ الشغل بنجاح**", color=discord.Color.green())
    embed.add_field(name="**📖 العمل**", value=work_name, inline=True)
    embed.add_field(name="**🔢 عدد الفصول المدفوعة المسجلة**", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية لم تُسجّل", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ النوع**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل الأنواع**", value=types_summary, inline=False)
    embed.add_field(name="**💰 المبلغ الإجمالي**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=False)
    if notes:
        embed.add_field(name="**📝 ملاحظات**", value=notes, inline=False)

    await ctx.send(embed=embed)

    notify_channel_id = SETTINGS.get("notify_channel_id")
    if notify_channel_id:
        channel = ctx.guild.get_channel(notify_channel_id)
        if channel:
            await channel.send(f"📢 {ctx.author.mention} أضاف {added} فصول مدفوعة في عمل `{work_name}`")

    total_user_amount = sum(item.get("total", 0) for item in records[user_id])
    threshold = SETTINGS.get("alert_threshold", 10.0)
    if total_user_amount >= threshold:
        try:
            await ctx.author.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}.")
        except:
            pass

# ----------------------------------------------------------------------
# Delete commands
# ----------------------------------------------------------------------
class DeleteSelect(discord.ui.Select):
    def __init__(self, user_id, work_name=None):
        self.user_id = user_id
        self.work_name = work_name
        options = []
        if work_name:
            options.append(discord.SelectOption(label="🗑️ حذف كل فصول هذا العمل", value="delete_work", description=f"حذف كل فصول عمل {work_name}"))
            options.append(discord.SelectOption(label="🔍 حذف فصل محدد", value="delete_chapter", description="اختيار فصل لحذفه"))
        else:
            options.append(discord.SelectOption(label="👤 حذف كل سجلات العضو", value="delete_all_user", description="حذف كل سجلات العضو بالكامل"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        super().__init__(placeholder="اختر إجراء...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "cancel":
            await interaction.response.edit_message(content="تم الإلغاء.", view=None)
            return
        if self.values[0] == "delete_all_user":
            await interaction.response.send_message("⚠️ **تحذير:** هل أنت متأكد من حذف كل سجلات هذا العضو؟\nأرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
            def check(m):
                return m.author == interaction.user and m.content == "تأكيد" and m.channel == interaction.channel
            try:
                await bot.wait_for('message', timeout=30.0, check=check)
            except:
                await interaction.followup.send("❌ تم إلغاء العملية.", ephemeral=True)
                return
            records = await load_records()
            if str(self.user_id) in records:
                del records[str(self.user_id)]
                await save_records(records)
                await log_audit("حذف_كل_سجلات_العضو", interaction.user.id, self.user_id, "حذف كل السجلات")
                await update_stats()
                await interaction.followup.send(f"✅ تم حذف كل سجلات العضو.", ephemeral=True)
            else:
                await interaction.followup.send("❌ لا توجد سجلات لهذا العضو.", ephemeral=True)
        elif self.values[0] == "delete_work" and self.work_name:
            await interaction.response.send_message(f"⚠️ **تحذير:** هل أنت متأكد من حذف كل فصول عمل `{self.work_name}` للعضو؟\nأرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
            def check(m):
                return m.author == interaction.user and m.content == "تأكيد" and m.channel == interaction.channel
            try:
                await bot.wait_for('message', timeout=30.0, check=check)
            except:
                await interaction.followup.send("❌ تم إلغاء العملية.", ephemeral=True)
                return
            records = await load_records()
            user_id_str = str(self.user_id)
            if user_id_str in records:
                new_entries = [e for e in records[user_id_str] if e.get("work_name") != self.work_name]
                removed_count = len(records[user_id_str]) - len(new_entries)
                records[user_id_str] = new_entries
                if not records[user_id_str]:
                    del records[user_id_str]
                await save_records(records)
                await log_audit("حذف_عمل_كامل", interaction.user.id, self.user_id, f"حذف عمل {self.work_name} ({removed_count} فصل)")
                await update_stats()
                await interaction.followup.send(f"✅ تم حذف عمل `{self.work_name}` بالكامل ({removed_count} فصل).", ephemeral=True)
            else:
                await interaction.followup.send("❌ لا توجد سجلات لهذا العضو.", ephemeral=True)
        elif self.values[0] == "delete_chapter":
            records = await load_records()
            user_id_str = str(self.user_id)
            if user_id_str not in records:
                await interaction.response.send_message("❌ لا توجد سجلات لهذا العضو.", ephemeral=True)
                return
            work_entries = [e for e in records[user_id_str] if e.get("work_name") == self.work_name]
            if not work_entries:
                await interaction.response.send_message("❌ لا توجد فصول لهذا العمل.", ephemeral=True)
                return
            options = []
            for e in work_entries:
                options.append(discord.SelectOption(label=f"فصل {e.get('chapter')}", value=e.get('chapter'), description=f"النوع: {e.get('work_type')}"))
            options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
            select = discord.ui.Select(placeholder="اختر الفصل المراد حذفه...", options=options)
            async def select_callback(interaction2):
                if select.values[0] == "cancel":
                    await interaction2.response.edit_message(content="تم الإلغاء.", view=None)
                    return
                chapter = select.values[0]
                await interaction2.response.send_message(f"⚠️ هل أنت متأكد من حذف الفصل {chapter} من عمل `{self.work_name}`؟\nأرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
                def check(m):
                    return m.author == interaction2.user and m.content == "تأكيد" and m.channel == interaction2.channel
                try:
                    await bot.wait_for('message', timeout=30.0, check=check)
                except:
                    await interaction2.followup.send("❌ تم إلغاء العملية.", ephemeral=True)
                    return
                records2 = await load_records()
                if user_id_str in records2:
                    new_entries = [e for e in records2[user_id_str] if not (e.get("work_name") == self.work_name and e.get("chapter") == chapter)]
                    removed = len(records2[user_id_str]) - len(new_entries)
                    records2[user_id_str] = new_entries
                    if not records2[user_id_str]:
                        del records2[user_id_str]
                    await save_records(records2)
                    await log_audit("حذف_فصل", interaction2.user.id, self.user_id, f"حذف فصل {chapter} من عمل {self.work_name}")
                    await update_stats()
                    await interaction2.followup.send(f"✅ تم حذف الفصل {chapter} من عمل `{self.work_name}`.", ephemeral=True)
                else:
                    await interaction2.followup.send("❌ لا توجد سجلات لهذا العضو.", ephemeral=True)
            select.callback = select_callback
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            await interaction.response.edit_message(content="**اختر الفصل المراد حذفه:**", view=view)

@bot.tree.command(name="حذف", description="حذف سجلات العضو - للمشرفين")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def delete_advanced(interaction: discord.Interaction, member: discord.Member, work_name: str = None):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "حذف")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        await interaction.response.send_message("❌ استخدم الأمر في القنوات المسموحة.", ephemeral=True)
        return
    records = await load_records()
    user_id_str = str(member.id)
    if user_id_str not in records or not records[user_id_str]:
        await interaction.response.send_message("❌ هذا العضو ما عنده أي شغل محفوظ.", ephemeral=True)
        return
    if work_name:
        work_exists = any(e.get("work_name") == work_name for e in records[user_id_str])
        if not work_exists:
            await interaction.response.send_message(f"❌ لا يوجد عمل باسم `{work_name}` لهذا العضو.", ephemeral=True)
            return
        view = discord.ui.View(timeout=60)
        select = DeleteSelect(member.id, work_name)
        view.add_item(select)
        await interaction.response.send_message(f"**🗑️ خيارات الحذف لعضو:** {member.mention}\n**العمل:** `{work_name}`", view=view)
    else:
        works = set(e.get("work_name") for e in records[user_id_str])
        options = []
        for w in works:
            options.append(discord.SelectOption(label=f"📖 {w}", value=w))
        options.append(discord.SelectOption(label="👤 حذف كل سجلات العضو", value="delete_all_user"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        if len(options) > 25:
            options = options[:25]
        select = discord.ui.Select(placeholder="اختر عملاً أو خياراً...", options=options)
        async def select_callback(interaction2):
            if select.values[0] == "cancel":
                await interaction2.response.edit_message(content="تم الإلغاء.", view=None)
                return
            if select.values[0] == "delete_all_user":
                await interaction2.response.send_message("⚠️ **تحذير:** هل أنت متأكد من حذف كل سجلات هذا العضو؟\nأرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
                def check(m):
                    return m.author == interaction2.user and m.content == "تأكيد"
                try:
                    await bot.wait_for('message', timeout=30.0, check=check)
                except:
                    await interaction2.followup.send("❌ تم إلغاء العملية.", ephemeral=True)
                    return
                records2 = await load_records()
                if str(member.id) in records2:
                    del records2[str(member.id)]
                    await save_records(records2)
                    await log_audit("حذف_كل_سجلات_العضو", interaction2.user.id, member.id, "حذف كل السجلات")
                    await update_stats()
                    await interaction2.followup.send(f"✅ تم حذف كل سجلات العضو.", ephemeral=True)
                else:
                    await interaction2.followup.send("❌ لا توجد سجلات.", ephemeral=True)
            else:
                work = select.values[0]
                view2 = discord.ui.View(timeout=60)
                select2 = DeleteSelect(member.id, work)
                view2.add_item(select2)
                await interaction2.response.edit_message(content=f"**خيارات الحذف لعمل `{work}`:**", view=view2)
        select.callback = select_callback
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(f"**🗑️ اختر العمل أو الإجراء لعضو:** {member.mention}", view=view)

@bot.command(name="حذف")
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def delete_work_text(ctx, member: discord.Member = None, number: int = None):
    if member is None or number is None:
        await ctx.send("**الاستخدام:** `!حذف @member 2`\nأو استخدم الأمر `/حذف` للخيارات المتقدمة.")
        return
    records = await load_records()
    user_id = str(member.id)
    if user_id not in records or not records[user_id]:
        await ctx.send("❌ هذا العضو ما عنده أي شغل محفوظ.")
        return
    if number < 1 or number > len(records[user_id]):
        await ctx.send("❌ رقم السجل غير صحيح.")
        return
    deleted = records[user_id].pop(number - 1)
    if not records[user_id]:
        del records[user_id]
    await save_records(records)
    await log_audit("حذف سجل (نصي)", ctx.author.id, member.id, f"السجل #{number}: {deleted.get('work_name')} - فصل {deleted.get('chapter')}")
    await update_stats()
    embed = discord.Embed(title="🗑️ **تم حذف السجل**", color=discord.Color.red())
    embed.add_field(name="**المستخدم**", value=member.mention, inline=True)
    embed.add_field(name="**العمل**", value=deleted.get('work_name', 'غير محدد'), inline=True)
    embed.add_field(name="**الفصل**", value=deleted.get('chapter', 'غير محدد'), inline=True)
    embed.add_field(name="**النوع**", value=deleted.get('work_type', 'غير محدد'), inline=True)
    embed.add_field(name="**المبلغ**", value=f"{SETTINGS.get('currency', '$')}{deleted.get('total', 0):.2f}", inline=True)
    await ctx.send(embed=embed)

@bot.tree.command(name="حذف_الكل", description="حذف كل السجلات - للمشرفين")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.user.id, i.command.qualified_name))
async def delete_all_work_slash(interaction: discord.Interaction):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "حذف_الكل")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        await interaction.response.send_message("❌ القناة غير مسموحة.", ephemeral=True)
        return
    records = await load_records()
    total = sum(len(items) for items in records.values())
    if total == 0:
        await interaction.response.send_message("📭 ما فيه أي سجلات.", ephemeral=True)
        return
    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", interaction.user.id, None, f"{total} سجل")
    await update_stats()
    await interaction.response.send_message(f"🗑️ تم حذف كل السجلات ({total}).")

@bot.command(name="حذف_الكل")
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 10, commands.BucketType.user)
async def delete_all_work_text(ctx):
    records = await load_records()
    total = sum(len(items) for items in records.values())
    if total == 0:
        await ctx.send("📭 ما فيه أي سجلات.")
        return
    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", ctx.author.id, None, f"{total} سجل")
    await update_stats()
    await ctx.send(f"🗑️ تم حذف كل السجلات ({total}).")

# ----------------------------------------------------------------------
# NEW: /حذف_كل_الأعمال (Admin deletes all works)
# ----------------------------------------------------------------------
@bot.tree.command(name="حذف_كل_الأعمال", description="حذف جميع الأعمال من القائمة (للمشرفين فقط)")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.user.id, i.command.qualified_name))
async def delete_all_works(interaction: discord.Interaction):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "حذف_كل_الأعمال")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    works = await load_works()
    if not works:
        await interaction.response.send_message("📭 لا توجد أعمال في القائمة.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"⚠️ **تحذير:** سيتم حذف جميع الأعمال ({len(works)} عمل) من القائمة.\n"
        "لن تتأثر السجلات.\n"
        "اكتب `تأكيد` خلال 30 ثانية للمتابعة.",
        ephemeral=True
    )
    def check(m):
        return m.author == interaction.user and m.content == "تأكيد" and m.channel == interaction.channel
    try:
        await bot.wait_for('message', timeout=30.0, check=check)
    except:
        await interaction.followup.send("❌ تم إلغاء العملية.", ephemeral=True)
        return

    await save_works([])
    await log_audit("حذف_كل_الأعمال", interaction.user.id, None, f"تم حذف {len(works)} عمل")
    await interaction.followup.send(f"✅ تم حذف جميع الأعمال ({len(works)} عمل) من القائمة.", ephemeral=True)

# ----------------------------------------------------------------------
# Work details view (with back button)
# ----------------------------------------------------------------------
class WorkDetailsView(discord.ui.View):
    def __init__(self, work_name, chapters_list, user_id, user_name, currency, back_callback: callable = None):
        super().__init__(timeout=120)
        self.work_name = work_name
        self.chapters_list = chapters_list
        self.user_id = user_id
        self.user_name = user_name
        self.currency = currency
        self.current_page = 0
        self.items_per_page = 10
        self.total_pages = (len(chapters_list) + self.items_per_page - 1) // self.items_per_page
        self.back_callback = back_callback
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.back_callback:
            back_btn = discord.ui.Button(label="◀ رجوع", style=discord.ButtonStyle.secondary)
            back_btn.callback = self.back_callback
            self.add_item(back_btn)
        if self.total_pages > 1:
            if self.current_page > 0:
                prev_button = discord.ui.Button(label="◀ السابق", style=discord.ButtonStyle.primary)
                prev_button.callback = self.previous_page
                self.add_item(prev_button)
            if self.current_page < self.total_pages - 1:
                next_button = discord.ui.Button(label="التالي ▶", style=discord.ButtonStyle.primary)
                next_button.callback = self.next_page
                self.add_item(next_button)
        close_button = discord.ui.Button(label="❌ إغلاق", style=discord.ButtonStyle.danger)
        close_button.callback = self.close_view
        self.add_item(close_button)

    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def close_view(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="تم إغلاق التفاصيل.", embed=None, view=None)

    def get_embed(self):
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_chapters = self.chapters_list[start:end]
        embed = discord.Embed(title=f"**تفاصيل عمل: {self.work_name}**", color=discord.Color.teal())
        embed.set_author(name=self.user_name)
        total_amount = sum(ch['total'] for ch in self.chapters_list)
        embed.add_field(name="**📊 إجمالي الفصول**", value=str(len(self.chapters_list)), inline=True)
        embed.add_field(name="**💰 إجمالي المبلغ**", value=f"{self.currency}{total_amount:.2f}", inline=True)
        for ch in page_chapters:
            embed.add_field(
                name=f"**📖 فصل {ch['chapter']}**",
                value=f"**النوع:** {ch['type']}\n**المبلغ:** {self.currency}{ch['total']:.2f}\n**ملاحظات:** {ch.get('notes', 'لا توجد')}",
                inline=False
            )
        if self.total_pages > 1:
            embed.set_footer(text=f"صفحة {self.current_page+1} من {self.total_pages}")
        return embed

# ----------------------------------------------------------------------
# /الأعمال command
# ----------------------------------------------------------------------
async def get_works_info(guild: discord.Guild):
    """Build list of works with their contributors."""
    approved_works = await load_works()
    records = await load_records()

    contrib_map = defaultdict(lambda: defaultdict(int))
    for user_id_str, entries in records.items():
        for entry in entries:
            work = entry.get("work_name")
            if work:
                contrib_map[work][user_id_str] += 1

    works_info = []
    for w in approved_works:
        work_name = w["name"]
        contributors = contrib_map.get(work_name, {})
        members_list = []
        for uid_str, count in contributors.items():
            uid = int(uid_str)
            username_hint = None
            if uid_str in records:
                for e in records[uid_str]:
                    if e.get("username"):
                        username_hint = e["username"]
                        break
            display = format_member_display(guild, uid, username_hint)
            members_list.append((uid, display))
        works_info.append((work_name, members_list))
    return works_info

class MemberSelect(discord.ui.Select):
    def __init__(self, work_name, members_info, guild, works_info_callback=None):
        self.work_name = work_name
        self.members_info = members_info
        self.guild = guild
        self.works_info_callback = works_info_callback
        options = []
        for uid, name in members_info[:24]:
            options.append(discord.SelectOption(label=name, value=str(uid), description="عرض فصوله في هذا العمل"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        super().__init__(placeholder="اختر عضواً...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "cancel":
            await interaction.response.edit_message(content="تم الإلغاء.", view=None)
            return
        user_id = int(self.values[0])
        user_display = next((name for uid, name in self.members_info if uid == user_id), str(user_id))
        records = await load_records()
        user_entries = records.get(str(user_id), [])
        work_entries = [e for e in user_entries if e.get("work_name") == self.work_name]
        if not work_entries:
            await interaction.response.send_message(f"❌ لا توجد فصول للعضو {user_display} في عمل {self.work_name}.", ephemeral=True)
            return
        chapters_details = []
        for e in work_entries:
            chapters_details.append({
                "chapter": e.get("chapter"),
                "type": e.get("work_type"),
                "total": e.get("total", 0),
                "notes": e.get("notes", "")
            })

        async def back_to_members(interaction2: discord.Interaction):
            select = MemberSelect(self.work_name, self.members_info, self.guild, self.works_info_callback)
            view_back = discord.ui.View(timeout=60)
            view_back.add_item(select)
            if self.works_info_callback:
                back_list_btn = discord.ui.Button(label="◀ رجوع للقائمة", style=discord.ButtonStyle.secondary)
                back_list_btn.callback = self.works_info_callback
                view_back.add_item(back_list_btn)
            await interaction2.response.edit_message(content=f"**اختر عضواً من عمل `{self.work_name}`:**", view=view_back)

        view_details = WorkDetailsView(
            self.work_name, chapters_details, user_id, user_display,
            SETTINGS.get('currency', '$'), back_callback=back_to_members
        )
        await interaction.response.edit_message(content=None, embed=view_details.get_embed(), view=view_details)

class WorkSelect(discord.ui.Select):
    def __init__(self, works_info, guild, works_info_callback=None):
        self.works_info = works_info
        self.guild = guild
        self.works_info_callback = works_info_callback
        options = []
        for work_name, _ in works_info[:24]:
            options.append(discord.SelectOption(label=work_name, value=work_name))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        super().__init__(placeholder="اختر العمل...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "cancel":
            await interaction.response.edit_message(content="تم الإلغاء.", view=None)
            return
        work_name = self.values[0]
        members_info = [(uid, name) for work, members in self.works_info if work == work_name for uid, name in members]
        if not members_info:
            await interaction.response.send_message(f"❌ لا يوجد مساهمين في عمل {work_name}.", ephemeral=True)
            return
        select = MemberSelect(work_name, members_info, self.guild, self.works_info_callback)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        if self.works_info_callback:
            back_btn = discord.ui.Button(label="◀ رجوع للقائمة", style=discord.ButtonStyle.secondary)
            back_btn.callback = self.works_info_callback
            view.add_item(back_btn)
        await interaction.response.edit_message(content=f"**اختر عضواً من عمل `{work_name}`:**", view=view)

class WorksPaginator(discord.ui.View):
    def __init__(self, all_works_info, guild):
        super().__init__(timeout=120)
        self.all_works_info = all_works_info
        self.guild = guild
        self.current_page = 0
        self.per_page = 24
        self.total_pages = max(1, (len(all_works_info) + self.per_page - 1) // self.per_page)
        self.update_buttons()

    async def show_works_list(self, interaction: discord.Interaction):
        new_info = await get_works_info(self.guild)
        self.all_works_info = new_info
        self.current_page = 0
        self.total_pages = max(1, (len(new_info) + self.per_page - 1) // self.per_page)
        self.update_buttons()
        embed = discord.Embed(title="📚 **قائمة الأعمال**", color=discord.Color.purple())
        embed.add_field(name="عدد الأعمال", value=str(len(new_info)), inline=False)
        embed.set_footer(text="اختر عملاً من القائمة لرؤية المساهمين. استخدم أزرار التنقل للصفحات.")
        await interaction.response.edit_message(embed=embed, view=self)

    def update_buttons(self):
        self.clear_items()
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_works = self.all_works_info[start:end]
        select = WorkSelect(page_works, self.guild, works_info_callback=self.show_works_list)
        self.add_item(select)
        if self.total_pages > 1:
            if self.current_page > 0:
                prev_btn = discord.ui.Button(label="◀ السابق", style=discord.ButtonStyle.primary)
                prev_btn.callback = self.previous_page
                self.add_item(prev_btn)
            if self.current_page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="التالي ▶", style=discord.ButtonStyle.primary)
                next_btn.callback = self.next_page
                self.add_item(next_btn)

    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

@bot.tree.command(name="الأعمال", description="عرض جميع الأعمال (المشاريع) والمساهمين")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def projects_report(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        await interaction.response.send_message("❌ القناة غير مسموحة.", ephemeral=True)
        return
    works_info = await get_works_info(interaction.guild)
    if not works_info:
        await interaction.response.send_message("📭 لا توجد أعمال مسجلة في القائمة.", ephemeral=True)
        return
    embed = discord.Embed(title="📚 **قائمة الأعمال**", color=discord.Color.purple())
    embed.add_field(name="عدد الأعمال", value=str(len(works_info)), inline=False)
    embed.set_footer(text="اختر عملاً من القائمة لرؤية المساهمين. استخدم أزرار التنقل للصفحات.")
    view = WorksPaginator(works_info, interaction.guild)
    await interaction.response.send_message(embed=embed, view=view)

# ----------------------------------------------------------------------
# Stats command
# ----------------------------------------------------------------------
@bot.tree.command(name="احصائيات", description="عرض إحصائيات متقدمة")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def stats(interaction: discord.Interaction):
    stat_doc = await stats_collection.find_one({"_id": "stats"})
    if not stat_doc:
        await interaction.response.send_message("لا توجد إحصائيات بعد.", ephemeral=True)
        return
    total_entries = stat_doc.get("total_entries", 0)
    total_amount = stat_doc.get("total_amount", 0)
    type_counts = stat_doc.get("type_counts", {})
    daily = stat_doc.get("daily", {"entries":0, "amount":0})
    weekly = stat_doc.get("weekly", {"entries":0, "amount":0})
    monthly = stat_doc.get("monthly", {"entries":0, "amount":0})
    top_members = stat_doc.get("top_members", [])
    last_updated = stat_doc.get("last_updated", "غير معروف")

    embed = discord.Embed(title="📊 **إحصائيات شاملة**", color=discord.Color.teal())
    embed.add_field(name="**📄 إجمالي السجلات**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    type_lines = "\n".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in type_counts.items()])
    embed.add_field(name="**📊 تفصيل الأنواع**", value=type_lines, inline=False)
    embed.add_field(name="**📅 اليوم**", value=f"سجلات: {daily['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{daily['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الأسبوع**", value=f"سجلات: {weekly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{weekly['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الشهر**", value=f"سجلات: {monthly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{monthly['amount']:.2f}", inline=True)
    if top_members:
        top_list = ""
        records = await load_records()
        for i, (uid, stats_data) in enumerate(top_members[:5], 1):
            uid_int = int(uid)
            username_hint = None
            if uid in records:
                for e in records[uid]:
                    if e.get("username"):
                        username_hint = e["username"]
                        break
            display = format_member_display(interaction.guild, uid_int, username_hint)
            top_list += f"{i}. {display} - {stats_data['total_amount']:.2f} {SETTINGS.get('currency', '$')} ({stats_data['total_entries']} فصل)\n"
        embed.add_field(name="**🏆 أفضل 5 أعضاء**", value=top_list, inline=False)
    embed.set_footer(text=f"آخر تحديث: {last_updated[:19] if last_updated != 'غير معروف' else last_updated}")
    await interaction.response.send_message(embed=embed)

# ----------------------------------------------------------------------
# My works / member works commands
# ----------------------------------------------------------------------
@bot.tree.command(name="أعمالي", description="عرض أعمالك مجمعة مع المكافآت والخصومات")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def my_works_slash(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        await interaction.response.send_message("❌ القناة غير مسموحة.", ephemeral=True)
        return
    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("📭 ليس لديك أي شغل.", ephemeral=True)
        return

    # فصل الأعمال العادية عن المكافآت والخصومات
    works = {}
    bonuses = []
    deductions = []
    for entry in records[user_id]:
        wtype = entry.get("work_type")
        if wtype == "مكافأة":
            bonuses.append(entry)
        elif wtype == "خصم":
            deductions.append(entry)
        else:
            work = entry.get("work_name", "غير محدد")
            works.setdefault(work, []).append(entry)

    embed = discord.Embed(title=f"**📚 أعمال {interaction.user.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {}
        for e in entries:
            wtype = e.get("work_type")
            types_count[wtype] = types_count.get(wtype, 0) + 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(name=f"**▸ {work}**", value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}", inline=False)

    # المكافآت والخصومات
    total_bonus = sum(e.get("total", 0) for e in bonuses)
    total_deduction = sum(abs(e.get("total", 0)) for e in deductions)
    total_all += total_bonus - total_deduction

    if bonuses or deductions:
        details = ""
        if total_bonus > 0:
            details += f"🎁 إجمالي المكافآت: {SETTINGS.get('currency', '$')}{total_bonus:.2f}\n"
        if total_deduction > 0:
            details += f"🔻 إجمالي الخصومات: {SETTINGS.get('currency', '$')}{total_deduction:.2f}\n"
        embed.add_field(name="**⚖️ مكافآت وخصومات**", value=details, inline=False)

    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)

    # أزرار تفاصيل الأعمال
    view = discord.ui.View(timeout=60)
    for work, entries in list(works.items())[:5]:
        chapters_details = [{"chapter": e.get("chapter"), "type": e.get("work_type"), "total": e.get("total", 0), "notes": e.get("notes", "")} for e in entries]
        button = discord.ui.Button(label=f"📖 {work}", style=discord.ButtonStyle.secondary)
        async def btn_cb(interaction, wn=work, ch_list=chapters_details):
            v = WorkDetailsView(wn, ch_list, user_id, interaction.user.display_name, SETTINGS.get('currency', '$'))
            await interaction.response.send_message(embed=v.get_embed(), view=v, ephemeral=True)
        button.callback = btn_cb
        view.add_item(button)
    await interaction.response.send_message(embed=embed, view=view)

@bot.command(name="أعمالي")
@commands.cooldown(1, 5, commands.BucketType.user)
async def my_works_text(ctx):
    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records or not records[user_id]:
        await ctx.send("📭 ليس لديك أي شغل.")
        return

    works = {}
    bonuses = []
    deductions = []
    for entry in records[user_id]:
        wtype = entry.get("work_type")
        if wtype == "مكافأة":
            bonuses.append(entry)
        elif wtype == "خصم":
            deductions.append(entry)
        else:
            work = entry.get("work_name", "غير محدد")
            works.setdefault(work, []).append(entry)

    embed = discord.Embed(title=f"**📚 أعمال {ctx.author.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {}
        for e in entries:
            wtype = e.get("work_type")
            types_count[wtype] = types_count.get(wtype, 0) + 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(name=f"**▸ {work}**", value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}", inline=False)

    total_bonus = sum(e.get("total", 0) for e in bonuses)
    total_deduction = sum(abs(e.get("total", 0)) for e in deductions)
    total_all += total_bonus - total_deduction

    if bonuses or deductions:
        details = ""
        if total_bonus > 0:
            details += f"🎁 إجمالي المكافآت: {SETTINGS.get('currency', '$')}{total_bonus:.2f}\n"
        if total_deduction > 0:
            details += f"🔻 إجمالي الخصومات: {SETTINGS.get('currency', '$')}{total_deduction:.2f}\n"
        embed.add_field(name="**⚖️ مكافآت وخصومات**", value=details, inline=False)

    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    await ctx.send(embed=embed)

@bot.tree.command(name="شغل", description="عرض شغل عضو مجمّع مع المكافآت والخصومات")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def show_work_slash(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        await interaction.response.send_message("❌ القناة غير مسموحة.", ephemeral=True)
        return
    target = member or interaction.user
    records = await load_records()
    user_id = str(target.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message(f"📭 لا يوجد شغل للعضو {target.mention}.", ephemeral=True)
        return

    works = {}
    bonuses = []
    deductions = []
    for entry in records[user_id]:
        wtype = entry.get("work_type")
        if wtype == "مكافأة":
            bonuses.append(entry)
        elif wtype == "خصم":
            deductions.append(entry)
        else:
            work = entry.get("work_name", "غير محدد")
            works.setdefault(work, []).append(entry)

    embed = discord.Embed(title=f"**📚 شغل {target.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {}
        for e in entries:
            wtype = e.get("work_type")
            types_count[wtype] = types_count.get(wtype, 0) + 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(name=f"**▸ {work}**", value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}", inline=False)

    total_bonus = sum(e.get("total", 0) for e in bonuses)
    total_deduction = sum(abs(e.get("total", 0)) for e in deductions)
    total_all += total_bonus - total_deduction

    if bonuses or deductions:
        details = ""
        if total_bonus > 0:
            details += f"🎁 إجمالي المكافآت: {SETTINGS.get('currency', '$')}{total_bonus:.2f}\n"
        if total_deduction > 0:
            details += f"🔻 إجمالي الخصومات: {SETTINGS.get('currency', '$')}{total_deduction:.2f}\n"
        embed.add_field(name="**⚖️ مكافآت وخصومات**", value=details, inline=False)

    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)

    view = discord.ui.View(timeout=60)
    for work, entries in list(works.items())[:5]:
        chapters_details = [{"chapter": e.get("chapter"), "type": e.get("work_type"), "total": e.get("total", 0), "notes": e.get("notes", "")} for e in entries]
        btn = discord.ui.Button(label=f"📖 {work}", style=discord.ButtonStyle.secondary)
        async def btn_cb(interaction, wn=work, chl=chapters_details):
            v = WorkDetailsView(wn, chl, user_id, target.display_name, SETTINGS.get('currency', '$'))
            await interaction.response.send_message(embed=v.get_embed(), view=v, ephemeral=True)
        btn.callback = btn_cb
        view.add_item(btn)
    await interaction.response.send_message(embed=embed, view=view)

@bot.command(name="شغل")
@commands.cooldown(1, 5, commands.BucketType.user)
async def show_work_text(ctx, member: discord.Member = None):
    member = member or ctx.author
    records = await load_records()
    user_id = str(member.id)
    if user_id not in records or not records[user_id]:
        await ctx.send(f"📭 ما عندي أي شغل للعضو {member.mention}.")
        return

    works = {}
    bonuses = []
    deductions = []
    for entry in records[user_id]:
        wtype = entry.get("work_type")
        if wtype == "مكافأة":
            bonuses.append(entry)
        elif wtype == "خصم":
            deductions.append(entry)
        else:
            work = entry.get("work_name", "غير محدد")
            works.setdefault(work, []).append(entry)

    embed = discord.Embed(title=f"**📚 شغل {member.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {}
        for e in entries:
            wtype = e.get("work_type")
            types_count[wtype] = types_count.get(wtype, 0) + 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(name=f"**▸ {work}**", value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}", inline=False)

    total_bonus = sum(e.get("total", 0) for e in bonuses)
    total_deduction = sum(abs(e.get("total", 0)) for e in deductions)
    total_all += total_bonus - total_deduction

    if bonuses or deductions:
        details = ""
        if total_bonus > 0:
            details += f"🎁 إجمالي المكافآت: {SETTINGS.get('currency', '$')}{total_bonus:.2f}\n"
        if total_deduction > 0:
            details += f"🔻 إجمالي الخصومات: {SETTINGS.get('currency', '$')}{total_deduction:.2f}\n"
        embed.add_field(name="**⚖️ مكافآت وخصومات**", value=details, inline=False)

    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    await ctx.send(embed=embed)

# ----------------------------------------------------------------------
# Other admin commands
# ----------------------------------------------------------------------
@bot.tree.command(name="لوحة_التحكم", description="لوحة تحكم للمشرفين")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def dashboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "لوحة_التحكم")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    records = await load_records()
    total_users = len(records)
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = sum(sum(e.get("total", 0) for e in entries) for entries in records.values())
    embed = discord.Embed(title="🖥️ **لوحة التحكم**", color=discord.Color.gold())
    embed.add_field(name="**👥 عدد الأعضاء النشطين**", value=total_users, inline=True)
    embed.add_field(name="**📄 عدد السجلات الكلي**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="**⚙️ العملة**", value=SETTINGS.get('currency', '$'), inline=True)
    embed.add_field(name="**🔔 قناة الإشعارات**", value=f"<#{SETTINGS.get('notify_channel_id')}>" if SETTINGS.get('notify_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**💾 قناة النسخ الاحتياطي**", value=f"<#{SETTINGS.get('daily_backup_channel_id')}>" if SETTINGS.get('daily_backup_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**⚠️ حد التنبيه**", value=f"{SETTINGS.get('currency', '$')}{SETTINGS.get('alert_threshold', 10):.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="سجل", description="عرض آخر 20 عملية إدارية")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def audit_log(interaction: discord.Interaction):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "سجل")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    logs = await audit_collection.find().sort("timestamp", -1).limit(20).to_list(length=20)
    if not logs:
        await interaction.response.send_message("لا توجد سجلات.", ephemeral=True)
        return
    embed = discord.Embed(title="📜 **سجل العمليات**", color=discord.Color.dark_gray())
    for log in logs:
        embed.add_field(
            name=f"**{log.get('action', 'غير معروف')}**",
            value=f"بواسطة: <@{log.get('moderator_id')}>\nللـ: {log.get('target_id') if log.get('target_id') else 'عام'}\nالتفاصيل: {log.get('details')}\nالوقت: {log.get('timestamp')[:19]}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="تقريري", description="تقرير أسبوعي خاص بك")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def my_weekly_report(interaction: discord.Interaction):
    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records:
        await interaction.response.send_message("ليس لديك أي سجلات.", ephemeral=True)
        return
    week_ago = datetime.utcnow() - timedelta(days=7)
    week_entries = [e for e in records[user_id] if "timestamp" in e and datetime.fromisoformat(e["timestamp"]) > week_ago]
    if not week_entries:
        await interaction.response.send_message("لا يوجد سجلات خلال الأسبوع الماضي.", ephemeral=True)
        return
    total = sum(e.get("total", 0) for e in week_entries)
    embed = discord.Embed(title="📅 **تقريرك الأسبوعي**", color=discord.Color.green())
    embed.add_field(name="**عدد المهام**", value=len(week_entries), inline=True)
    embed.add_field(name="**المجموع**", value=f"{SETTINGS.get('currency', '$')}{total:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="تعديل", description="تعديل آخر سجل قمت بإضافته")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def edit_last(interaction: discord.Interaction, العمل: str = None, الفصل: str = None, النوع: str = None, ملاحظات: str = None):
    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("لا يوجد سجلات.", ephemeral=True)
        return
    last = records[user_id][-1]
    if العمل:
        last["work_name"] = العمل
    if الفصل:
        last["chapter"] = الفصل
    if النوع:
        norm_type = النوع.strip().replace(' ', '_')
        if norm_type not in PRICES:
            await interaction.response.send_message("النوع غير صحيح.", ephemeral=True)
            return
        last["work_type"] = norm_type
        last["total"] = PRICES[norm_type]
    if ملاحظات is not None:
        last["notes"] = ملاحظات
    await save_records(records)
    await update_stats()
    await interaction.response.send_message("✅ تم تعديل آخر سجل بنجاح.", ephemeral=True)

@bot.tree.command(name="تصدير", description="تصدير كل البيانات إلى Excel")
@app_commands.checks.cooldown(1, 10, key=lambda i: (i.user.id, i.command.qualified_name))
async def export_excel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تصدير")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    records = await load_records()
    rows = []
    for user_id, entries in records.items():
        user = interaction.guild.get_member(int(user_id))
        username = user.display_name if user else user_id
        for entry in entries:
            rows.append({
                "اسم العضو": username,
                "معرف العضو": user_id,
                "العمل": entry.get("work_name"),
                "الفصل": entry.get("chapter"),
                "النوع": entry.get("work_type"),
                "المبلغ": entry.get("total"),
                "ملاحظات": entry.get("notes"),
                "التاريخ": entry.get("timestamp", "")
            })
    df = pd.DataFrame(rows)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="الشغل")
    buffer.seek(0)
    await interaction.response.send_message(file=discord.File(buffer, filename="work_report.xlsx"))

@bot.tree.command(name="اعدادات", description="إعدادات البوت (للمشرفين)")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def bot_settings(interaction: discord.Interaction, العملة: str = None, قناة_الإشعارات: discord.TextChannel = None, قناة_النسخ: discord.TextChannel = None, حد_التنبيه: float = None):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "اعدادات")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    if العملة:
        SETTINGS["currency"] = العملة
    if قناة_الإشعارات:
        SETTINGS["notify_channel_id"] = قناة_الإشعارات.id
    if قناة_النسخ:
        SETTINGS["daily_backup_channel_id"] = قناة_النسخ.id
    if حد_التنبيه is not None:
        SETTINGS["alert_threshold"] = حد_التنبيه
    await save_settings(SETTINGS)
    await interaction.response.send_message("✅ تم تحديث الإعدادات.", ephemeral=True)

# ----------------------------------------------------------------------
# Works management commands
# ----------------------------------------------------------------------
@bot.tree.command(name="اضافة_عمل", description="إضافة عمل جديد إلى قائمة الأعمال المدفوعة (للمشرفين)")
@app_commands.describe(الاسم="اسم العمل", بداية_الفصول_المدفوعة="أول فصل مدفوع (اختياري، اتركه فارغاً إذا كان العمل كله مدفوع)", نشط="هل العمل نشط الآن؟")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def add_work(interaction: discord.Interaction, الاسم: str, بداية_الفصول_المدفوعة: int = None, نشط: bool = True):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "اضافة_عمل")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    works = await load_works()
    if any(w["name"] == الاسم for w in works):
        await interaction.response.send_message(f"❌ العمل `{الاسم}` موجود بالفعل.", ephemeral=True)
        return
    new_work = {"name": الاسم, "paid_start": بداية_الفصول_المدفوعة, "active": نشط}
    works.append(new_work)
    await save_works(works)
    await log_audit("اضافة_عمل", interaction.user.id, None, f"أضاف عمل {الاسم} (paid_start={بداية_الفصول_المدفوعة}, active={نشط})")
    desc = "كل الفصول مدفوعة" if بداية_الفصول_المدفوعة is None else f"يبدأ من فصل {بداية_الفصول_المدفوعة}"
    await interaction.response.send_message(f"✅ تمت إضافة العمل `{الاسم}`.\nالحالة: {desc} | نشط: {'✅' if نشط else '❌'}", ephemeral=True)

@bot.tree.command(name="حذف_عمل", description="حذف عمل من القائمة (للمشرفين)")
@app_commands.autocomplete(العمل=work_autocomplete)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def delete_work(interaction: discord.Interaction, العمل: str):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "حذف_عمل")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    works = await load_works()
    target = next((w for w in works if w["name"] == العمل), None)
    if not target:
        await interaction.response.send_message("❌ العمل غير موجود.", ephemeral=True)
        return

    view = discord.ui.View(timeout=60)
    async def delete_with_records(interaction2: discord.Interaction):
        await interaction2.response.send_message("⚠️ **تأكيد:** سيتم حذف العمل **وكل سجلاته** نهائياً.\nاكتب `تأكيد` خلال 30 ثانية.", ephemeral=True)
        def check(m):
            return m.author == interaction2.user and m.content == "تأكيد" and m.channel == interaction2.channel
        try:
            await bot.wait_for('message', timeout=30.0, check=check)
        except:
            await interaction2.followup.send("❌ تم الإلغاء.", ephemeral=True)
            return
        removed = await delete_all_records_of_work(العمل)
        new_works = [w for w in works if w["name"] != العمل]
        await save_works(new_works)
        await log_audit("حذف_عمل_مع_السجلات", interaction2.user.id, None, f"حذف {العمل} و {removed} سجل")
        await interaction2.followup.send(f"✅ تم حذف العمل `{العمل}` وكل سجلاته ({removed} سجل).", ephemeral=True)

    async def delete_work_only(interaction2: discord.Interaction):
        await interaction2.response.send_message("⚠️ **تأكيد:** سيتم حذف العمل من القائمة فقط (السجلات تبقى).\nاكتب `تأكيد` خلال 30 ثانية.", ephemeral=True)
        def check(m):
            return m.author == interaction2.user and m.content == "تأكيد" and m.channel == interaction2.channel
        try:
            await bot.wait_for('message', timeout=30.0, check=check)
        except:
            await interaction2.followup.send("❌ تم الإلغاء.", ephemeral=True)
            return
        new_works = [w for w in works if w["name"] != العمل]
        await save_works(new_works)
        await log_audit("حذف_عمل_فقط", interaction2.user.id, None, f"حذف {العمل} من القائمة (السجلات باقية)")
        await interaction2.followup.send(f"✅ تم حذف العمل `{العمل}` من القائمة (السجلات لم تمس).", ephemeral=True)

    delete_with_btn = discord.ui.Button(label="🗑️ حذف العمل وكل سجلاته", style=discord.ButtonStyle.danger)
    delete_with_btn.callback = delete_with_records
    delete_only_btn = discord.ui.Button(label="📁 حذف العمل فقط (إخفاؤه)", style=discord.ButtonStyle.primary)
    delete_only_btn.callback = delete_work_only
    cancel_btn = discord.ui.Button(label="❌ إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel_cb(interaction2: discord.Interaction):
        await interaction2.response.edit_message(content="تم الإلغاء.", view=None)
    cancel_btn.callback = cancel_cb
    view.add_item(delete_with_btn)
    view.add_item(delete_only_btn)
    view.add_item(cancel_btn)
    await interaction.response.send_message(f"**🗑️ حذف العمل:** `{العمل}`\nاختر الطريقة:", view=view, ephemeral=True)

@bot.tree.command(name="تعديل_عمل", description="تعديل بيانات عمل (للمشرفين)")
@app_commands.autocomplete(العمل=work_autocomplete)
@app_commands.describe(العمل="اختر العمل", الاسم_الجديد="اسم جديد (اختياري)", بداية_الفصول_المدفوعة="أول فصل مدفوع (اتركه فارغاً إن لم يتغير)", الكل_مدفوع="تفعيل إذا كان العمل كله مدفوعاً", نشط="حالة النشاط")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def edit_work(interaction: discord.Interaction, العمل: str, الاسم_الجديد: str = None, بداية_الفصول_المدفوعة: int = None, الكل_مدفوع: bool = False, نشط: bool = None):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تعديل_عمل")
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    works = await load_works()
    target = next((w for w in works if w["name"] == العمل), None)
    if not target:
        await interaction.response.send_message("❌ العمل غير موجود.", ephemeral=True)
        return
    changed = []
    if الاسم_الجديد and الاسم_الجديد != target["name"]:
        if any(w["name"] == الاسم_الجديد for w in works):
            await interaction.response.send_message("❌ الاسم الجديد موجود مسبقاً.", ephemeral=True)
            return
        target["name"] = الاسم_الجديد
        changed.append(f"الاسم → {الاسم_الجديد}")
    if الكل_مدفوع:
        target["paid_start"] = None
        changed.append("كل الفصول مدفوعة")
    elif بداية_الفصول_المدفوعة is not None:
        target["paid_start"] = بداية_الفصول_المدفوعة
        changed.append(f"بداية الدفع = {بداية_الفصول_المدفوعة}")
    if نشط is not None and نشط != target.get("active", True):
        target["active"] = نشط
        changed.append(f"نشط = {نشط}")
    if not changed:
        await interaction.response.send_message("لم تقم بأي تغيير.", ephemeral=True)
        return
    await save_works(works)
    await log_audit("تعديل_عمل", interaction.user.id, None, f"تعديل {العمل}: {', '.join(changed)}")
    await interaction.response.send_message(f"✅ تم تعديل العمل `{العمل}`:\n" + "\n".join(changed), ephemeral=True)

class WorksListPaginator(discord.ui.View):
    def __init__(self, works: list):
        super().__init__(timeout=120)
        self.works = works
        self.current_page = 0
        self.per_page = 20
        self.total_pages = max(1, (len(works) + self.per_page - 1) // self.per_page)
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.current_page > 0:
            prev_btn = discord.ui.Button(label="◀ السابق", style=discord.ButtonStyle.primary)
            prev_btn.callback = self.previous_page
            self.add_item(prev_btn)
        if self.current_page < self.total_pages - 1:
            next_btn = discord.ui.Button(label="التالي ▶", style=discord.ButtonStyle.primary)
            next_btn.callback = self.next_page
            self.add_item(next_btn)
        page_indicator = discord.ui.Button(
            label=f"صفحة {self.current_page + 1} من {self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True
        )
        self.add_item(page_indicator)

    def get_embed(self) -> discord.Embed:
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_works = self.works[start:end]
        embed = discord.Embed(title="📋 **قائمة الأعمال المدفوعة**", color=discord.Color.blurple())
        for w in page_works:
            paid_info = "كل الفصول مدفوعة" if w.get("paid_start") is None else f"يبدأ من فصل {w['paid_start']}"
            active_icon = "✅" if w.get("active", True) else "❌"
            embed.add_field(name=f"{active_icon} {w['name']}", value=paid_info, inline=False)
        return embed

    async def previous_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

@bot.tree.command(name="عرض_الاعمال", description="عرض قائمة الأعمال المدفوعة وحالتها")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def list_works(interaction: discord.Interaction):
    works = await load_works()
    if not works:
        await interaction.response.send_message("📭 لا توجد أعمال في القائمة.", ephemeral=True)
        return
    view = WorksListPaginator(works)
    await interaction.response.send_message(embed=view.get_embed(), view=view)

# ----------------------------------------------------------------------
# NEW: /مكافأة and /خصم (Admin bonus and deduction system)
# ----------------------------------------------------------------------
@bot.tree.command(name="مكافأة", description="إضافة مكافأة (مبلغ موجب) لعضو - للإدارة فقط")
@app_commands.describe(عضو="العضو المستحق للمكافأة", المبلغ="المبلغ الموجب المراد إضافته", السبب="سبب المكافأة (اختياري)")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def add_bonus(interaction: discord.Interaction, عضو: discord.Member, المبلغ: float, السبب: str = None):
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "مكافأة")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if المبلغ <= 0:
        await interaction.response.send_message("❌ المبلغ يجب أن يكون أكبر من صفر.", ephemeral=True)
        return

    records = await load_records()
    user_id = str(عضو.id)
    if user_id not in records:
        records[user_id] = []

    bonus_entry = {
        "work_name": "نظام المكافآت والخصومات",
        "chapter": "مكافأة",
        "work_type": "مكافأة",
        "total": abs(المبلغ),
        "notes": السبب or "",
        "timestamp": datetime.utcnow().isoformat(),
        "username": عضو.name,
        "added_by": str(interaction.user.id)
    }
    records[user_id].append(bonus_entry)
    await save_records(records)
    await update_stats()

    embed = discord.Embed(title="🎁 **تمت إضافة المكافأة**", color=discord.Color.green())
    embed.add_field(name="**👤 العضو**", value=عضو.mention, inline=True)
    embed.add_field(name="**💰 المبلغ**", value=f"{SETTINGS.get('currency', '$')}{abs(المبلغ):.2f}", inline=True)
    if السبب:
        embed.add_field(name="**📝 السبب**", value=السبب, inline=False)
    embed.add_field(name="**🛡️ أضيفت بواسطة**", value=interaction.user.mention, inline=True)
    await interaction.response.send_message(embed=embed)

    await log_audit("مكافأة", interaction.user.id, عضو.id, f"مكافأة {abs(المبلغ):.2f} - السبب: {السبب or 'غير محدد'}")
    try:
        await عضو.send(f"🎁 لقد تلقيت مكافأة بقيمة {SETTINGS.get('currency', '$')}{abs(المبلغ):.2f} من {interaction.user.mention}.\nالسبب: {السبب or 'غير محدد'}")
    except:
        pass

@bot.tree.command(name="خصم", description="خصم مبلغ (سالب) من عضو - للإدارة فقط")
@app_commands.describe(عضو="العضو المراد الخصم منه", المبلغ="المبلغ الموجب (سيتم خصمه)", السبب="سبب الخصم (اختياري)")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user