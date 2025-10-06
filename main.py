import os
import re
import json
import asyncio
import time
from datetime import datetime
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import (
    UserIsBlocked, PeerIdInvalid, RPCError, FloodWait, 
    ChatAdminRequired, UserNotParticipant, MessageDeleteForbidden
)
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI
import uvicorn
import threading
from typing import Optional, Dict, List
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configuration and Setup ---

load_dotenv()

# Environment Variables with validation
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
MONGO_URI = os.environ.get("MONGO_URI")
JSON_FILTER_FILE = os.environ.get("JSON_FILTER_FILE", "filters.json")
JSON_USER_FILE = os.environ.get("JSON_USER_FILE", "users.json")
START_PHOTO_URL = os.environ.get(
    "START_PHOTO_URL", 
    "https://telegra.ph/file/5a5d09f7b494f6c462370.jpg"
)
SUPPORT_CHAT = os.environ.get("SUPPORT_CHAT", "teamrajweb")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "teamrajweb")

# Validate critical environment variables
if not BOT_TOKEN or not API_ID or not API_HASH:
    raise ValueError("BOT_TOKEN, API_ID, and API_HASH must be set in environment variables!")

ADMIN_IDS = []
try:
    admin_ids_str = os.environ.get("ADMIN_IDS", "")
    if admin_ids_str:
        ADMIN_IDS = [int(uid.strip()) for uid in admin_ids_str.split(',') if uid.strip()]
except ValueError as e:
    logger.error(f"Invalid ADMIN_IDS format: {e}")

# Pyrogram Client
app = Client(
    "filter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50,
    sleep_threshold=10
)


# --- Advanced Storage System ---

class AdvancedStorage:
    """Enhanced Storage with MongoDB + JSON fallback"""
    
    def __init__(self):
        self.use_mongo = False
        self.local_filters = {}
        self.local_users = {}
        self.local_groups = {}
        self.local_stats = {
            "total_searches": 0,
            "total_broadcasts": 0,
            "bot_started": time.time()
        }
        self._init_mongodb()
        if not self.use_mongo:
            self._load_json()

    def _init_mongodb(self):
        """Initialize MongoDB connection"""
        if not MONGO_URI:
            logger.warning("MONGO_URI not provided, using JSON fallback")
            return
            
        try:
            self.db_client = AsyncIOMotorClient(
                MONGO_URI, 
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000
            )
            # Test connection
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.db_client.admin.command('ping'))
            loop.close()
            
            self.filter_collection = self.db_client["filter_db"]["filters"]
            self.user_collection = self.db_client["filter_db"]["users"]
            self.group_collection = self.db_client["filter_db"]["groups"]
            self.stats_collection = self.db_client["filter_db"]["stats"]
            self.use_mongo = True
            logger.info("✅ MongoDB connected successfully")
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}. Using JSON fallback")

    def _load_json(self):
        """Load data from JSON files"""
        files_map = {
            JSON_FILTER_FILE: 'local_filters',
            JSON_USER_FILE: 'local_users',
            'groups.json': 'local_groups',
            'stats.json': 'local_stats'
        }
        
        for filename, attr_name in files_map.items():
            if os.path.exists(filename):
                try:
                    with open(filename, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if attr_name == 'local_stats':
                            self.local_stats.update(data)
                        else:
                            setattr(self, attr_name, data)
                except Exception as e:
                    logger.error(f"Error loading {filename}: {e}")

    def _save_json(self):
        """Save data to JSON files"""
        files_map = {
            JSON_FILTER_FILE: self.local_filters,
            JSON_USER_FILE: self.local_users,
            'groups.json': self.local_groups,
            'stats.json': self.local_stats
        }
        
        for filename, data in files_map.items():
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Error saving {filename}: {e}")

    # --- Filter Methods ---
    
    async def add_filter(self, keyword: str, file_data: dict):
        """Add filter with metadata"""
        keyword = keyword.lower().strip()
        file_data['added_at'] = time.time()
        
        if self.use_mongo:
            await self.filter_collection.update_one(
                {'keyword': keyword},
                {'$push': {'files': file_data}},
                upsert=True
            )
        else:
            if keyword not in self.local_filters:
                self.local_filters[keyword] = []
            self.local_filters[keyword].append(file_data)
            self._save_json()

    async def get_all_filters(self) -> Dict:
        """Get all filters"""
        if self.use_mongo:
            filters_dict = {}
            async for doc in self.filter_collection.find({}):
                filters_dict[doc['keyword']] = doc['files']
            return filters_dict
        return self.local_filters

    async def delete_filter(self, keyword: str) -> bool:
        """Delete filter"""
        keyword = keyword.lower().strip()
        
        if self.use_mongo:
            result = await self.filter_collection.delete_one({'keyword': keyword})
            return result.deleted_count > 0
        else:
            if keyword in self.local_filters:
                del self.local_filters[keyword]
                self._save_json()
                return True
            return False

    async def search_filters(self, query: str) -> List[str]:
        """Search filters by keyword"""
        query = query.lower().strip()
        all_filters = await self.get_all_filters()
        return [k for k in all_filters.keys() if query in k]

    # --- User Management ---
    
    async def add_user(self, user_id: int, user_data: Optional[Dict] = None):
        """Add/Update user"""
        user_id_str = str(user_id)
        current_time = time.time()
        
        # Get existing user info if available
        existing_user = await self.get_user_info(user_id)
        
        user_info = {
            'last_seen': current_time,
            'username': user_data.get('username', '') if user_data else '',
            'first_name': user_data.get('first_name', '') if user_data else '',
            'search_count': existing_user.get('search_count', 0) if existing_user else 0
        }
        
        if self.use_mongo:
            await self.user_collection.update_one(
                {'_id': user_id_str},
                {'$set': user_info, '$setOnInsert': {'join_date': current_time}},
                upsert=True
            )
        else:
            if user_id_str not in self.local_users:
                user_info['join_date'] = current_time
            else:
                user_info['join_date'] = self.local_users[user_id_str].get('join_date', current_time)
                user_info['search_count'] = self.local_users[user_id_str].get('search_count', 0)
            
            self.local_users[user_id_str] = user_info
            self._save_json()

    async def get_user_info(self, user_id: int) -> Optional[Dict]:
        """Get user info"""
        user_id_str = str(user_id)
        
        if self.use_mongo:
            return await self.user_collection.find_one({'_id': user_id_str})
        return self.local_users.get(user_id_str)

    async def increment_user_search(self, user_id: int):
        """Increment user's search count"""
        user_id_str = str(user_id)
        
        if self.use_mongo:
            await self.user_collection.update_one(
                {'_id': user_id_str},
                {'$inc': {'search_count': 1}}
            )
        else:
            if user_id_str in self.local_users:
                self.local_users[user_id_str]['search_count'] = \
                    self.local_users[user_id_str].get('search_count', 0) + 1
                self._save_json()

    async def get_all_users(self) -> List[str]:
        """Get all user IDs"""
        if self.use_mongo:
            return [str(doc['_id']) async for doc in self.user_collection.find({})]
        return list(self.local_users.keys())

    async def remove_user(self, user_id: int):
        """Remove user"""
        user_id_str = str(user_id)
        
        if self.use_mongo:
            await self.user_collection.delete_one({'_id': user_id_str})
        else:
            if user_id_str in self.local_users:
                del self.local_users[user_id_str]
                self._save_json()

    # --- Group Management ---
    
    async def add_group(self, chat_id: int, chat_data: Dict):
        """Add/Update group"""
        chat_id_str = str(chat_id)
        current_time = time.time()
        
        group_info = {
            'title': chat_data.get('title', ''),
            'username': chat_data.get('username', ''),
            'members_count': chat_data.get('members_count', 0),
            'last_active': current_time,
        }
        
        if self.use_mongo:
            await self.group_collection.update_one(
                {'_id': chat_id_str},
                {'$set': group_info, '$setOnInsert': {'join_date': current_time}},
                upsert=True
            )
        else:
            if chat_id_str not in self.local_groups:
                group_info['join_date'] = current_time
            else:
                group_info['join_date'] = self.local_groups[chat_id_str].get('join_date', current_time)
            
            self.local_groups[chat_id_str] = group_info
            self._save_json()

    async def get_all_groups(self) -> List[str]:
        """Get all group IDs"""
        if self.use_mongo:
            return [str(doc['_id']) async for doc in self.group_collection.find({})]
        return list(self.local_groups.keys())

    # --- Statistics ---
    
    async def increment_stat(self, stat_name: str):
        """Increment statistic"""
        if self.use_mongo:
            await self.stats_collection.update_one(
                {'_id': 'global'},
                {'$inc': {stat_name: 1}},
                upsert=True
            )
        else:
            self.local_stats[stat_name] = self.local_stats.get(stat_name, 0) + 1
            self._save_json()

    async def get_stats(self) -> Dict:
        """Get statistics"""
        if self.use_mongo:
            stats = await self.stats_collection.find_one({'_id': 'global'})
            if not stats:
                return {'bot_started': self.local_stats.get('bot_started', time.time())}
            stats.pop('_id', None)
            stats['bot_started'] = self.local_stats.get('bot_started', time.time())
            return stats
        return self.local_stats


# Initialize storage
STORAGE = AdvancedStorage()


# --- Custom Filters ---

def is_admin(_, __, message: Message):
    return message.from_user and message.from_user.id in ADMIN_IDS

admin_only = filters.create(is_admin)


# --- Start Command ---

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Enhanced start command"""
    
    if message.chat.type == ChatType.PRIVATE:
        user_data = {
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
        }
        await STORAGE.add_user(message.chat.id, user_data)
    
    caption = f"""
╔═══❰ 🎭 TEAM NARZO ANIME BOT 🎭 ❱═══╗

**👋 HEY {message.from_user.first_name}!**

**🌟 WELCOME TO THE ADVANCED AUTO-FILTER BOT! 🌟**

**⚡ PREMIUM FEATURES ⚡**
━━━━━━━━━━━━━━━━━━━━
✨ Lightning Fast Search
🎯 Smart Auto-Filter System
🔥 Unlimited Movie Collection
📊 Advanced Analytics
🛡️ 24/7 Active Support
━━━━━━━━━━━━━━━━━━━━

**💎 ADD ME TO YOUR GROUP & ENJOY! 💎**

**🔗 MAINTAINED BY:** [TEAM NARZO](https://t.me/{SUPPORT_CHAT})

╚═══════════════════════════╝
"""
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 Commands", callback_data="help_commands"),
            InlineKeyboardButton("ℹ️ About", callback_data="about_info")
        ],
        [
            InlineKeyboardButton("📊 Statistics", callback_data="user_stats")
        ],
        [
            InlineKeyboardButton("➕ Add Me To Your Group ➕", 
                               url=f"http://t.me/{client.me.username}?startgroup=true")
        ],
        [
            InlineKeyboardButton("💬 Support", url=f"https://t.me/{SUPPORT_CHAT}"),
            InlineKeyboardButton("📢 Updates", url=f"https://t.me/{UPDATE_CHANNEL}")
        ]
    ])

    try:
        await client.send_photo(
            chat_id=message.chat.id,
            photo=START_PHOTO_URL,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error sending photo: {e}")
        await message.reply_text(
            caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN
        )


# --- Stats Command ---

@app.on_message(filters.command("stats") & admin_only)
async def stats_handler(client: Client, message: Message):
    """Bot statistics"""
    
    users = await STORAGE.get_all_users()
    groups = await STORAGE.get_all_groups()
    filters_dict = await STORAGE.get_all_filters()
    stats = await STORAGE.get_stats()
    
    uptime = time.time() - stats.get('bot_started', time.time())
    uptime_str = time.strftime('%H:%M:%S', time.gmtime(uptime))
    
    stats_msg = f"""
╔═══❰ 📊 BOT STATISTICS 📊 ❱═══╗

**👥 USER STATS:**
━━━━━━━━━━━━━━━━━━
• **Total Users:** `{len(users)}`
• **Total Groups:** `{len(groups)}`

**📁 CONTENT STATS:**
━━━━━━━━━━━━━━━━━━
• **Total Filters:** `{len(filters_dict)}`
• **Total Files:** `{sum(len(v) for v in filters_dict.values())}`
• **Total Searches:** `{stats.get('total_searches', 0)}`

**⚙️ SYSTEM INFO:**
━━━━━━━━━━━━━━━━━━
• **Storage:** `{'🟢 MongoDB' if STORAGE.use_mongo else '🟡 JSON'}`
• **Uptime:** `{uptime_str}`
• **Broadcasts:** `{stats.get('total_broadcasts', 0)}`

╚════════════════════════════╝
"""
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_stats")]
    ])
    
    await message.reply_text(stats_msg, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)


# --- Ping Command ---

@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message):
    """Latency check"""
    start_time = time.time()
    sent_message = await message.reply_text("🏓 **Pinging...**")
    end_time = time.time()
    
    latency = round((end_time - start_time) * 1000)
    
    if latency < 100:
        emoji, status = "🟢", "Excellent"
    elif latency < 200:
        emoji, status = "🟡", "Good"
    else:
        emoji, status = "🔴", "Poor"
    
    await sent_message.edit_text(
        f"╔═══❰ 🏓 PONG! 🏓 ❱═══╗\n\n"
        f"{emoji} **Latency:** `{latency} ms`\n"
        f"📶 **Status:** `{status}`\n"
        f"💾 **Storage:** `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`\n\n"
        f"╚═══════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Broadcast ---

@app.on_message(filters.command("broadcast") & admin_only & filters.reply)
async def broadcast_handler(client: Client, message: Message):
    """Broadcast message to all users"""
    
    replied_msg = message.reply_to_message
    if not replied_msg:
        return await message.reply_text("❌ Reply to a message to broadcast")
        
    status_msg = await message.reply_text(
        "📡 **Initializing broadcast...**",
        parse_mode=ParseMode.MARKDOWN
    )
    
    user_ids = await STORAGE.get_all_users()
    total = len(user_ids)
    success, failed, removed = 0, 0, 0
    start_time = time.time()
    
    update_interval = max(1, total // 20)
    
    for idx, user_id_str in enumerate(user_ids, 1):
        try:
            await replied_msg.copy(int(user_id_str))
            success += 1
            await asyncio.sleep(0.05)
            
        except (UserIsBlocked, PeerIdInvalid):
            await STORAGE.remove_user(int(user_id_str))
            removed += 1
            failed += 1
            
        except FloodWait as e:
            await asyncio.sleep(e.value)
            
        except Exception:
            failed += 1
        
        if idx % update_interval == 0 or idx == total:
            progress = (idx / total) * 100
            try:
                await status_msg.edit_text(
                    f"📡 **Broadcasting:** `{progress:.1f}%`\n"
                    f"✅ Sent: `{success}` | ❌ Failed: `{failed}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except (FloodWait, MessageDeleteForbidden):
                pass
    
    duration = round(time.time() - start_time, 2)
    await STORAGE.increment_stat('total_broadcasts')
    
    await status_msg.edit_text(
        f"╔═══❰ ✅ BROADCAST COMPLETE ❱═══╗\n\n"
        f"• **Sent:** `{success}` 🟢\n"
        f"• **Failed:** `{failed}` 🔴\n"
        f"• **Removed:** `{removed}` 🗑️\n"
        f"• **Time:** `{duration}s`\n\n"
        f"╚════════════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Filter Management ---

@app.on_message(filters.command("addfilter") & admin_only & filters.reply)
async def add_filter_handler(client: Client, message: Message):
    """Add filter"""
    if len(message.command) < 2:
        return await message.reply_text(
            "**Usage:** `/addfilter <keyword>`\n"
            "**Note:** Reply to a message",
            parse_mode=ParseMode.MARKDOWN
        )

    keyword = " ".join(message.command[1:]).strip()
    replied_msg = message.reply_to_message
    
    file_type = "text"
    if replied_msg.media:
        file_type = str(replied_msg.media).lower()
        
    file_data = {
        "chat_id": replied_msg.chat.id,
        "message_id": replied_msg.id,
        "added_by": message.from_user.id,
        "file_type": file_type
    }
    
    await STORAGE.add_filter(keyword, file_data)
    
    await message.reply_text(
        f"✅ **Filter Added**\n\n"
        f"**Keyword:** `{keyword}`\n"
        f"**Type:** `{file_type}`",
        parse_mode=ParseMode.MARKDOWN
    )


@app.on_message(filters.command("delfilter") & admin_only)
async def del_filter_handler(client: Client, message: Message):
    """Delete filter"""
    if len(message.command) < 2:
        return await message.reply_text("**Usage:** `/delfilter <keyword>`", parse_mode=ParseMode.MARKDOWN)

    keyword = " ".join(message.command[1:]).strip()
    
    if await STORAGE.delete_filter(keyword):
        await message.reply_text(f"✅ Filter `{keyword}` deleted!", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(f"❌ Filter `{keyword}` not found!", parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command("listfilters") & admin_only)
async def list_filters_handler(client: Client, message: Message):
    """List all filters"""
    all_filters = await STORAGE.get_all_filters()
    
    if not all_filters:
        return await message.reply_text("🚫 No filters found!")

    sorted_filters = sorted(all_filters.items(), key=lambda x: len(x[1]), reverse=True)
    
    filters_list = "\n".join(
        f"**{i+1}.** `{k}` - {len(v)} files"
        for i, (k, v) in enumerate(sorted_filters[:50])
    )
    
    total_files = sum(len(v) for v in all_filters.values())
    
    await message.reply_text(
        f"📚 **FILTER LIST**\n\n"
        f"**Total Keywords:** `{len(all_filters)}`\n"
        f"**Total Files:** `{total_files}`\n\n"
        f"{filters_list}",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Smart Keyword Matching ---

@app.on_message(
    filters.text & 
    (filters.private | filters.group) & 
    ~filters.edited & 
    ~filters.command([
        "start", "help", "stats", "ping", "addfilter", 
        "delfilter", "listfilters", "broadcast", "myinfo"
    ])
)
async def keyword_match_handler(client: Client, message: Message):
    """Smart keyword matching"""
    
    # Store user/group info
    if message.chat.type == ChatType.PRIVATE:
        user_data = {
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
        }
        await STORAGE.add_user(message.chat.id, user_data)
        await STORAGE.increment_user_search(message.chat.id)
        
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        try:
            member_count = await client.get_chat_members_count(message.chat.id)
        except:
            member_count = 0

        chat_data = {
            'title': message.chat.title or '',
            'username': message.chat.username or '',
            'members_count': member_count
        }
        await STORAGE.add_group(message.chat.id, chat_data)
    
    text = message.text.lower()
    all_filters = await STORAGE.get_all_filters()
    matched_keywords = []
    
    # Smart matching
    for keyword in all_filters.keys():
        if not keyword:
            continue
            
        regex = r'\b' + re.escape(keyword) + r'\b'
        if re.search(regex, text):
            matched_keywords.append(keyword)
    
    if matched_keywords:
        await STORAGE.increment_stat('total_searches')
        
        for keyword in matched_keywords[:5]:
            files = all_filters.get(keyword, [])
            
            for file_data in files[:10]:
                try:
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=file_data["chat_id"],
                        message_id=file_data["message_id"]
                    )
                    await asyncio.sleep(0.5)
                
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    logger.error(f"Error forwarding: {e}")


# --- Callback Handler ---

@app.on_callback_query()
async def callback_handler(client: Client, callback_query: CallbackQuery):
    """Handle callbacks"""
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    try:
        if data == "help_commands":
            help_text = """
📚 **BOT COMMANDS**

**User Commands:**
• `/start` - Start bot
• `/ping` - Check latency
• `/myinfo` - Your stats

**Admin Commands:**
• `/addfilter <keyword>` - Add filter
• `/delfilter <keyword>` - Delete filter
• `/listfilters` - List filters
• `/broadcast` - Broadcast message
• `/stats` - Bot statistics
"""
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back", callback_data="back_to_start")]
            ])
            
            await callback_query.edit_message_text(
                help_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        elif data == "back_to_start":
            caption = f"""
╔═══❰ 🎭 TEAM NARZO BOT 🎭 ❱═══╗

**👋 Welcome {callback_query.from_user.first_name}!**

Use the buttons below to navigate.

╚═══════════════════════════╝
"""
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📚 Commands", callback_data="help_commands"),
                    InlineKeyboardButton("ℹ️ About", callback_data="about_info")
                ],
                [InlineKeyboardButton("📊 My Stats", callback_data="user_stats")]
            ])
            
            await callback_query.edit_message_text(
                caption,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        elif data == "about_info":
            users = await STORAGE.get_all_users()
            groups = await STORAGE.get_all_groups()
            
            about_text = f"""
ℹ️ **ABOUT BOT**

**Statistics:**
• Users: `{len(users)}`
• Groups: `{len(groups)}`
• Storage: `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`

**Developer:** TEAM NARZO
**Support:** @{SUPPORT_CHAT}
"""
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back", callback_data="back_to_start")]
            ])
            
            await callback_query.edit_message_text(
                about_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        elif data == "user_stats":
            user_info = await STORAGE.get_user_info(user_id)
            
            if user_info:
                join_date = datetime.fromtimestamp(user_info.get('join_date', time.time()))
                
                stats_text = f"""
📊 **YOUR STATISTICS**

**Name:** {callback_query.from_user.first_name}
**User ID:** `{user_id}`
**Joined:** {join_date.strftime('%d %b %Y')}
**Searches:** `{user_info.get('search_count', 0)}`
"""
            else:
                stats_text = "❌ No stats found. Use /start first!"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Back", callback_data="back_to_start")]
            ])
            
            await callback_query.edit_message_text(
                stats_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        elif data == "refresh_stats":
            if user_id in ADMIN_IDS:
                await callback_query.answer("Refreshing stats...", show_alert=False)
                # Trigger stats refresh
                await stats_handler(client, callback_query.message)
            else:
                await callback_query.answer("❌ Admin only!", show_alert=True)

        else:
            await callback_query.answer()
            
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await callback_query.answer("❌ Error occurred!", show_alert=True)


# --- Additional Commands ---

@app.on_message(filters.command("myinfo"))
async def my_info_handler(client: Client, message: Message):
    """User's personal info"""
    user_id = message.from_user.id
    user_info = await STORAGE.get_user_info(user_id)
    
    if user_info:
        join_date = datetime.fromtimestamp(user_info.get('join_date', time.time()))
        
        info_text = f"""
👤 **YOUR INFO**

**Name:** {message.from_user.first_name}
**Username:** @{message.from_user.username or 'Not Set'}
**User ID:** `{user_id}`
**Joined:** {join_date.strftime('%d %b %Y')}
**Searches:** `{user_info.get('search_count', 0)}`
**Status:** {'Admin 👑' if user_id in ADMIN_IDS else 'Member'}
"""
    else:
        info_text = "❌ No info found. Use /start first!"
    
    await message.reply_text(info_text, parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    """Help command"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 View Commands", callback_data="help_commands")]
    ])
    
    await message.reply_text(
        "**👋 Need Help?**\n\nClick below to see all commands!",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )


# --- FastAPI Setup ---

api = FastAPI(title="Team Narzo Bot API", version="3.0")

@api.get("/")
def health_check():
    """Health check endpoint"""
    uptime = time.time() - STORAGE.local_stats.get('bot_started', time.time())
    return {
        "status": "online",
        "bot": "Team Narzo Anime Bot",
        "version": "3.0",
        "timestamp": datetime.now().isoformat(),
        "storage": "MongoDB" if STORAGE.use_mongo else "JSON",
        "uptime_seconds": round(uptime, 2)
    }

@api.get("/stats")
async def api_stats():
    """Statistics endpoint"""
    users = await STORAGE.get_all_users()
    groups = await STORAGE.get_all_groups()
    filters_dict = await STORAGE.get_all_filters()
    stats = await STORAGE.get_stats()
    
    return {
        "total_users": len(users),
        "total_groups": len(groups),
        "total_filters": len(filters_dict),
        "total_files": sum(len(v) for v in filters_dict.values()),
        "total_searches": stats.get('total_searches', 0),
        "storage_type": "MongoDB" if STORAGE.use_mongo else "JSON"
    }


def run_api():
    """Run FastAPI server"""
    port = int(os.environ.get("PORT", 8000))
    try:
        logger.info(f"Starting FastAPI on port {port}")
        uvicorn.run(api, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        logger.error(f"FastAPI failed: {e}")


def start_bot():
    """Start bot"""
    logger.info("Starting Team Narzo Bot...")
    logger.info(f"Storage: {'MongoDB' if STORAGE.use_mongo else 'JSON'}")
    logger.info(f"Admins: {len(ADMIN_IDS)}")
    
    try:
        app.run()
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")


# --- Main ---

if __name__ == "__main__":
    print("""
╔══════════════════════════════════╗
║   🎭 TEAM NARZO ANIME BOT 🎭    ║
║      Advanced Edition v3.0       ║
╚══════════════════════════════════╝
    """)
    
    # Start API in background
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # Start bot
    start_bot()
