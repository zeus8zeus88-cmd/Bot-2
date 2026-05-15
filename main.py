import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
import re
from collections import defaultdict

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
collection = db["records"]
settings_collection = db["settings"]
audit_collection = db["audit_log"]
stats_collection = db["stats"]
projects_collection = db["projects"]

# Helper functions
async def load_records():
    """Load records from MongoDB"""
    print("[LOG] load_records() called - Attempting to fetch data from MongoDB...")
    try:
        doc = await collection.find_one({"_id": "records"})
        if doc and "data" in doc:
            print("[LOG] load_records() - Data found, returning records.")
            return doc["data"]
        else:
            print("[LOG] load_records() - No records found, returning empty dict.")
            return {}
    except Exception as e:
        print(f"[ERROR] load_records() - Failed to fetch data: {e}")
        return {}

async def save_records(records):
    """Save records to MongoDB"""
    print("[LOG] save_records() called - Attempting to save data to MongoDB...")
    try:
        await collection.update_one(
            {"_id": "records"},
            {"$set": {"data": records}},
            upsert=True
        )
        print("[LOG] save_records() - Data saved successfully.")
    except Exception as e:
        print(f"[ERROR] save_records() - Failed to save data: {e}")

async def load_settings():
    """Load settings (allowed channels, currency, notification channel, etc.) from MongoDB"""
    print("[LOG] load_settings() called")
    try:
        doc = await settings_collection.find_one({"_id": "settings"})
        if doc:
            return doc
        else:
            return {
                "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
                "currency": "$",
                "notify_channel_id": None,
                "daily_backup_channel_id": None,
                "alert_threshold": 10.0
            }
    except Exception as e:
        print(f"[ERROR] load_settings() - Failed: {e}")
        return {
            "allowed_channels": DEFAULT_ALLOWED_CHANNELS.copy(),
            "currency": "$",
            "notify_channel_id": None,
            "daily_backup_channel_id": None,
            "alert_threshold": 10.0
        }

async def save_settings(settings):
    """Save settings to MongoDB"""
    print(f"[LOG] save_settings() called")
    try:
        await settings_collection.update_one(
            {"_id": "settings"},
            {"$set": settings},
            upsert=True
        )
        print("[LOG] save_settings() - Settings saved successfully")
    except Exception as e:
        print(f"[ERROR] save_settings() - Failed: {e}")

async def log_audit(action, moderator_id, target_id, details):
    """Log an admin action to audit_log collection"""
    log_entry = {
        "action": action,
        "moderator_id": str(moderator_id),
        "target_id": str(target_id) if target_id else None,
        "details": details,
        "timestamp": datetime.utcnow().isoformat()
    }
    await audit_collection.insert_one(log_entry)

async def update_stats():
    """Update comprehensive stats"""
    records = await load_records()
    
    # Basic totals
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = 0
    type_counts = {k:0 for k in PRICES.keys()}
    member_stats = {}
    
    for user_id, entries in records.items():
        member_total = 0
        member_counts = {k:0 for k in PRICES.keys()}
        for entry in entries:
            amount = entry.get("total", 0)
            total_amount += amount
            member_total += amount
            wtype = entry.get("work_type")
            if wtype in type_counts:
                type_counts[wtype] += 1
                # إذا كان النوع غير موجود في القاموس، قم بإنشائه واعطه القيمة 0
if wtype not in member_counts:
    member_counts[wtype] = 0

member_counts[wtype] += 1

                type_counts[wtype] = type_counts.get(wtype,0)+1
                member_counts[wtype] = member_counts.get(wtype,0)+1
        member_stats[user_id] = {
            "total_amount": member_total,
            "total_entries": len(entries),
            "type_counts": member_counts
        }
    
    # Sort members by total amount (top 5)
    top_members = sorted(member_stats.items(), key=lambda x: x[1]["total_amount"], reverse=True)[:5]
    top_members_data = [(uid, stats) for uid, stats in top_members]
    
    # Calculate stats for different periods
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
    print("[LOG] Stats updated successfully")

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

def get_color_for_type(work_type):
    colors = {
        "تحرير": discord.Color.blue(),
        "ترجمة_كوري": discord.Color.green(),
        "ترجمة_انجليزي": discord.Color.green(),
        "تبييض": discord.Color.orange()
    }
    return colors.get(work_type, discord.Color.default())

def parse_chapter_range(range_str):
    """Parse chapter range like '1-5' or '1,2,3' and return list of chapter numbers as strings"""
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
    """
    Parse types input like 'ترجمة_كوري' (single type for all)
    or 'ترجمة_كوري-تحرير-تبييض' (dash-separated for each chapter)
    or 'ترجمة_كوري-تحرير' (if chapters_count==2)
    If pattern start: 'ترجمة_كوري-تحرير' and chapters_count>2, then first chapter gets first type, all remaining get second type.
    """
    types_input = types_input.strip()
    if '-' in types_input:
        parts = types_input.split('-')
        # If number of parts equals chapters_count, use as is
        if len(parts) == chapters_count:
            return [p.strip() for p in parts]
        elif len(parts) == 2:
            # Pattern: first type for first chapter, second type for rest
            first = parts[0].strip()
            rest = parts[1].strip()
            result = [first] + [rest] * (chapters_count - 1)
            return result
        else:
            # Not matching, try comma as fallback
            if ',' in types_input:
                return parse_mixed_types(types_input.replace('-', ','), chapters_count)
            else:
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
        # Single type for all chapters
        return [types_input] * chapters_count

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global variable for settings (will be set in on_ready)
SETTINGS = {}

# ---------- Ignore command not found errors ----------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands, do nothing
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ ما عندك صلاحية تستخدم هذا الأمر.")
        return
    # For other errors, you may log or send, but we'll keep it quiet for unknown commands only
    await ctx.send(f"⚠️ صار خطأ: `{error}`")

@bot.event
async def on_ready():
    global SETTINGS
    print(f"[LOG] Logged in as {bot.user}")
    
    # Load settings from DB
    SETTINGS = await load_settings()
    print(f"[LOG] Settings loaded: allowed_channels={SETTINGS.get('allowed_channels')}, currency={SETTINGS.get('currency')}")
    
    # Test MongoDB connection
    print("[LOG] Testing MongoDB connection...")
    try:
        await mongo_client.admin.command('ping')
        print("[LOG] MongoDB connection successful! (ping command succeeded)")
    except Exception as e:
        print(f"[ERROR] MongoDB connection failed: {e}")
    
    # Sync slash commands
    print("[LOG] Syncing slash commands...")
    await bot.tree.sync()
    print("[LOG] Slash commands synced")
    
    # Update stats at startup
    await update_stats()
    
    # Start daily backup task
    daily_backup.start()
    # Start stats update task (every hour)
    update_stats_task.start()

@bot.check
async def only_allowed_channel(ctx):
    if ctx.channel.name in SETTINGS.get("allowed_channels", []):
        return True
    # Only send message if command exists (ignore channel check for unknown commands? Actually we check before command runs)
    # But we will keep the message
    channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
    await ctx.send(f"❌ استخدم أوامر البوت فقط في أحد الرومات: {channels_str}.")
    return False

# ---------- Helper function to check admin permission ----------
def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_messages

# ---------- Tasks ----------
@tasks.loop(hours=24)
async def daily_backup():
    """Automatic backup every 24 hours"""
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

# ---------- Slash and Text command: تحديد_قنوات ----------
@bot.tree.command(name="تحديد_قنوات", description="تحديد القنوات المسموحة (قناتين كحد أقصى) - للإدارة فقط")
async def set_allowed_channels_slash(
    interaction: discord.Interaction, 
    channel1: discord.TextChannel, 
    channel2: discord.TextChannel = None
):
    if not is_admin(interaction):
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

# ---------- Slash command: رفع_البيانات ----------
@bot.tree.command(name="رفع_البيانات", description="رفع ملف records.json لاستعادة البيانات إلى MongoDB")
async def upload_records(interaction: discord.Interaction, file: discord.Attachment):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if not file.filename.endswith('.json'):
        await interaction.followup.send("❌ الملف يجب أن يكون بصيغة JSON.", ephemeral=True)
        return

    try:
        content = await file.read()
        data = json.loads(content.decode('utf-8'))

        if not isinstance(data, dict):
            await interaction.followup.send("❌ الملف غير صالح: البيانات الأساسية يجب أن تكون قاموساً (object).", ephemeral=True)
            return

        await collection.update_one(
            {"_id": "records"},
            {"$set": {"data": data}},
            upsert=True
        )

        total_users = len(data)
        total_entries = sum(len(entries) for entries in data.values() if isinstance(entries, list))

        print(f"[LOG] Data restored via slash command: users={total_users}, entries={total_entries}")
        await log_audit("رفع_البيانات", interaction.user.id, None, f"تم رفع {total_entries} سجل")
        
        # Update stats after restore
        await update_stats()

        await interaction.followup.send(
            f"✅ تم استعادة البيانات بنجاح!\n"
            f"عدد المستخدمين: {total_users}\n"
            f"إجمالي السجلات: {total_entries}",
            ephemeral=True
        )
    except json.JSONDecodeError:
        await interaction.followup.send("❌ الملف ليس بصيغة JSON صحيحة.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Slash command restore failed: {e}")
        await interaction.followup.send(f"❌ حدث خطأ: {str(e)}", ephemeral=True)

# ---------- Slash and Text command: اوامر (help) ----------
@bot.tree.command(name="اوامر", description="عرض قائمة بجميع أوامر البوت")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📌 **أوامر البوت**", color=discord.Color.purple())
    embed.add_field(name="**▸ تسجيل شغل جديد**", value="`!تحليل` أو `/تسجيل`\n*يدعم فصلاً واحداً أو عدة فصول، وأنماط أنواع مثل `ترجمة_كوري-تحرير`*", inline=False)
    embed.add_field(name="**▸ عرض أعمالي**", value="`!أعمالي` أو `/أعمالي`", inline=False)
    embed.add_field(name="**▸ عرض شغل عضو**", value="`!شغل @member` أو `/شغل`", inline=False)
    embed.add_field(name="**▸ عرض الأسعار**", value="`!اسعار` أو `/اسعار`", inline=False)
    embed.add_field(name="**▸ تعديل السعر (للمشرفين)**", value="`/تعديل_سعر`", inline=False)
    embed.add_field(name="**▸ حذف (للمشرفين)**", value="`/حذف` (يدعم حذف كل السجلات، أو عمل كامل، أو فصل محدد)", inline=False)
    embed.add_field(name="**▸ حذف كل السجلات (للمشرفين)**", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="**▸ تحديد القنوات (للمشرفين)**", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="**▸ لوحة التحكم (للمشرفين)**", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="**▸ الإحصائيات**", value="`/احصائيات`", inline=False)
    embed.add_field(name="**▸ سجل العمليات (للمشرفين)**", value="`/سجل`", inline=False)
    embed.add_field(name="**▸ تقريري الأسبوعي**", value="`/تقريري`", inline=False)
    embed.add_field(name="**▸ تعديل آخر سجل**", value="`/تعديل`", inline=False)
    embed.add_field(name="**▸ تصدير Excel (للمشرفين)**", value="`/تصدير`", inline=False)
    embed.add_field(name="**▸ إعدادات العملة والإشعارات (للمشرفين)**", value="`/اعدادات`", inline=False)
    embed.add_field(name="**▸ تقرير المشاريع**", value="`/مشاريع`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await interaction.response.send_message(embed=embed)

@bot.command(name="اوامر")
async def help_commands(ctx):
    embed = discord.Embed(title="📌 **أوامر البوت**", color=discord.Color.purple())
    embed.add_field(name="**▸ تسجيل شغل جديد**", value="`!تحليل` أو `/تسجيل`\n*يدعم فصلاً واحداً أو عدة فصول، وأنماط أنواع مثل `ترجمة_كوري-تحرير`*", inline=False)
    embed.add_field(name="**▸ عرض أعمالي**", value="`!أعمالي` أو `/أعمالي`", inline=False)
    embed.add_field(name="**▸ عرض شغل عضو**", value="`!شغل @member` أو `/شغل`", inline=False)
    embed.add_field(name="**▸ عرض الأسعار**", value="`!اسعار` أو `/اسعار`", inline=False)
    embed.add_field(name="**▸ تعديل السعر (للمشرفين)**", value="`/تعديل_سعر`", inline=False)
    embed.add_field(name="**▸ حذف (للمشرفين)**", value="`/حذف` (خيارات متعددة)", inline=False)
    embed.add_field(name="**▸ حذف كل السجلات (للمشرفين)**", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="**▸ تحديد القنوات (للمشرفين)**", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="**▸ لوحة التحكم (للمشرفين)**", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="**▸ الإحصائيات**", value="`/احصائيات`", inline=False)
    embed.add_field(name="**▸ سجل العمليات (للمشرفين)**", value="`/سجل`", inline=False)
    embed.add_field(name="**▸ تقريري الأسبوعي**", value="`/تقريري`", inline=False)
    embed.add_field(name="**▸ تعديل آخر سجل**", value="`/تعديل`", inline=False)
    embed.add_field(name="**▸ تصدير Excel (للمشرفين)**", value="`/تصدير`", inline=False)
    embed.add_field(name="**▸ إعدادات العملة والإشعارات (للمشرفين)**", value="`/اعدادات`", inline=False)
    embed.add_field(name="**▸ تقرير المشاريع**", value="`/مشاريع`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await ctx.send(embed=embed)

# ---------- Slash command: اسعار ----------
@bot.tree.command(name="اسعار", description="عرض أسعار أنواع العمل الحالية")
async def prices_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="💰 **قائمة الأسعار**", color=discord.Color.gold())
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"**{display_name}**", value=f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.command(name="اسعار")
async def prices_text(ctx):
    embed = discord.Embed(title="💰 **قائمة الأسعار**", color=discord.Color.gold())
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"**{display_name}**", value=f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await ctx.send(embed=embed)

# ---------- Slash command: تعديل_سعر ----------
@bot.tree.command(name="تعديل_سعر", description="تعديل سعر نوع عمل (للمشرفين)")
async def edit_price_slash(interaction: discord.Interaction, النوع: str, السعر: float):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    # Normalize type input (remove spaces, lower, replace spaces with underscore)
    norm_type = النوع.strip().replace(' ', '_')
    # Find matching key in PRICES (case-insensitive, with underscores)
    matched = None
    for key in PRICES.keys():
        if key.replace('_', ' ').lower() == norm_type.replace('_', ' ').lower() or key.lower() == norm_type.lower():
            matched = key
            break
    if matched is None:
        await interaction.response.send_message(f"❌ النوع `{النوع}` غير موجود. الأنواع المتاحة: {', '.join(PRICES.keys())}", ephemeral=True)
        return
    PRICES[matched] = السعر
    # Optionally save prices to settings for persistence (optional)
    # We can store prices in settings collection as well
    settings = await load_settings()
    settings["prices"] = PRICES
    await save_settings(settings)
    await log_audit("تعديل_سعر", interaction.user.id, None, f"تغيير سعر {matched} إلى {السعر}")
    await interaction.response.send_message(f"✅ تم تحديث سعر `{matched}` إلى {SETTINGS.get('currency', '$')}{السعر:.2f}", ephemeral=True)

# ---------- Slash command: تسجيل (unified) ----------
class RegisterModal(discord.ui.Modal, title="تسجيل شغل جديد"):
    work_name = discord.ui.TextInput(label="اسم العمل", placeholder="مثال: Solo Leveling", required=True)
    chapters = discord.ui.TextInput(label="الفصول", placeholder="مثال: 5  أو  1-5  أو  1,3,5", required=True)
    types = discord.ui.TextInput(label="الأنواع", placeholder="مثال: ترجمة_كوري  أو  ترجمة_كوري-تحرير-تبييض  أو  ترجمة_كوري-تحرير (الباقي تحرير)", required=True)
    notes = discord.ui.TextInput(label="ملاحظات (اختياري)", placeholder="أي تفاصيل إضافية", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        chapters_list = parse_chapter_range(self.chapters.value)
        if not chapters_list:
            await interaction.response.send_message("❌ نطاق الفصول غير صالح. استخدم مثلاً `5` أو `1-5` أو `1,3,5`.", ephemeral=True)
            return
        
        types_list = parse_mixed_types(self.types.value, len(chapters_list))
        if types_list is None:
            await interaction.response.send_message(f"❌ عدد الأنواع المدخلة لا يتطابق مع عدد الفصول ({len(chapters_list)}).\nاستخدم شرطة `-` للفصل بين الأنواع، مثلاً `ترجمة_كوري-تحرير` (الأول ترجمة والباقي تحرير) أو اكتب نوعاً واحداً للكل.", ephemeral=True)
            return
        
        # Validate types
        for t in types_list:
            if t not in PRICES:
                await interaction.response.send_message(f"❌ النوع `{t}` غير صحيح. الأنواع المسموحة: {', '.join(PRICES.keys())}", ephemeral=True)
                return
        
        records = await load_records()
        user_id = str(interaction.user.id)
        if user_id not in records:
            records[user_id] = []
        
        added = 0
        for idx, ch in enumerate(chapters_list):
            work_type = types_list[idx]
            total = PRICES[work_type]
            records[user_id].append({
                "work_name": self.work_name.value,
                "chapter": ch,
                "work_type": work_type,
                "total": total,
                "notes": self.notes.value or "",
                "timestamp": datetime.utcnow().isoformat()
            })
            added += 1
        
        await save_records(records)
        await update_stats()
        
        embed = discord.Embed(title="✅ **تم حفظ الشغل بنجاح**", color=discord.Color.green())
        embed.add_field(name="**📖 العمل**", value=self.work_name.value, inline=True)
        embed.add_field(name="**🔢 عدد الفصول**", value=str(added), inline=True)
        if len(set(types_list)) == 1:
            embed.add_field(name="**🛠️ النوع**", value=types_list[0], inline=True)
            total_amount = added * PRICES[types_list[0]]
        else:
            total_amount = sum(PRICES[t] for t in types_list)
            types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(chapters_list, types_list)])
            embed.add_field(name="**🛠️ تفاصيل الأنواع**", value=types_summary, inline=False)
        embed.add_field(name="**💰 المبلغ الإجمالي**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=False)
        if self.notes.value:
            embed.add_field(name="**📝 ملاحظات**", value=self.notes.value, inline=False)
        
        await interaction.response.send_message(embed=embed)
        
        # Notify admins if notification channel set
        notify_channel_id = SETTINGS.get("notify_channel_id")
        if notify_channel_id:
            channel = interaction.guild.get_channel(notify_channel_id)
            if channel:
                await channel.send(f"📢 {interaction.user.mention} أضاف {added} فصول في عمل `{self.work_name.value}`")
        
        # Check threshold alert
        total_user_amount = sum(item.get("total",0) for item in records[user_id])
        threshold = SETTINGS.get("alert_threshold", 10.0)
        if total_user_amount >= threshold:
            try:
                await interaction.user.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}. تواصل مع الإدارة لصرف مستحقاتك.")
            except:
                pass

@bot.tree.command(name="تسجيل", description="تسجيل شغل جديد (فصل واحد أو عدة فصول، بنوع واحد أو أنواع مختلفة)")
async def register_slash(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    await interaction.response.send_modal(RegisterModal())

# ---------- Text command: تحليل (supports enhanced types) ----------
@bot.command(name="تحليل")
async def analysis(ctx, *, text=None):
    if not text:
        await ctx.send(
            "**📝 الصيغة:**\n"
            "```text\n"
            "!تحليل\n"
            "العمل: اسم العمل\n"
            "الفصل: رقم الفصل  أو  نطاق الفصول (مثل 1-5)\n"
            "النوع: نوع واحد  أو  أنواع مفصولة بـ - (مثل ترجمة_كوري-تحرير)\n"
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

    chapters_list = parse_chapter_range(chapter_str)
    if not chapters_list:
        await ctx.send("❌ نطاق الفصول غير صالح. استخدم مثلاً `5` أو `1-5` أو `1,3,5`.")
        return

    types_list = parse_mixed_types(types_str, len(chapters_list))
    if types_list is None:
        await ctx.send(f"❌ عدد الأنواع المدخلة لا يتطابق مع عدد الفصول ({len(chapters_list)}).\nاستخدم شرطة `-` للفصل بين الأنواع، مثلاً `ترجمة_كوري-تحرير` (الأول ترجمة والباقي تحرير) أو اكتب نوعاً واحداً للكل.")
        return

    for t in types_list:
        if t not in PRICES:
            await ctx.send(f"❌ النوع `{t}` غير صحيح. الأنواع: {', '.join(PRICES.keys())}")
            return

    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    for idx, ch in enumerate(chapters_list):
        work_type = types_list[idx]
        total = PRICES[work_type]
        records[user_id].append({
            "work_name": work_name,
            "chapter": ch,
            "work_type": work_type,
            "total": total,
            "notes": notes,
            "timestamp": datetime.utcnow().isoformat()
        })
        added += 1

    await save_records(records)
    await update_stats()

    embed = discord.Embed(title="✅ **تم حفظ الشغل بنجاح**", color=discord.Color.green())
    embed.add_field(name="**📖 العمل**", value=work_name, inline=True)
    embed.add_field(name="**🔢 عدد الفصول**", value=str(added), inline=True)
    if len(set(types_list)) == 1:
        embed.add_field(name="**🛠️ النوع**", value=types_list[0], inline=True)
        total_amount = added * PRICES[types_list[0]]
    else:
        total_amount = sum(PRICES[t] for t in types_list)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(chapters_list, types_list)])
        embed.add_field(name="**🛠️ تفاصيل الأنواع**", value=types_summary, inline=False)
    embed.add_field(name="**💰 المبلغ الإجمالي**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=False)
    if notes:
        embed.add_field(name="**📝 ملاحظات**", value=notes, inline=False)

    await ctx.send(embed=embed)

    # Notify admins
    notify_channel_id = SETTINGS.get("notify_channel_id")
    if notify_channel_id:
        channel = ctx.guild.get_channel(notify_channel_id)
        if channel:
            await channel.send(f"📢 {ctx.author.mention} أضاف {added} فصول في عمل `{work_name}`")

    # Threshold check
    total_user_amount = sum(item.get("total",0) for item in records[user_id])
    threshold = SETTINGS.get("alert_threshold", 10.0)
    if total_user_amount >= threshold:
        try:
            await ctx.author.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}. تواصل مع الإدارة لصرف مستحقاتك.")
        except:
            pass

# ---------- Advanced Delete Command (unchanged, but ensure stats update) ----------
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
            await interaction.response.send_message("⚠️ **تحذير:** هل أنت متأكد من حذف كل سجلات هذا العضو؟ هذا الإجراء لا يمكن التراجع عنه.\nأرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
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
            await interaction.response.send_message(f"⚠️ **تحذير:** هل أنت متأكد من حذف كل فصول عمل `{self.work_name}` للعضو؟ أرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
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
                await log_audit("حذف_عمل_كامل", interaction.user.id, self.user_id, f"حذف عمل {self.work_name} (عدد الفصول: {removed_count})")
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
                await interaction2.response.send_message(f"⚠️ هل أنت متأكد من حذف الفصل {chapter} من عمل `{self.work_name}`؟ أرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
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

@bot.tree.command(name="حذف", description="حذف سجلات العضو (كل السجلات، أو عمل كامل، أو فصل محدد) - للمشرفين")
async def delete_advanced(interaction: discord.Interaction, member: discord.Member, work_name: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
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
            options.append(discord.SelectOption(label=f"📖 {w}", value=w, description="اختر هذا العمل لإدارة حذفه"))
        options.append(discord.SelectOption(label="👤 حذف كل سجلات العضو", value="delete_all_user"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        # Ensure we don't exceed 25 options (Discord limit)
        if len(options) > 25:
            options = options[:25]  # truncate
        select = discord.ui.Select(placeholder="اختر عملاً أو خياراً...", options=options)
        async def select_callback(interaction2):
            if select.values[0] == "cancel":
                await interaction2.response.edit_message(content="تم الإلغاء.", view=None)
                return
            if select.values[0] == "delete_all_user":
                await interaction2.response.send_message("⚠️ **تحذير:** هل أنت متأكد من حذف كل سجلات هذا العضو؟ أرسل `تأكيد` خلال 30 ثانية.", ephemeral=True)
                def check(m):
                    return m.author == interaction2.user and m.content == "تأكيد" and m.channel == interaction2.channel
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
                    await interaction2.followup.send("❌ لا توجد سجلات لهذا العضو.", ephemeral=True)
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

# ---------- Keep the original simple delete command ----------
@bot.command(name="حذف")
@commands.has_permissions(manage_messages=True)
async def delete_work_text(ctx, member: discord.Member = None, number: int = None):
    if member is None or number is None:
        await ctx.send("**الاستخدام:** `!حذف @member 2`\nأو استخدم الأمر السحابي `/حذف` للخيارات المتقدمة.")
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

# ---------- Slash and Text command: حذف_الكل ----------
@bot.tree.command(name="حذف_الكل", description="حذف كل السجلات من كل الأعضاء (للمشرفين)")
async def delete_all_work_slash(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    
    records = await load_records()
    total_deleted = sum(len(items) for items in records.values())
    if total_deleted == 0:
        await interaction.response.send_message("📭 ما فيه أي سجلات محفوظة.", ephemeral=True)
        return

    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", interaction.user.id, None, f"عدد السجلات المحذوفة: {total_deleted}")
    await update_stats()

    await interaction.response.send_message(f"🗑️ تم حذف كل السجلات من كل الأعضاء. عدد السجلات المحذوفة: {total_deleted}")

@bot.command(name="حذف_الكل")
@commands.has_permissions(manage_messages=True)
async def delete_all_work_text(ctx):
    records = await load_records()
    total_deleted = sum(len(items) for items in records.values())
    if total_deleted == 0:
        await ctx.send("📭 ما فيه أي سجلات محفوظة.")
        return

    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", ctx.author.id, None, f"عدد السجلات المحذوفة: {total_deleted}")
    await update_stats()

    await ctx.send(f"🗑️ تم حذف كل السجلات من كل الأعضاء. عدد السجلات المحذوفة: {total_deleted}")

# ---------- Helper View for showing work details per project (unchanged) ----------
class WorkDetailsView(discord.ui.View):
    def __init__(self, work_name, chapters_list, user_id, user_name, currency):
        super().__init__(timeout=120)
        self.work_name = work_name
        self.chapters_list = chapters_list
        self.user_id = user_id
        self.user_name = user_name
        self.currency = currency
        self.current_page = 0
        self.items_per_page = 10
        self.total_pages = (len(chapters_list) + self.items_per_page - 1) // self.items_per_page
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.total_pages > 1:
            if self.current_page > 0:
                prev_button = discord.ui.Button(label="◀ السابق", style=discord.ButtonStyle.primary, custom_id="prev")
                prev_button.callback = self.previous_page
                self.add_item(prev_button)
            if self.current_page < self.total_pages - 1:
                next_button = discord.ui.Button(label="التالي ▶", style=discord.ButtonStyle.primary, custom_id="next")
                next_button.callback = self.next_page
                self.add_item(next_button)
        close_button = discord.ui.Button(label="❌ إغلاق", style=discord.ButtonStyle.danger, custom_id="close")
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

# ---------- Projects command with nested buttons, fixed for 25 options limit ----------
class MemberSelect(discord.ui.Select):
    def __init__(self, work_name, members_info, guild):
        self.work_name = work_name
        self.members_info = members_info  # list of (user_id, user_name)
        self.guild = guild
        options = []
        for uid, name in members_info[:25]:
            options.append(discord.SelectOption(label=name, value=str(uid), description=f"عرض فصول {name} في هذا العمل"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        super().__init__(placeholder="اختر عضواً لرؤية تفاصيل فصوله...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "cancel":
            await interaction.response.edit_message(content="تم الإلغاء.", view=None)
            return
        user_id = int(self.values[0])
        user = self.guild.get_member(user_id)
        if not user:
            user_name = next((name for uid, name in self.members_info if uid == user_id), str(user_id))
        else:
            user_name = user.display_name
        records = await load_records()
        user_entries = records.get(str(user_id), [])
        work_entries = [e for e in user_entries if e.get("work_name") == self.work_name]
        if not work_entries:
            await interaction.response.send_message(f"❌ لا توجد فصول للعضو {user_name} في عمل {self.work_name}.", ephemeral=True)
            return
        chapters_details = []
        for e in work_entries:
            chapters_details.append({
                "chapter": e.get("chapter"),
                "type": e.get("work_type"),
                "total": e.get("total", 0),
                "notes": e.get("notes", "")
            })
        view_details = WorkDetailsView(self.work_name, chapters_details, user_id, user_name, SETTINGS.get('currency', '$'))
        await interaction.response.edit_message(content=None, embed=view_details.get_embed(), view=view_details)

class WorkSelect(discord.ui.Select):
    def __init__(self, works_info, guild):
        self.works_info = works_info  # list of (work_name, members_list)
        self.guild = guild
        options = []
        for work_name, _ in works_info[:25]:
            options.append(discord.SelectOption(label=work_name, value=work_name, description="اختر هذا العمل لعرض المساهمين"))
        options.append(discord.SelectOption(label="❌ إلغاء", value="cancel"))
        super().__init__(placeholder="اختر العمل لعرض المساهمين...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "cancel":
            await interaction.response.edit_message(content="تم الإلغاء.", view=None)
            return
        work_name = self.values[0]
        members_info = [(uid, name) for work, members in self.works_info if work == work_name for uid, name in members]
        if not members_info:
            await interaction.response.send_message(f"❌ لا يوجد مساهمين في عمل {work_name}.", ephemeral=True)
            return
        select = MemberSelect(work_name, members_info, self.guild)
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.edit_message(content=f"**اختر عضواً من عمل `{work_name}`:**", view=view)

@bot.tree.command(name="مشاريع", description="عرض جميع المشاريع (الأعمال) والمساهمين وتفاصيل الفصول")
async def projects_report(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return

    records = await load_records()
    work_contributors = defaultdict(lambda: defaultdict(int))
    for user_id_str, entries in records.items():
        for entry in entries:
            work = entry.get("work_name")
            if not work:
                continue
            work_contributors[work][user_id_str] += 1
    
    if not work_contributors:
        await interaction.response.send_message("📭 لا توجد أعمال مسجلة حتى الآن.", ephemeral=True)
        return
    
    works_info = []
    guild = interaction.guild
    for work, contributors in work_contributors.items():
        members_list = []
        for uid_str, count in contributors.items():
            uid = int(uid_str)
            member = guild.get_member(uid)
            name = member.display_name if member else uid_str
            members_list.append((uid, name))
        works_info.append((work, members_list))
    
    # Limit to 25 works for the select menu
    if len(works_info) > 25:
        works_info = works_info[:25]
    
    embed = discord.Embed(title="📚 **قائمة المشاريع (الأعمال)**", color=discord.Color.purple())
    for work, members in works_info:
        embed.add_field(name=f"**▸ {work}**", value=f"عدد المساهمين: {len(members)}", inline=False)
    embed.set_footer(text="اختر عملاً من القائمة المنسدلة لرؤية المساهمين ثم تفاصيل كل عضو.")
    view = discord.ui.View(timeout=120)
    select = WorkSelect(works_info, guild)
    view.add_item(select)
    await interaction.response.send_message(embed=embed, view=view)

# ---------- Enhanced Stats Command ----------
@bot.tree.command(name="احصائيات", description="عرض إحصائيات متقدمة وشاملة")
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
    
    embed = discord.Embed(title="📊 **إحصائيات شاملة للبوت**", color=discord.Color.teal())
    embed.add_field(name="**📄 إجمالي السجلات**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    # Display type counts
    type_lines = "\n".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in type_counts.items()])
    embed.add_field(name="**📊 تفصيل الأنواع**", value=type_lines, inline=False)
    embed.add_field(name="**📅 اليوم**", value=f"سجلات: {daily['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{daily['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الأسبوع**", value=f"سجلات: {weekly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{weekly['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الشهر**", value=f"سجلات: {monthly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{monthly['amount']:.2f}", inline=True)
    if top_members:
        top_list = ""
        for i, (uid, stats_data) in enumerate(top_members[:5], 1):
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else uid
            top_list += f"{i}. **{name}** - {stats_data['total_amount']:.2f} {SETTINGS.get('currency', '$')} ({stats_data['total_entries']} فصل)\n"
        embed.add_field(name="**🏆 أفضل 5 أعضاء**", value=top_list, inline=False)
    embed.set_footer(text=f"آخر تحديث: {last_updated[:19] if last_updated != 'غير معروف' else last_updated}")
    await interaction.response.send_message(embed=embed)

# ---------- Slash and Text command: أعمالي ----------
@bot.tree.command(name="أعمالي", description="عرض أعمالك الخاصة مجمعة حسب اسم العمل")
async def my_works_slash(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("📭 ليس لديك أي شغل مسجل حتى الآن.", ephemeral=True)
        return
    
    works = {}
    for entry in records[user_id]:
        work = entry.get("work_name", "غير محدد")
        if work not in works:
            works[work] = []
        works[work].append(entry)
    
    embed = discord.Embed(title=f"**📚 أعمال {interaction.user.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {k:0 for k in PRICES.keys()}
        for e in entries:
            wtype = e.get("work_type")
            if wtype in types_count:
                types_count[wtype] += 1
            else:
                types_count[wtype] = 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(
            name=f"**▸ {work}**",
            value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}",
            inline=False
        )
    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    view = discord.ui.View(timeout=60)
    for work, entries in list(works.items())[:5]:
        chapters_details = []
        for e in entries:
            chapters_details.append({
                "chapter": e.get("chapter"),
                "type": e.get("work_type"),
                "total": e.get("total", 0),
                "notes": e.get("notes", "")
            })
        button = discord.ui.Button(label=f"📖 {work}", style=discord.ButtonStyle.secondary, custom_id=f"detail_{work}")
        async def button_callback(interaction: discord.Interaction, work_name=work, ch_list=chapters_details):
            view_details = WorkDetailsView(work_name, ch_list, user_id, interaction.user.display_name, SETTINGS.get('currency', '$'))
            await interaction.response.send_message(embed=view_details.get_embed(), view=view_details, ephemeral=True)
        button.callback = button_callback
        view.add_item(button)
    await interaction.response.send_message(embed=embed, view=view)

@bot.command(name="أعمالي")
async def my_works_text(ctx):
    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records or not records[user_id]:
        await ctx.send("📭 ليس لديك أي شغل مسجل حتى الآن.")
        return
    
    works = {}
    for entry in records[user_id]:
        work = entry.get("work_name", "غير محدد")
        if work not in works:
            works[work] = []
        works[work].append(entry)
    
    embed = discord.Embed(title=f"**📚 أعمال {ctx.author.display_name}**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {k:0 for k in PRICES.keys()}
        for e in entries:
            wtype = e.get("work_type")
            if wtype in types_count:
                types_count[wtype] += 1
            else:
                types_count[wtype] = 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(
            name=f"**▸ {work}**",
            value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}",
            inline=False
        )
    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    await ctx.send(embed=embed)
    await ctx.send("🔍 لرؤية تفاصيل كل عمل، استخدم الأمر `/أعمالي` (سحابي) حيث يمكنك الضغط على الأزرار.")

# ---------- Slash command: شغل for any member ----------
@bot.tree.command(name="شغل", description="عرض شغل عضو مجمّع حسب اسم العمل")
async def show_work_slash(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"❌ استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    
    target = member or interaction.user
    records = await load_records()
    user_id = str(target.id)

    if user_id not in records or not records[user_id]:
        await interaction.response.send_message(f"📭 لا يوجد شغل محفوظ للعضو {target.display_name}.", ephemeral=True)
        return

    works = {}
    for entry in records[user_id]:
        work = entry.get("work_name", "غير محدد")
        if work not in works:
            works[work] = []
        works[work].append(entry)
    
    embed = discord.Embed(title=f"**📚 شغل {target.display_name} (مجمّع)**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {k:0 for k in PRICES.keys()}
        for e in entries:
            wtype = e.get("work_type")
            if wtype in types_count:
                types_count[wtype] += 1
            else:
                types_count[wtype] = 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(
            name=f"**▸ {work}**",
            value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}",
            inline=False
        )
    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    
    view = discord.ui.View(timeout=60)
    for work, entries in list(works.items())[:5]:
        chapters_details = []
        for e in entries:
            chapters_details.append({
                "chapter": e.get("chapter"),
                "type": e.get("work_type"),
                "total": e.get("total", 0),
                "notes": e.get("notes", "")
            })
        button = discord.ui.Button(label=f"📖 {work}", style=discord.ButtonStyle.secondary, custom_id=f"detail_{work}")
        async def detail_cb(interaction: discord.Interaction, work_name=work, ch_list=chapters_details):
            view_det = WorkDetailsView(work_name, ch_list, user_id, target.display_name, SETTINGS.get('currency', '$'))
            await interaction.response.send_message(embed=view_det.get_embed(), view=view_det, ephemeral=True)
        button.callback = detail_cb
        view.add_item(button)
    
    await interaction.response.send_message(embed=embed, view=view)

@bot.command(name="شغل")
async def show_work_text(ctx, member: discord.Member = None):
    member = member or ctx.author
    records = await load_records()
    user_id = str(member.id)

    if user_id not in records or not records[user_id]:
        await ctx.send(f"📭 ما عندي أي شغل محفوظ للعضو {member.display_name}.")
        return

    works = {}
    for entry in records[user_id]:
        work = entry.get("work_name", "غير محدد")
        if work not in works:
            works[work] = []
        works[work].append(entry)
    
    embed = discord.Embed(title=f"**📚 شغل {member.display_name} (مجمّع)**", color=discord.Color.blue())
    total_all = 0
    for work, entries in works.items():
        work_total = sum(e.get("total", 0) for e in entries)
        total_all += work_total
        chapters_count = len(entries)
        types_count = {k:0 for k in PRICES.keys()}
        for e in entries:
            wtype = e.get("work_type")
            if wtype in types_count:
                types_count[wtype] += 1
            else:
                types_count[wtype] = 1
        type_str = ", ".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in types_count.items() if v>0])
        embed.add_field(
            name=f"**▸ {work}**",
            value=f"**الفصول:** {chapters_count}\n**التفصيل:** {type_str}\n**المجموع:** {SETTINGS.get('currency', '$')}{work_total:.2f}",
            inline=False
        )
    embed.add_field(name="**💵 الإجمالي العام**", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
    await ctx.send(embed=embed)
    await ctx.send("🔍 لرؤية تفاصيل كل عمل، استخدم الأمر `/شغل` (سحابي) حيث يمكنك الضغط على الأزرار.")

# ---------- Other admin commands ----------
@bot.tree.command(name="لوحة_التحكم", description="لوحة تحكم للمشرفين")
async def dashboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ ما عندك صلاحية.", ephemeral=True)
        return
    records = await load_records()
    total_users = len(records)
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = 0
    for entries in records.values():
        total_amount += sum(entry.get("total", 0) for entry in entries)
    embed = discord.Embed(title="🖥️ **لوحة التحكم**", color=discord.Color.gold())
    embed.add_field(name="**👥 عدد الأعضاء النشطين**", value=total_users, inline=True)
    embed.add_field(name="**📄 عدد السجلات الكلي**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="**⚙️ العملة**", value=SETTINGS.get('currency', '$'), inline=True)
    embed.add_field(name="**🔔 قناة الإشعارات**", value=f"<#{SETTINGS.get('notify_channel_id')}>" if SETTINGS.get('notify_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**💾 قناة النسخ الاحتياطي**", value=f"<#{SETTINGS.get('daily_backup_channel_id')}>" if SETTINGS.get('daily_backup_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**⚠️ حد التنبيه**", value=f"{SETTINGS.get('currency', '$')}{SETTINGS.get('alert_threshold', 10):.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="سجل", description="عرض آخر 20 عملية إدارية (للمشرفين)")
async def audit_log(interaction: discord.Interaction):
    if not is_admin(interaction):
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
async def edit_last(interaction: discord.Interaction, العمل: str = None, الفصل: str = None, النوع: str = None, ملاحظات: str = None):
    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("لا يوجد سجلات لتعديلها.", ephemeral=True)
        return
    last = records[user_id][-1]
    if العمل:
        last["work_name"] = العمل
    if الفصل:
        last["chapter"] = الفصل
    if النوع:
        if النوع not in PRICES:
            await interaction.response.send_message("النوع غير صحيح.", ephemeral=True)
            return
        last["work_type"] = النوع
        last["total"] = PRICES[النوع]
    if ملاحظات is not None:
        last["notes"] = ملاحظات
    await save_records(records)
    await update_stats()
    await interaction.response.send_message("✅ تم تعديل آخر سجل بنجاح.", ephemeral=True)

@bot.tree.command(name="تصدير", description="تصدير كل البيانات إلى ملف Excel (للمشرفين)")
async def export_excel(interaction: discord.Interaction):
    if not is_admin(interaction):
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
async def bot_settings(interaction: discord.Interaction, العملة: str = None, قناة_الإشعارات: discord.TextChannel = None, قناة_النسخ: discord.TextChannel = None, حد_التنبيه: float = None):
    if not is_admin(interaction):
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

# Save prices to settings on startup
async def init_prices():
    settings = await load_settings()
    if "prices" not in settings:
        settings["prices"] = PRICES
        await save_settings(settings)

# نقوم بتعريف دالة الـ hook وتثبيتها داخل البوت
async def custom_setup():
    # هنا الـ loop جاهز تماماً ويمكننا إنشاء المهمة بأمان
    bot.loop.create_task(init_prices())

# نربط الدالة بالبوت قبل التشغيل
bot.setup_hook = custom_setup

# الآن يمكنك التشغيل بأمان
bot.run(TOKEN)