import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO

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
    "ترجمة": 0.50,
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
    """Update daily/weekly/monthly stats"""
    today = datetime.utcnow().date().isoformat()
    week_start = (datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())).date().isoformat()
    month_start = datetime.utcnow().date().replace(day=1).isoformat()
    
    records = await load_records()
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = 0
    type_counts = {"تحرير": 0, "ترجمة": 0, "تبييض": 0}
    
    for user_id, entries in records.items():
        for entry in entries:
            amount = entry.get("total", 0)
            total_amount += amount
            wtype = entry.get("work_type")
            if wtype in type_counts:
                type_counts[wtype] += 1
    
    stat_doc = {
        "date": today,
        "week": week_start,
        "month": month_start,
        "total_entries": total_entries,
        "total_amount": total_amount,
        "type_counts": type_counts
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

def get_color_for_type(work_type):
    colors = {
        "تحرير": discord.Color.blue(),
        "ترجمة": discord.Color.green(),
        "تبييض": discord.Color.orange()
    }
    return colors.get(work_type, discord.Color.default())

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global variable for settings (will be set in on_ready)
SETTINGS = {}

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
    
    # Start daily backup task
    daily_backup.start()
    # Start stats update task (every hour)
    update_stats_task.start()

@bot.check
async def only_allowed_channel(ctx):
    if ctx.channel.name in SETTINGS.get("allowed_channels", []):
        return True
    channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
    await ctx.send(f"استخدم أوامر البوت فقط في أحد الرومات: {channels_str}.")
    return False

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("ما عندك صلاحية تستخدم هذا الأمر.")
        return
    await ctx.send(f"صار خطأ: `{error}`")

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
        await interaction.response.send_message("ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
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
        await interaction.response.send_message("ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if not file.filename.endswith('.json'):
        await interaction.followup.send("الملف يجب أن يكون بصيغة JSON.", ephemeral=True)
        return

    try:
        content = await file.read()
        data = json.loads(content.decode('utf-8'))

        if not isinstance(data, dict):
            await interaction.followup.send("الملف غير صالح: البيانات الأساسية يجب أن تكون قاموساً (object).", ephemeral=True)
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

        await interaction.followup.send(
            f"✅ تم استعادة البيانات بنجاح!\n"
            f"عدد المستخدمين: {total_users}\n"
            f"إجمالي السجلات: {total_entries}",
            ephemeral=True
        )
    except json.JSONDecodeError:
        await interaction.followup.send("الملف ليس بصيغة JSON صحيحة.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] Slash command restore failed: {e}")
        await interaction.followup.send(f"حدث خطأ: {str(e)}", ephemeral=True)

# ---------- Slash and Text command: اوامر (help) ----------
@bot.tree.command(name="اوامر", description="عرض قائمة بجميع أوامر البوت")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(title="📌 أوامر البوت", color=discord.Color.purple())
    embed.add_field(name="تسجيل شغل جديد", value="`!تحليل` أو `/تسجيل`\n*يستخدمه العضو لحفظ شغله.*", inline=False)
    embed.add_field(name="عرض شغلك", value="`!شغل` أو `/شغل`", inline=False)
    embed.add_field(name="عرض شغل عضو", value="`!شغل @member` أو `/شغل member:`", inline=False)
    embed.add_field(name="حذف سجل (للمشرفين)", value="`!حذف @member رقم` أو `/حذف`", inline=False)
    embed.add_field(name="حذف كل السجلات (للمشرفين)", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="تحديد القنوات (للمشرفين)", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="لوحة التحكم (للمشرفين)", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="الإحصائيات", value="`/احصائيات`", inline=False)
    embed.add_field(name="سجل العمليات (للمشرفين)", value="`/سجل`", inline=False)
    embed.add_field(name="تقريري الأسبوعي", value="`/تقريري`", inline=False)
    embed.add_field(name="تعديل آخر سجل", value="`/تعديل`", inline=False)
    embed.add_field(name="تصدير Excel (للمشرفين)", value="`/تصدير`", inline=False)
    embed.add_field(name="إعدادات العملة والإشعارات (للمشرفين)", value="`/اعدادات`", inline=False)
    embed.add_field(name="تقرير المشاريع", value="`/مشاريع`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await interaction.response.send_message(embed=embed)

@bot.command(name="اوامر")
async def help_commands(ctx):
    embed = discord.Embed(title="📌 أوامر البوت", color=discord.Color.purple())
    embed.add_field(name="تسجيل شغل جديد", value="`!تحليل` أو `/تسجيل`", inline=False)
    embed.add_field(name="عرض شغلك", value="`!شغل` أو `/شغل`", inline=False)
    embed.add_field(name="عرض شغل عضو", value="`!شغل @member` أو `/شغل member:`", inline=False)
    embed.add_field(name="حذف سجل (للمشرفين)", value="`!حذف @member رقم` أو `/حذف`", inline=False)
    embed.add_field(name="حذف كل السجلات (للمشرفين)", value="`!حذف_الكل` أو `/حذف_الكل`", inline=False)
    embed.add_field(name="تحديد القنوات (للمشرفين)", value="`!تحديد_قنوات` أو `/تحديد_قنوات`", inline=False)
    embed.add_field(name="لوحة التحكم (للمشرفين)", value="`/لوحة_التحكم`", inline=False)
    embed.add_field(name="الإحصائيات", value="`/احصائيات`", inline=False)
    embed.add_field(name="سجل العمليات (للمشرفين)", value="`/سجل`", inline=False)
    embed.add_field(name="تقريري الأسبوعي", value="`/تقريري`", inline=False)
    embed.add_field(name="تعديل آخر سجل", value="`/تعديل`", inline=False)
    embed.add_field(name="تصدير Excel (للمشرفين)", value="`/تصدير`", inline=False)
    embed.add_field(name="إعدادات العملة والإشعارات (للمشرفين)", value="`/اعدادات`", inline=False)
    embed.add_field(name="تقرير المشاريع", value="`/مشاريع`", inline=False)
    embed.set_footer(text=f"القنوات المسموحة: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
    await ctx.send(embed=embed)

# ---------- Slash command: تسجيل (with modal) ----------
class WorkModal(discord.ui.Modal, title="تسجيل شغل جديد"):
    work_name = discord.ui.TextInput(label="اسم العمل", placeholder="مثال: رواية الألم", required=True)
    chapter = discord.ui.TextInput(label="رقم الفصل", placeholder="مثال: 5", required=True)
    work_type = discord.ui.TextInput(label="النوع", placeholder="تحرير / ترجمة / تبييض", required=True)
    notes = discord.ui.TextInput(label="ملاحظات (اختياري)", placeholder="أي تفاصيل إضافية", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        work_type = self.work_type.value.strip()
        if work_type not in PRICES:
            await interaction.response.send_message("النوع غير صحيح. اختر: تحرير، ترجمة، تبييض", ephemeral=True)
            return
        total = PRICES[work_type]
        records = await load_records()
        user_id = str(interaction.user.id)
        if user_id not in records:
            records[user_id] = []
        records[user_id].append({
            "work_name": self.work_name.value,
            "chapter": self.chapter.value,
            "work_type": work_type,
            "total": total,
            "notes": self.notes.value or "",
            "timestamp": datetime.utcnow().isoformat()
        })
        await save_records(records)
        await update_stats()
        embed = discord.Embed(title="✅ تم حفظ الشغل", color=discord.Color.green())
        embed.add_field(name="📖 العمل", value=self.work_name.value, inline=True)
        embed.add_field(name="🔢 الفصل", value=self.chapter.value, inline=True)
        embed.add_field(name="🛠️ النوع", value=work_type, inline=True)
        embed.add_field(name="💰 المبلغ", value=f"{SETTINGS.get('currency', '$')}{total:.2f}", inline=True)
        if self.notes.value:
            embed.add_field(name="📝 ملاحظات", value=self.notes.value, inline=False)
        await interaction.response.send_message(embed=embed)
        # Notify admins if notification channel set
        notify_channel_id = SETTINGS.get("notify_channel_id")
        if notify_channel_id:
            channel = interaction.guild.get_channel(notify_channel_id)
            if channel:
                await channel.send(f"📢 {interaction.user.mention} أضاف شغل جديد: {self.work_name.value} - فصل {self.chapter.value} ({work_type})")
        # Check threshold alert
        total_user_amount = sum(item.get("total",0) for item in records[user_id])
        threshold = SETTINGS.get("alert_threshold", 10.0)
        if total_user_amount >= threshold:
            try:
                await interaction.user.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}. تواصل مع الإدارة لصرف مستحقاتك.")
            except:
                pass

@bot.tree.command(name="تسجيل", description="تسجيل شغل جديد باستخدام نموذج")
async def register_slash(interaction: discord.Interaction):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    await interaction.response.send_modal(WorkModal())

@bot.command(name="تحليل")
async def analysis(ctx, *, text=None):
    if not text:
        await ctx.send(
            "اكتبها كذا في رسالة واحدة:\n\n"
            "```text\n"
            "!تحليل\n"
            "العمل: اسم العمل\n"
            "الفصل: رقم الفصل\n"
            "النوع: تحرير\n"
            "ملاحظات: اختياري\n"
            "```\n"
            "الأنواع: تحرير، ترجمة، تبييض"
        )
        return

    fields = parse_fields(text)
    work_name = fields.get("العمل") or fields.get("اسم العمل")
    chapter = fields.get("الفصل") or fields.get("رقم الفصل")
    work_type = fields.get("النوع") or fields.get("الشغل")
    notes = fields.get("ملاحظات", "")

    if not work_name or not chapter or not work_type:
        await ctx.send("فيه بيانات ناقصة. لازم تكتب: `العمل`، `الفصل`، `النوع`")
        return

    work_type = work_type.strip()
    if work_type not in PRICES:
        await ctx.send("النوع لازم يكون واحد من: تحرير، ترجمة، تبييض")
        return

    total = PRICES[work_type]
    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records:
        records[user_id] = []
    records[user_id].append({
        "work_name": work_name,
        "chapter": chapter,
        "work_type": work_type,
        "total": total,
        "notes": notes,
        "timestamp": datetime.utcnow().isoformat()
    })
    await save_records(records)
    await update_stats()
    embed = discord.Embed(title="✅ تم حفظ الشغل", color=discord.Color.green())
    embed.add_field(name="📖 العمل", value=work_name, inline=True)
    embed.add_field(name="🔢 الفصل", value=chapter, inline=True)
    embed.add_field(name="🛠️ النوع", value=work_type, inline=True)
    embed.add_field(name="💰 المبلغ", value=f"{SETTINGS.get('currency', '$')}{total:.2f}", inline=True)
    if notes:
        embed.add_field(name="📝 ملاحظات", value=notes, inline=False)
    await ctx.send(embed=embed)
    # Notify admins
    notify_channel_id = SETTINGS.get("notify_channel_id")
    if notify_channel_id:
        channel = ctx.guild.get_channel(notify_channel_id)
        if channel:
            await channel.send(f"📢 {ctx.author.mention} أضاف شغل جديد: {work_name} - فصل {chapter} ({work_type})")
    # Threshold check
    total_user_amount = sum(item.get("total",0) for item in records[user_id])
    threshold = SETTINGS.get("alert_threshold", 10.0)
    if total_user_amount >= threshold:
        try:
            await ctx.author.send(f"🔔 تنبيه: إجمالي شغلك وصل إلى {SETTINGS.get('currency', '$')}{total_user_amount:.2f}. تواصل مع الإدارة لصرف مستحقاتك.")
        except:
            pass

# ---------- Slash and Text command: شغل (with buttons for admin delete) ----------
class WorkView(discord.ui.View):
    def __init__(self, user_id, records):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.records = records
        # Add delete buttons for each record (only if admin, but we check in callback)
        for i in range(len(records)):
            button = discord.ui.Button(label=f"حذف #{i+1}", style=discord.ButtonStyle.danger, custom_id=f"delete_{i}")
            button.callback = self.create_delete_callback(i)
            self.add_item(button)

    def create_delete_callback(self, index):
        async def callback(interaction: discord.Interaction):
            if not is_admin(interaction):
                await interaction.response.send_message("ما عندك صلاحية للحذف.", ephemeral=True)
                return
            records = await load_records()
            user_id_str = str(self.user_id)
            if user_id_str not in records or index >= len(records[user_id_str]):
                await interaction.response.send_message("السجل غير موجود.", ephemeral=True)
                return
            deleted = records[user_id_str].pop(index)
            await save_records(records)
            await log_audit("حذف سجل (زر)", interaction.user.id, self.user_id, f"السجل #{index+1}: {deleted.get('work_name')}")
            await interaction.response.send_message(f"🗑️ تم حذف السجل #{index+1}.", ephemeral=True)
            # Refresh the message
            await interaction.edit_original_response(content="تم التحديث، استخدم الأمر مرة أخرى لعرض القائمة الجديدة.", view=None)
        return callback

@bot.tree.command(name="شغل", description="عرض شغل عضو (نفسك أو عضو آخر)")
async def show_work_slash(interaction: discord.Interaction, member: discord.Member = None):
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    
    target = member or interaction.user
    records = await load_records()
    user_id = str(target.id)

    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("ما عندي أي شغل محفوظ لهذا العضو.", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 شغل {target.display_name}", color=discord.Color.blue())
    grand_total = 0
    for index, item in enumerate(records[user_id], start=1):
        work_type = item.get("work_type", "غير محدد")
        total = item.get("total", PRICES.get(work_type, 0))
        grand_total += total
        embed.add_field(
            name=f"#{index}",
            value=f"📖 العمل: {item.get('work_name', 'غير محدد')}\n🔢 الفصل: {item.get('chapter', 'غير محدد')}\n🛠️ النوع: {work_type}\n💰 المبلغ: {SETTINGS.get('currency', '$')}{total:.2f}\n📝 ملاحظات: {item.get('notes', 'لا توجد')}",
            inline=False
        )
    embed.set_footer(text=f"المجموع: {SETTINGS.get('currency', '$')}{grand_total:.2f}")
    view = WorkView(target.id, records[user_id]) if is_admin(interaction) else None
    await interaction.response.send_message(embed=embed, view=view)

@bot.command(name="شغل")
async def show_work(ctx, member: discord.Member = None):
    member = member or ctx.author
    records = await load_records()
    user_id = str(member.id)

    if user_id not in records or not records[user_id]:
        await ctx.send("ما عندي أي شغل محفوظ لهذا العضو.")
        return

    embed = discord.Embed(title=f"📋 شغل {member.display_name}", color=discord.Color.blue())
    grand_total = 0
    for index, item in enumerate(records[user_id], start=1):
        work_type = item.get("work_type", "غير محدد")
        total = item.get("total", PRICES.get(work_type, 0))
        grand_total += total
        embed.add_field(
            name=f"#{index}",
            value=f"📖 العمل: {item.get('work_name', 'غير محدد')}\n🔢 الفصل: {item.get('chapter', 'غير محدد')}\n🛠️ النوع: {work_type}\n💰 المبلغ: {SETTINGS.get('currency', '$')}{total:.2f}\n📝 ملاحظات: {item.get('notes', 'لا توجد')}",
            inline=False
        )
    embed.set_footer(text=f"المجموع: {SETTINGS.get('currency', '$')}{grand_total:.2f}")
    await ctx.send(embed=embed)

# ---------- Slash and Text command: حذف ----------
@bot.tree.command(name="حذف", description="حذف سجل معين من شغل عضو (للمشرفين)")
async def delete_work_slash(interaction: discord.Interaction, member: discord.Member, number: int):
    if not is_admin(interaction):
        await interaction.response.send_message("ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    
    records = await load_records()
    user_id = str(member.id)
    if user_id not in records or not records[user_id]:
        await interaction.response.send_message("هذا العضو ما عنده أي شغل محفوظ.", ephemeral=True)
        return
    if number < 1 or number > len(records[user_id]):
        await interaction.response.send_message("رقم السجل غير صحيح.", ephemeral=True)
        return

    deleted = records[user_id].pop(number - 1)
    await save_records(records)
    await log_audit("حذف سجل", interaction.user.id, member.id, f"السجل #{number}: {deleted.get('work_name')}")

    embed = discord.Embed(title="🗑️ تم حذف السجل", color=discord.Color.red())
    embed.add_field(name="المستخدم", value=member.mention, inline=True)
    embed.add_field(name="العمل", value=deleted.get('work_name', 'غير محدد'), inline=True)
    embed.add_field(name="النوع", value=deleted.get('work_type', 'غير محدد'), inline=True)
    embed.add_field(name="المبلغ", value=f"{SETTINGS.get('currency', '$')}{deleted.get('total', 0):.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.command(name="حذف")
@commands.has_permissions(manage_messages=True)
async def delete_work(ctx, member: discord.Member = None, number: int = None):
    if member is None or number is None:
        await ctx.send("الاستخدام: `!حذف @member 2`")
        return

    records = await load_records()
    user_id = str(member.id)

    if user_id not in records or not records[user_id]:
        await ctx.send("هذا العضو ما عنده أي شغل محفوظ.")
        return
    if number < 1 or number > len(records[user_id]):
        await ctx.send("رقم السجل غير صحيح.")
        return

    deleted = records[user_id].pop(number - 1)
    await save_records(records)
    await log_audit("حذف سجل (نصي)", ctx.author.id, member.id, f"السجل #{number}: {deleted.get('work_name')}")

    embed = discord.Embed(title="🗑️ تم حذف السجل", color=discord.Color.red())
    embed.add_field(name="المستخدم", value=member.mention, inline=True)
    embed.add_field(name="العمل", value=deleted.get('work_name', 'غير محدد'), inline=True)
    embed.add_field(name="النوع", value=deleted.get('work_type', 'غير محدد'), inline=True)
    embed.add_field(name="المبلغ", value=f"{SETTINGS.get('currency', '$')}{deleted.get('total', 0):.2f}", inline=True)
    await ctx.send(embed=embed)

# ---------- Slash and Text command: حذف_الكل ----------
@bot.tree.command(name="حذف_الكل", description="حذف كل السجلات من كل الأعضاء (للمشرفين)")
async def delete_all_work_slash(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    if interaction.channel.name not in SETTINGS.get("allowed_channels", []):
        channels_str = ", ".join([f"#{ch}" for ch in SETTINGS.get("allowed_channels", [])])
        await interaction.response.send_message(f"استخدم هذا الأمر فقط في أحد الرومات: {channels_str}.", ephemeral=True)
        return
    
    records = await load_records()
    total_deleted = sum(len(items) for items in records.values())
    if total_deleted == 0:
        await interaction.response.send_message("ما فيه أي سجلات محفوظة.", ephemeral=True)
        return

    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", interaction.user.id, None, f"عدد السجلات المحذوفة: {total_deleted}")

    await interaction.response.send_message(f"🗑️ تم حذف كل السجلات من كل الأعضاء. عدد السجلات المحذوفة: {total_deleted}")

@bot.command(name="حذف_الكل")
@commands.has_permissions(manage_messages=True)
async def delete_all_work(ctx):
    records = await load_records()
    total_deleted = sum(len(items) for items in records.values())
    if total_deleted == 0:
        await ctx.send("ما فيه أي سجلات محفوظة.")
        return

    records.clear()
    await save_records(records)
    await log_audit("حذف_الكل", ctx.author.id, None, f"عدد السجلات المحذوفة: {total_deleted}")

    await ctx.send(f"🗑️ تم حذف كل السجلات من كل الأعضاء. عدد السجلات المحذوفة: {total_deleted}")

# ---------- New commands: Dashboard, Stats, Audit Log, Weekly Report, Edit Last, Export Excel, Settings, Projects, Currency ----------
@bot.tree.command(name="لوحة_التحكم", description="لوحة تحكم للمشرفين")
async def dashboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ما عندك صلاحية.", ephemeral=True)
        return
    records = await load_records()
    total_users = len(records)
    total_entries = sum(len(entries) for entries in records.values())
    total_amount = 0
    for entries in records.values():
        total_amount += sum(entry.get("total", 0) for entry in entries)
    embed = discord.Embed(title="🖥️ لوحة التحكم", color=discord.Color.gold())
    embed.add_field(name="👥 عدد الأعضاء النشطين", value=total_users, inline=True)
    embed.add_field(name="📄 عدد السجلات الكلي", value=total_entries, inline=True)
    embed.add_field(name="💰 إجمالي المبالغ", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="⚙️ العملة", value=SETTINGS.get('currency', '$'), inline=True)
    embed.add_field(name="🔔 قناة الإشعارات", value=f"<#{SETTINGS.get('notify_channel_id')}>" if SETTINGS.get('notify_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="💾 قناة النسخ الاحتياطي", value=f"<#{SETTINGS.get('daily_backup_channel_id')}>" if SETTINGS.get('daily_backup_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="⚠️ حد التنبيه", value=f"{SETTINGS.get('currency', '$')}{SETTINGS.get('alert_threshold', 10):.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="احصائيات", description="عرض إحصائيات متقدمة")
async def stats(interaction: discord.Interaction):
    stat_doc = await stats_collection.find_one({"_id": "stats"})
    if not stat_doc:
        await interaction.response.send_message("لا توجد إحصائيات بعد.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 إحصائيات البوت", color=discord.Color.teal())
    embed.add_field(name="إجمالي السجلات", value=stat_doc.get("total_entries", 0), inline=True)
    embed.add_field(name="إجمالي المبالغ", value=f"{SETTINGS.get('currency', '$')}{stat_doc.get('total_amount', 0):.2f}", inline=True)
    types = stat_doc.get("type_counts", {})
    embed.add_field(name="📖 تحرير", value=types.get("تحرير", 0), inline=True)
    embed.add_field(name="🌐 ترجمة", value=types.get("ترجمة", 0), inline=True)
    embed.add_field(name="✨ تبييض", value=types.get("تبييض", 0), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="سجل", description="عرض آخر 20 عملية إدارية (للمشرفين)")
async def audit_log(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ما عندك صلاحية.", ephemeral=True)
        return
    logs = await audit_collection.find().sort("timestamp", -1).limit(20).to_list(length=20)
    if not logs:
        await interaction.response.send_message("لا توجد سجلات.", ephemeral=True)
        return
    embed = discord.Embed(title="📜 سجل العمليات", color=discord.Color.dark_gray())
    for log in logs:
        embed.add_field(
            name=log.get("action", "غير معروف"),
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
    embed = discord.Embed(title=f"📅 تقريرك الأسبوعي", color=discord.Color.green())
    embed.add_field(name="عدد المهام", value=len(week_entries), inline=True)
    embed.add_field(name="المجموع", value=f"{SETTINGS.get('currency', '$')}{total:.2f}", inline=True)
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
    await interaction.response.send_message("✅ تم تعديل آخر سجل بنجاح.", ephemeral=True)

@bot.tree.command(name="تصدير", description="تصدير كل البيانات إلى ملف Excel (للمشرفين)")
async def export_excel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("ما عندك صلاحية.", ephemeral=True)
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
        await interaction.response.send_message("ما عندك صلاحية.", ephemeral=True)
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

@bot.tree.command(name="مشاريع", description="عرض تقرير المشاريع (الروايات/المانجا)")
async def projects_report(interaction: discord.Interaction):
    records = await load_records()
    projects = {}
    for user_id, entries in records.items():
        for entry in entries:
            work = entry.get("work_name")
            if not work:
                continue
            if work not in projects:
                projects[work] = {"total_pages": 0, "contributors": set()}
            projects[work]["total_pages"] += 1
            projects[work]["contributors"].add(user_id)
    if not projects:
        await interaction.response.send_message("لا توجد مشاريع مسجلة.")
        return
    embed = discord.Embed(title="📚 تقرير المشاريع", color=discord.Color.purple())
    for work, data in list(projects.items())[:10]:
        embed.add_field(name=work, value=f"عدد الفصول: {data['total_pages']}\nعدد المساهمين: {len(data['contributors'])}", inline=False)
    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)