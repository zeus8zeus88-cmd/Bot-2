from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands
from state import bot
from helpers.core import *
from helpers.core import make_embed
from views.paginators import WorkDetailsView, WorksPaginator, get_works_info
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
    embed.add_field(name="**📄 إجمالي الفصول**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    type_lines = "\n".join([f"**{k.replace('_',' ').title()}:** {v}" for k,v in type_counts.items()])
    embed.add_field(name="**📊 تفصيل التخصصات**", value=type_lines, inline=False)
    embed.add_field(name="**📅 اليوم**", value=f"فصول: {daily['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{daily['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الأسبوع**", value=f"فصول: {weekly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{weekly['amount']:.2f}", inline=True)
    embed.add_field(name="**📆 هذا الشهر**", value=f"فصول: {monthly['entries']}\nالمبلغ: {SETTINGS.get('currency', '$')}{monthly['amount']:.2f}", inline=True)
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

    embed = make_embed("finance", f"💼 اللوحة الشخصية • {interaction.user.display_name}", "ملخص مالي وحسابي لأعمالك.", interaction, interaction.user)
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

    embed.add_field(name="💵 الصافي النهائي", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)

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

    embed.add_field(name="💵 الصافي النهائي", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
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

    embed.add_field(name="💵 الصافي النهائي", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)

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

    embed.add_field(name="💵 الصافي النهائي", value=f"{SETTINGS.get('currency', '$')}{total_all:.2f}", inline=False)
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
    embed = make_embed("admin", "🖥️ لوحة التحكم الرئيسية", "مركز إدارة شامل للمشرفين.", interaction, interaction.user)
    embed.add_field(name="**👥 عدد الأعضاء النشطين**", value=total_users, inline=True)
    embed.add_field(name="**📄 عدد السجلات الكلي**", value=total_entries, inline=True)
    embed.add_field(name="**💰 إجمالي المبالغ**", value=f"{SETTINGS.get('currency', '$')}{total_amount:.2f}", inline=True)
    embed.add_field(name="**⚙️ العملة**", value=SETTINGS.get('currency', '$'), inline=True)
    embed.add_field(name="**🔔 قناة الإشعارات**", value=f"<#{SETTINGS.get('notify_channel_id')}>" if SETTINGS.get('notify_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**💾 قناة النسخ الاحتياطي**", value=f"<#{SETTINGS.get('daily_backup_channel_id')}>" if SETTINGS.get('daily_backup_channel_id') else "غير محدد", inline=True)
    embed.add_field(name="**⚠️ حد التنبيه**", value=f"{SETTINGS.get('currency', '$')}{SETTINGS.get('alert_threshold', 10):.2f}", inline=True)
    payment_day = SETTINGS.get("payment_day")
    embed.add_field(name="**📅 موعد الدفع الشهري**", value=f"يوم {payment_day} الساعة {SETTINGS.get('payment_hour', 0)}" if payment_day else "غير محدد", inline=True)
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
        await interaction.response.send_message("لا يوجد فصول خلال الأسبوع الماضي.", ephemeral=True)
        return
    total = sum(e.get("total", 0) for e in week_entries)
    embed = discord.Embed(title="📅 **تقريرك الأسبوعي**", color=discord.Color.green())
    embed.add_field(name="**عدد المهام**", value=len(week_entries), inline=True)
    embed.add_field(name="**المجموع**", value=f"{SETTINGS.get('currency', '$')}{total:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="تعديل", description="تعديل آخر سجل قمت بإضافته")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def edit_last(interaction: discord.Interaction, العمل: str = None, الفصل: str = None, التخصص: str = None, ملاحظات: str = None):
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
    if التخصص:
        norm_type = map_type(التخصص)
        if norm_type not in PRICES:
            await interaction.response.send_message("التخصص غير صحيح.", ephemeral=True)
            return
        last["work_type"] = norm_type
        last["total"] = PRICES[norm_type]
    if ملاحظات is not None:
        last["notes"] = ملاحظات
    await save_records(records)
    await update_stats()
    await interaction.response.send_message("✅ تم تعديل آخر سجل بنجاح.", ephemeral=True)
