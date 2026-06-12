تيويويويimport os
import json
from pathlib import Path
from datetime importبيييي datetime, timedelta
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
# يمكن أن تحتوي على أسماء قنوات (نص) أو معرفات رقمية
DEFAULT_ALLOWED_CHANNELS = ["تسجيــــــــل-اعمال〢💵", "ム・💎〢شات・اونر"]

# Initial specialties definition (will be stored in settings)
DEFAULT_SPECIALTIES = {
    "تحرير": {"price": 0.50, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "ترجمة_كوري": {"price": 0.75, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "ترجمة_انجليزي": {"price": 0.60, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "تبييض": {"price": 0.25, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "سحب": {"price": 0.01, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "دمج": {"price": 0.01, "active": True, "last_modified": datetime.utcnow().isoformat()},
    "رفع": {"price": 0.005, "active": True, "last_modified": datetime.utcnow().isoformat()},
}