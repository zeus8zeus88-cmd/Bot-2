import os
import json
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
import motor.motor_asyncio

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    raise ValueError("DISCORD_TOKEN is missing from .env file")

MONGO_URI = os.getenv("MONGO_URI")
if MONGO_URI is None:
    raise ValueError("MONGO_URI is missing from .env file")

ALLOWED_CHANNEL_NAME = "تسجيــــــــل-اعمال〢💵"

PRICES = {
    "تحرير": 0.50,
    "ترجمة": 0.50,
    "تبييض": 0.25,
}

# MongoDB setup
print("[LOG] Creating MongoDB client...")
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client["work_bot"]
collection = db["records"]

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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[LOG] Logged in as {bot.user}")
    
    # Test MongoDB connection
    print("[LOG] Testing MongoDB connection...")
    try:
        # Perform a simple ping operation
        await mongo_client.admin.command('ping')
        print("[LOG] MongoDB connection successful! (ping command succeeded)")
    except Exception as e:
        print(f"[ERROR] MongoDB connection failed: {e}")
    
    # Sync slash commands
    print("[LOG] Syncing slash commands...")
    await bot.tree.sync()
    print("[LOG] Slash commands synced")

@bot.check
async def only_allowed_channel(ctx):
    if ctx.channel.name == ALLOWED_CHANNEL_NAME:
        return True
    await ctx.send(f"استخدم أوامر البوت فقط في روم #{ALLOWED_CHANNEL_NAME}.")
    return False

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("ما عندك صلاحية تستخدم هذا الأمر.")
        return
    await ctx.send(f"صار خطأ: `{error}`")

# ---------- Slash command to restore data from JSON file ----------
@bot.tree.command(name="رفع_البيانات", description="رفع ملف records.json لاستعادة البيانات إلى MongoDB")
async def upload_records(interaction: discord.Interaction, file: discord.Attachment):
    # Only allow users with manage_messages permission
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Check file extension
    if not file.filename.endswith('.json'):
        await interaction.followup.send("الملف يجب أن يكون بصيغة JSON.", ephemeral=True)
        return

    try:
        # Read file content
        content = await file.read()
        data = json.loads(content.decode('utf-8'))

        # Validate structure (should be dict with user IDs as keys)
        if not isinstance(data, dict):
            await interaction.followup.send("الملف غير صالح: البيانات الأساسية يجب أن تكون قاموساً (object).", ephemeral=True)
            return

        # Save to MongoDB
        await collection.update_one(
            {"_id": "records"},
            {"$set": {"data": data}},
            upsert=True
        )

        # Count total records
        total_users = len(data)
        total_entries = sum(len(entries) for entries in data.values() if isinstance(entries, list))

        print(f"[LOG] Data restored via slash command: users={total_users}, entries={total_entries}")

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

# ---------- Original text commands (modified to use async load/save) ----------
@bot.command(name="اوامر")
async def help_commands(ctx):
    await ctx.send(
        "**📌 أوامر البوت:**\n\n"
        "**1. تسجيل شغل جديد**\n"
        "`!تحليل`\n"
        "يستخدمه العضو عشان يحفظ شغله.\n\n"
        "الصيغة:\n"
        "```text\n"
        "!تحليل\n"
        "العمل: اسم العمل\n"
        "الفصل: رقم الفصل\n"
        "النوع: ترجمة\n"
        "ملاحظات: اختياري\n"
        "```\n"
        "الأنواع المسموحة:\n"
        "`ترجمة` = $0.50\n"
        "`تحرير` = $0.50\n"
        "`تبييض` = $0.25\n\n"
        "**2. عرض شغلك**\n"
        "`!شغل`\n"
        "يعرض كل الشغل المحفوظ لك مع المجموع.\n\n"
        "**3. عرض شغل عضو**\n"
        "`!شغل @member`\n"
        "يعرض شغل العضو المحدد.\n\n"
        "**4. حذف سجل - للإدارة فقط**\n"
        "`!حذف @member رقم_السجل`\n"
        "يحذف سجل معين من شغل عضو.\n\n"
        "مثال:\n"
        "`!حذف @jamal 2`\n\n"
        "**5. حذف كل السجلات - للإدارة فقط**\n"
        "`!حذف_الكل`\n"
        "يحذف كل السجلات المحفوظة لكل الأعضاء.\n\n"
        f"**ملاحظة:** أوامر البوت تعمل فقط في روم `#{ALLOWED_CHANNEL_NAME}`.\n"
        "رقم السجل يظهر عند استخدام أمر `!شغل @member` مثل `#1` و `#2`."
    )

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
        await ctx.send(
            "فيه بيانات ناقصة. لازم تكتب:\n"
            "`العمل`، `الفصل`، `النوع`"
        )
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
    })

    await save_records(records)

    await ctx.send(
        f"✅ تم حفظ الشغل.\n\n"
        f"📖 العمل: {work_name}\n"
        f"🔢 الفصل: {chapter}\n"
        f"🛠️ النوع: {work_type}\n"
        f"💰 المبلغ: ${total:.2f}"
    )

@bot.command(name="شغل")
async def show_work(ctx, member: discord.Member = None):
    member = member or ctx.author

    records = await load_records()
    user_id = str(member.id)

    if user_id not in records or not records[user_id]:
        await ctx.send("ما عندي أي شغل محفوظ لهذا العضو.")
        return

    result = f"📋 **شغل {member.display_name}:**\n\n"
    grand_total = 0

    for index, item in enumerate(records[user_id], start=1):
        work_type = item.get("work_type", "غير محدد")
        total = item.get("total")

        if total is None:
            total = PRICES.get(work_type, 0)

        grand_total += total

        block = (
            f"**#{index}**\n"
            f"📖 العمل: {item.get('work_name', 'غير محدد')}\n"
            f"🔢 الفصل: {item.get('chapter', 'غير محدد')}\n"
            f"🛠️ النوع: {work_type}\n"
            f"💰 المبلغ: ${total:.2f}\n"
        )

        if item.get("notes"):
            block += f"📝 ملاحظات: {item['notes']}\n"

        block += "\n"

        if len(result) + len(block) > 1900:
            await ctx.send(result)
            result = ""

        result += block

    result += f"──────────────\n💵 **المجموع: ${grand_total:.2f}**"

    await ctx.send(result)

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

    deleted_type = deleted.get("work_type", "غير محدد")
    deleted_total = deleted.get("total")

    if deleted_total is None:
        deleted_total = PRICES.get(deleted_type, 0)

    await ctx.send(
        f"🗑️ تم حذف السجل #{number} من شغل {member.mention}:\n"
        f"📖 {deleted.get('work_name', 'غير محدد')} - "
        f"{deleted_type} - ${deleted_total:.2f}"
    )

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

    await ctx.send(f"🗑️ تم حذف كل السجلات من كل الأعضاء. عدد السجلات المحذوفة: {total_deleted}")

bot.run(TOKEN)