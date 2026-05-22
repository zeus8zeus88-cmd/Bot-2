from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands
from state import bot
from helpers.core import *
from tasks.lifecycle import work_autocomplete
@bot.tree.command(name="تسجيل", description="تسجيل شغل جديد (يدعم الفلترة حسب الأعمال المدفوعة)")
@app_commands.autocomplete(العمل=work_autocomplete)
@app_commands.describe(
    العمل="اسم العمل (اختر من القائمة)",
    الفصول="نطاق الفصول مثل 1-5 أو 1,3,5",
    التخصصات="التخصصات مثل ترجمة كوري-تحرير-تبييض (بدون شرطة سفلية)",
    ملاحظات="ملاحظات اختيارية"
)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def register_slash(interaction: discord.Interaction, العمل: str, الفصول: str, التخصصات: str, ملاحظات: str = None):
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

    # تحليل التخصصات مع إمكانية استخدام المسافات بدلاً من الشَرطات السفلية
    original_types = parse_mixed_types(التخصصات, len(chapters_list))
    if original_types is None:
        await interaction.response.send_message(f"❌ عدد التخصصات لا يتطابق مع عدد الفصول ({len(chapters_list)}).", ephemeral=True)
        return

    mapped_types = [map_type(t) for t in original_types]

    # فلترة التخصصات للفصول المدفوعة
    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    # التحقق من صحة التخصصات
    for t in filtered_types:
        if t not in PRICES:
            await interaction.response.send_message(f"❌ التخصص `{t}` غير صحيح. التخصصات المتاحة: {', '.join(PRICES.keys())}", ephemeral=True)
            return

    records = await load_records()
    user_id = str(interaction.user.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = interaction.user.name
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        # التحقق من عدم التكرار
        if is_duplicate(records, user_id, العمل, ch, work_type):
            continue  # تجاهل الفصل المكرر
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

    if added == 0:
        await interaction.response.send_message("⚠️ لم يتم إضافة أي فصل جديد (جميع الفصول إما مكررة أو مجانية).", ephemeral=True)
        return

    await save_records(records)
    await update_stats()

    embed = make_embed("finance", "🧾 إيصال تسجيل العمل", "تمت معالجة عملية التسجيل بنجاح.", interaction, interaction.user)
    embed.add_field(name="👤 العضو", value=interaction.user.mention, inline=True)
    embed.add_field(name="📖 العمل", value=العمل, inline=True)
    embed.add_field(name="✅ الفصول المسجلة", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية تم تجاهلها", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ التخصص**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل التخصصات**", value=types_summary, inline=False)
    embed.add_field(name="💰 الإجمالي", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="📅 تاريخ العملية", value=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    embed.add_field(name="📌 الحالة", value="مكتمل", inline=True)
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
    التخصصات="التخصصات مثل ترجمة كوري-تحرير-تبييض",
    ملاحظات="ملاحظات اختيارية"
)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def register_for_member(
    interaction: discord.Interaction,
    عضو: discord.Member,
    العمل: str,
    الفصول: str,
    التخصصات: str,
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

    # تحليل التخصصات
    types_list = parse_mixed_types(التخصصات, len(chapters_list))
    if types_list is None:
        await interaction.response.send_message(f"❌ عدد التخصصات لا يتطابق مع عدد الفصول ({len(chapters_list)}).", ephemeral=True)
        return

    # تعيين التخصصات إلى المفاتيح الفعلية
    mapped_types = [map_type(t) for t in types_list]

    # مطابقة التخصصات للفصول المدفوعة فقط
    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    # التحقق من صحة التخصصات
    for t in filtered_types:
        if t not in PRICES:
            await interaction.response.send_message(f"❌ التخصص `{t}` غير صحيح. التخصصات المسموحة: {', '.join(PRICES.keys())}", ephemeral=True)
            return

    # جلب السجلات وحفظها
    records = await load_records()
    user_id = str(عضو.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = عضو.name
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        if is_duplicate(records, user_id, العمل, ch, work_type):
            continue
        total = PRICES[work_type]
        records[user_id].append({
            "work_name": العمل,
            "chapter": ch,
            "work_type": work_type,
            "total": total,
            "notes": ملاحظات or "",
            "timestamp": datetime.utcnow().isoformat(),
            "username": username,
            "added_by": str(interaction.user.id)
        })
        added += 1

    if added == 0:
        await interaction.response.send_message("⚠️ لم يتم إضافة أي فصل جديد (جميع الفصول مكررة).", ephemeral=True)
        return

    await save_records(records)
    await update_stats()

    # بناء التضمين
    embed = make_embed("finance", "🧾 إيصال تسجيل العمل", "تمت معالجة عملية التسجيل بنجاح.", interaction, interaction.user)
    embed.add_field(name="**👤 العضو**", value=عضو.mention, inline=True)
    embed.add_field(name="👤 العضو", value=interaction.user.mention, inline=True)
    embed.add_field(name="📖 العمل", value=العمل, inline=True)
    embed.add_field(name="✅ الفصول المسجلة", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية تم تجاهلها", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ التخصص**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل التخصصات**", value=types_summary, inline=False)
    embed.add_field(name="💰 الإجمالي", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="📅 تاريخ العملية", value=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    embed.add_field(name="📌 الحالة", value="مكتمل", inline=True)
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
                    f"أضاف {added} فصل لـ {العمل} (التخصصات: {','.join(filtered_types)})")

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
            "التخصص: تخصص واحد  أو  تخصصات مفصولة بـ - (مثل ترجمة كوري-تحرير)\n"
            "ملاحظات: اختياري\n"
            "```\n"
            "**التخصصات المتاحة:** " + "، ".join(PRICES.keys())
        )
        return

    fields = parse_fields(text)
    work_name = fields.get("العمل") or fields.get("اسم العمل")
    chapter_str = fields.get("الفصل") or fields.get("رقم الفصل")
    types_str = fields.get("التخصص") or fields.get("الشغل")
    notes = fields.get("ملاحظات", "")

    if not work_name or not chapter_str or not types_str:
        await ctx.send("❌ فيه بيانات ناقصة. لازم تكتب: `العمل`، `الفصل`، `التخصص`")
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
        await ctx.send(f"❌ عدد التخصصات لا يتطابق مع عدد الفصول ({len(chapters_list)}).")
        return

    mapped_types = [map_type(t) for t in original_types]

    filtered_types = []
    kept_set = set(paid_chapters)
    for idx, ch in enumerate(chapters_list):
        if ch in kept_set:
            filtered_types.append(mapped_types[idx])

    for t in filtered_types:
        if t not in PRICES:
            await ctx.send(f"❌ التخصص `{t}` غير صحيح. التخصصات: {', '.join(PRICES.keys())}")
            return

    records = await load_records()
    user_id = str(ctx.author.id)
    if user_id not in records:
        records[user_id] = []

    added = 0
    username = ctx.author.name
    for idx, ch in enumerate(paid_chapters):
        work_type = filtered_types[idx]
        if is_duplicate(records, user_id, work_name, ch, work_type):
            continue
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

    if added == 0:
        await ctx.send("⚠️ لم يتم إضافة أي فصل جديد (جميع الفصول مكررة).")
        return

    await save_records(records)
    await update_stats()

    embed = make_embed("finance", "🧾 إيصال تسجيل العمل", "تمت معالجة عملية التسجيل بنجاح.", interaction, interaction.user)
    embed.add_field(name="**📖 العمل**", value=work_name, inline=True)
    embed.add_field(name="✅ الفصول المسجلة", value=str(added), inline=True)
    if free_count > 0:
        embed.add_field(name="⏭️ فصول مجانية تم تجاهلها", value=str(free_count), inline=True)
    if len(set(filtered_types)) == 1:
        embed.add_field(name="**🛠️ التخصص**", value=filtered_types[0], inline=True)
        total_amount = added * PRICES[filtered_types[0]]
    else:
        total_amount = sum(PRICES[t] for t in filtered_types)
        types_summary = "\n".join([f"فصل {ch}: {t}" for ch, t in zip(paid_chapters, filtered_types)])
        embed.add_field(name="**🛠️ تفاصيل التخصصات**", value=types_summary, inline=False)
    embed.add_field(name="💰 الإجمالي", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="📅 تاريخ العملية", value=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    embed.add_field(name="📌 الحالة", value="مكتمل", inline=True)
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
