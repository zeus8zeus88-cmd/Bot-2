import discord
from datetime import datetime
from discord import app_commands
from discord.ext import commands
from state import bot
from helpers.core import SETTINGS, save_settings, load_settings, rebuild_prices, PRICES, make_embed
from tasks.lifecycle import specialty_autocomplete

HELP_PAGES = [
    ("👤 أوامر الأعضاء", ["`/تسجيل` تسجيل عمل", "`/أعمالي` لوحة شخصية", "`/تقريري` تقرير أسبوعي", "`/ملخص_شهري` ملخص الشهر", "`/اسعار` عرض الأسعار"]),
    ("🧾 أوامر التسجيل", ["`/تسجيل`", "`/تسجيل_للغير`", "`/تعديل` تعديل آخر سجل"]),
    ("📊 أوامر التقارير", ["`/احصائيات`", "`/الأعمال`", "`/شغل`", "`/تصدير`"]),
    ("🛠️ الإدارة العامة", ["`/لوحة_التحكم`", "`/سجل`", "`/تحديد_قنوات`", "`/اعدادات`", "`/رفع_البيانات`"]),
    ("💼 الأعمال/التخصصات/الدفع", ["`/اضافة_عمل` `/حذف_عمل` `/تعديل_عمل`", "`/اضافة_تخصص` `/تعطيل_تخصص` `/تفعيل_تخصص`", "`/تعديل_سعر` `/تحديث_أسعار`", "`/تحديد_موعد_الدفع` `/تقرير_دفع`"]),
]

class HelpPager(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.idx = 0

    def build(self, interaction=None):
        title, rows = HELP_PAGES[self.idx]
        emb = make_embed("info", f"📘 مركز المساعدة • {title}", "استخدم التالي/السابق للتنقل بين الصفحات.", interaction)
        emb.add_field(name="الأوامر", value="\n".join([f"• {r}" for r in rows]), inline=False)
        emb.set_footer(text=f"صفحة {self.idx+1}/{len(HELP_PAGES)} • القنوات: {', '.join([f'#{ch}' for ch in SETTINGS.get('allowed_channels', [])])}")
        return emb

    @discord.ui.button(label="◀ السابق", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.idx = (self.idx - 1) % len(HELP_PAGES)
        await interaction.response.edit_message(embed=self.build(interaction), view=self)

    @discord.ui.button(label="التالي ▶", style=discord.ButtonStyle.primary)
    async def nxt(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.idx = (self.idx + 1) % len(HELP_PAGES)
        await interaction.response.edit_message(embed=self.build(interaction), view=self)

@bot.tree.command(name="اوامر", description="عرض قائمة أوامر البوت مقسمة على صفحات")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def help_slash(interaction: discord.Interaction):
    v = HelpPager()
    await interaction.response.send_message(embed=v.build(interaction), view=v, ephemeral=True)

@bot.command(name="اوامر")
@commands.cooldown(1, 5, commands.BucketType.user)
async def help_commands(ctx):
    v = HelpPager()
    await ctx.send(embed=v.build(), view=v)

@bot.tree.command(name="اسعار", description="عرض أسعار التخصصات الحالية")
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def prices_slash(interaction: discord.Interaction):
    embed = make_embed("finance", "💰 قائمة الأسعار", "الأسعار الحالية للتخصصات.", interaction)
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"{display_name}", value=f"{SETTINGS.get('currency', '$')}{price:.3f}" if t == "رفع" else f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.command(name="اسعار")
@commands.cooldown(1, 5, commands.BucketType.user)
async def prices_text(ctx):
    embed = make_embed("finance", "💰 قائمة الأسعار", "الأسعار الحالية للتخصصات.")
    for t, price in PRICES.items():
        display_name = t.replace('_', ' ').title()
        embed.add_field(name=f"{display_name}", value=f"{SETTINGS.get('currency', '$')}{price:.3f}" if t == "رفع" else f"{SETTINGS.get('currency', '$')}{price:.2f}", inline=True)
    await ctx.send(embed=embed)

@bot.tree.command(name="تعديل_سعر", description="تعديل سعر تخصص (للمشرفين)")
@app_commands.autocomplete(التخصص=specialty_autocomplete)
@app_commands.checks.cooldown(1, 5, key=lambda i: (i.user.id, i.command.qualified_name))
async def edit_price_slash(interaction: discord.Interaction, التخصص: str, السعر: float):
    from helpers.core import is_admin, log_unauthorized, map_type, log_audit
    if not is_admin(interaction):
        await log_unauthorized(interaction.user.id, "تعديل_سعر")
        await interaction.response.send_message("❌ ما عندك صلاحية تستخدم هذا الأمر.", ephemeral=True)
        return
    norm_type = map_type(التخصص)
    if norm_type not in SETTINGS.get("specialties", {}):
        await interaction.response.send_message(f"❌ التخصص `{التخصص}` غير موجود.", ephemeral=True)
        return
    SETTINGS["specialties"][norm_type]["price"] = السعر
    SETTINGS["specialties"][norm_type]["last_modified"] = datetime.utcnow().isoformat()
    await save_settings(SETTINGS)
    rebuild_prices()
    await log_audit("تعديل_سعر", interaction.user.id, None, f"تغيير سعر {norm_type} إلى {السعر}")
    await interaction.response.send_message(f"✅ تم تحديث سعر `{norm_type}` إلى {SETTINGS.get('currency', '$')}{السعر:.2f}", ephemeral=True)
