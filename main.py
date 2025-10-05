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
from pyrogram.errors import UserIsBlocked, PeerIdInvalid, RPCError, FloodWait, ChatAdminRequired, UserNotParticipant
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI
import uvicorn
import threading
from typing import Optional, Dict, List

# --- Configuration and Setup ---

load_dotenv()

# Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
MONGO_URI = os.environ.get("MONGO_URI")
JSON_FILTER_FILE = os.environ.get("JSON_FILTER_FILE", "filters.json")
JSON_USER_FILE = os.environ.get("JSON_USER_FILE", "users.json")
# Fix: Ensure a fallback URL works
START_PHOTO_URL = os.environ.get("START_PHOTO_URL", "https://telegra.ph/file/5a5d09f7b494f6c462370.jpg") 
SUPPORT_CHAT = os.environ.get("SUPPORT_CHAT", "teamrajweb")
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL", "teamrajweb")

ADMIN_IDS = []
try:
    ADMIN_IDS = [int(uid.strip()) for uid in os.environ.get("ADMIN_IDS", "").split(',') if uid.strip()]
except ValueError:
    print("⚠️ Warning: ADMIN_IDS mein sirf numbers hone chahiye.")

# Pyrogram Client with Custom Settings
app = Client(
    "filter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=50,
    sleep_threshold=10
)


# --- Advanced Storage System (MongoDB + JSON Fallback) ---

class AdvancedStorage:
    """Enhanced Storage System with Analytics & Caching"""
    
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
        self.cache = {}
        
        if MONGO_URI:
            try:
                # Fix: Use a safer connection setting
                self.db_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
                self.db_client.admin.command('ping')
                self.filter_collection = self.db_client["filter_db"]["filters"]
                self.user_collection = self.db_client["filter_db"]["users"]
                self.group_collection = self.db_client["filter_db"]["groups"]
                self.stats_collection = self.db_client["filter_db"]["stats"]
                self.use_mongo = True
                print("✅ MongoDB connected successfully.")
            except Exception as e:
                print(f"❌ MongoDB connection failed: {e}. Falling back to JSON file.")
        
        if not self.use_mongo:
            self._load_json()

    def _load_json(self):
        """Load data from JSON files"""
        files_to_load = {
            JSON_FILTER_FILE: 'local_filters',
            JSON_USER_FILE: 'local_users',
            'groups.json': 'local_groups',
            'stats.json': 'local_stats'
        }
        
        for filename, attr_name in files_to_load.items():
            if os.path.exists(filename):
                try:
                    with open(filename, 'r', encoding='utf-8') as f:
                        # Fix: Ensure stats loads correctly, especially the float 'bot_started'
                        data = json.load(f)
                        if attr_name == 'local_stats':
                            self.local_stats.update(data)
                        else:
                            setattr(self, attr_name, data)
                except Exception as e:
                    print(f"⚠️ Error loading {filename}: {e}")

    def _save_json(self):
        """Save data to JSON files"""
        files_to_save = {
            JSON_FILTER_FILE: self.local_filters,
            JSON_USER_FILE: self.local_users,
            'groups.json': self.local_groups,
            'stats.json': self.local_stats
        }
        
        for filename, data in files_to_save.items():
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except Exception as e:
                print(f"❌ Error saving {filename}: {e}")

    # --- Filter Methods with Caching ---
    
    async def add_filter(self, keyword: str, file_data: dict):
        """Add filter with metadata"""
        keyword = keyword.lower()
        file_data['added_at'] = time.time()
        file_data['added_by'] = file_data.get('added_by', 'Unknown')
        
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
        
        # Clear cache for robustness, though caching wasn't fully implemented in original
        # For simplicity, we'll rely on direct DB/JSON reads for the existing structure.
        pass 

    async def get_all_filters(self) -> Dict:
        """Get all filters"""
        # Fix: Remove caching logic as it wasn't fully developed, rely on direct reads
        if self.use_mongo:
            filters_list = {}
            async for doc in self.filter_collection.find({}):
                filters_list[doc['keyword']] = doc['files']
            return filters_list
        else:
            return self.local_filters

    async def delete_filter(self, keyword: str) -> bool:
        """Delete filter"""
        keyword = keyword.lower()
        if self.use_mongo:
            result = await self.filter_collection.delete_one({'keyword': keyword})
            success = result.deleted_count > 0
        else:
            success = keyword in self.local_filters
            if success:
                del self.local_filters[keyword]
                self._save_json()
        
        return success

    async def search_filters(self, query: str) -> List[str]:
        """Search filters by keyword"""
        query = query.lower()
        all_filters = await self.get_all_filters()
        return [k for k in all_filters.keys() if query in k]

    # --- User Management ---
    
    async def add_user(self, user_id: int, user_data: Optional[Dict] = None):
        """Add/Update user with detailed info"""
        user_id_str = str(user_id)
        current_time = time.time()
        
        user_info = {
            'last_seen': current_time,
            'username': user_data.get('username', '') if user_data else '',
            'first_name': user_data.get('first_name', '') if user_data else '',
            # Fix: Ensure search_count is maintained on update
            'search_count': user_data.get('search_count', 0) if user_data and 'search_count' in user_data else (await self.get_user_info(user_id)).get('search_count', 0) if await self.get_user_info(user_id) else 0
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
                existing_info = self.local_users[user_id_str]
                user_info['join_date'] = existing_info.get('join_date', current_time)
                # Fix: Preserve existing search count if not explicitly provided
                user_info['search_count'] = existing_info.get('search_count', 0)
            
            self.local_users[user_id_str] = user_info
            self._save_json()

    async def get_user_info(self, user_id: int) -> Optional[Dict]:
        """Get detailed user info"""
        user_id_str = str(user_id)
        
        if self.use_mongo:
            return await self.user_collection.find_one({'_id': user_id_str})
        else:
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
                self.local_users[user_id_str]['search_count'] = self.local_users[user_id_str].get('search_count', 0) + 1
                self._save_json()

    async def get_all_users(self) -> List[str]:
        """Get all user IDs as list of strings"""
        if self.use_mongo:
            # Fix: Ensure we return strings to match JSON storage format
            return [str(doc['_id']) async for doc in self.user_collection.find({})]
        else:
            return list(self.local_users.keys())

    async def remove_user(self, user_id: int):
        """Remove user from database"""
        user_id_str = str(user_id)
        if self.use_mongo:
            await self.user_collection.delete_one({'_id': user_id_str})
        else:
            if user_id_str in self.local_users:
                del self.local_users[user_id_str]
                self._save_json()

    # --- Group Management ---
    
    async def add_group(self, chat_id: int, chat_data: Dict):
        """Add/Update group info"""
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
                existing_info = self.local_groups[chat_id_str]
                group_info['join_date'] = existing_info.get('join_date', current_time)
            
            self.local_groups[chat_id_str] = group_info
            self._save_json()

    async def get_all_groups(self) -> List[str]:
        """Get all group IDs as list of strings"""
        if self.use_mongo:
            return [str(doc['_id']) async for doc in self.group_collection.find({})]
        else:
            return list(self.local_groups.keys())

    # --- Statistics ---
    
    async def increment_stat(self, stat_name: str):
        """Increment a statistic counter"""
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
        """Get all statistics"""
        if self.use_mongo:
            stats = await self.stats_collection.find_one({'_id': 'global'})
            # Fix: Ensure bot_started time is included even if DB is empty
            stats_dict = stats if stats else {}
            stats_dict.pop('_id', None) # Remove MongoDB internal ID
            stats_dict['bot_started'] = self.local_stats.get('bot_started', time.time())
            return stats_dict
        else:
            return self.local_stats


# Storage instance
STORAGE = AdvancedStorage()


# --- Custom Filters ---

def is_admin(_, __, message: Message):
    return message.from_user and message.from_user.id in ADMIN_IDS

admin_only = filters.create(is_admin)


# --- Stylish Start Command ---

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Ultra Stylish Start Command with Analytics"""
    
    if message.chat.type == ChatType.PRIVATE:
        user_data = {
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
            # join_date is handled in add_user
        }
        await STORAGE.add_user(message.chat.id, user_data)
    
    caption = f"""
╔═══❰ 🎭 **TEAM NARZO ANIME BOT** 🎭 ❱═══╗

**👋 HEY {message.from_user.first_name}!**

**🌟 WELCOME TO THE MOST ADVANCED AUTO-FILTER BOT! 🌟**

**⚡ PREMIUM FEATURES UNLOCKED ⚡**
━━━━━━━━━━━━━━━━━━━━
✨ **Lightning Fast Search**
🎯 **Smart Auto-Filter System**
🔥 **Unlimited Movie Collection**
📊 **Advanced Analytics**
🛡️ **24/7 Active Support**
━━━━━━━━━━━━━━━━━━━━

**💎 ADD ME TO YOUR GROUP & ENJOY PREMIUM EXPERIENCE! 💎**

**🔗 MAINTAINED BY:** [TEAM NARZO](https://t.me/{SUPPORT_CHAT})

╚═══════════════════════════╝
"""
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📚 Commands", callback_data="help_commands"),
            InlineKeyboardButton("💰 Earn Money", url="https://t.me/narzoxbot")
        ],
        [
            InlineKeyboardButton("👑 Premium", callback_data="premium_info"),
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
        print(f"❌ Error sending photo, sending text instead: {e}")
        await message.reply_text(
            caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN
        )


# --- Advanced Stats Command ---

@app.on_message(filters.command("stats") & admin_only)
async def stats_handler(client: Client, message: Message):
    """Detailed Bot Statistics for Admins"""
    
    users = await STORAGE.get_all_users()
    groups = await STORAGE.get_all_groups()
    filters_dict = await STORAGE.get_all_filters() # Renamed to avoid shadowing
    stats = await STORAGE.get_stats()
    
    uptime = time.time() - stats.get('bot_started', time.time())
    uptime_str = time.strftime('%H:%M:%S', time.gmtime(uptime))
    
    stats_msg = f"""
╔═══❰ 📊 **BOT STATISTICS** 📊 ❱═══╗

**👥 USER STATS:**
━━━━━━━━━━━━━━━━━━
- **Total Users:** `{len(users)}`
- **Total Groups:** `{len(groups)}`
- **Active Users (24h):** `N/A (Requires advanced query)` 

**📁 CONTENT STATS:**
━━━━━━━━━━━━━━━━━━
- **Total Filters:** `{len(filters_dict)}`
- **Total Files:** `{sum(len(v) for v in filters_dict.values())}`
- **Total Searches:** `{stats.get('total_searches', 0)}`

**⚙️ SYSTEM INFO:**
━━━━━━━━━━━━━━━━━━
- **Storage:** `{'🟢 MongoDB' if STORAGE.use_mongo else '🟡 JSON File'}`
- **Uptime:** `{uptime_str}`
- **Broadcasts Sent:** `{stats.get('total_broadcasts', 0)}`

**🔥 LAST 24H ACTIVITY:**
━━━━━━━━━━━━━━━━━━
- **New Users:** `N/A`
- **Searches (Total):** `{stats.get('total_searches', 0)}`

╚════════════════════════════╝
"""
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh_stats"),
            InlineKeyboardButton("📊 Detailed", callback_data="detailed_stats")
        ]
    ])
    
    await message.reply_text(stats_msg, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)


# --- Enhanced Ping Command ---

@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message):
    """Advanced Latency Check"""
    start_time = time.time()
    sent_message = await message.reply_text("🏓 **Pinging...**")
    end_time = time.time()
    
    latency = round((end_time - start_time) * 1000)
    
    # Emoji based on latency
    if latency < 100:
        emoji = "🟢"
        status = "Excellent"
    elif latency < 200:
        emoji = "🟡"
        status = "Good"
    else:
        emoji = "🔴"
        status = "Poor"
    
    await sent_message.edit_text(
        f"╔═══❰ 🏓 **PONG!** 🏓 ❱═══╗\n\n"
        f"{emoji} **Latency:** `{latency} ms`\n"
        f"📶 **Status:** `{status}`\n"
        f"💾 **Storage:** `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`\n\n"
        f"╚═══════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Advanced Search Filter ---

@app.on_message(filters.command("searchfilter") & admin_only)
async def search_filter_handler(client: Client, message: Message):
    """Search filters with pagination"""
    if len(message.command) < 2:
        return await message.reply_text("**Usage:** `/searchfilter <keyword>`", parse_mode=ParseMode.MARKDOWN)

    search_text = " ".join(message.command[1:])
    found_filters = await STORAGE.search_filters(search_text)
    
    if found_filters:
        all_filters_data = await STORAGE.get_all_filters() # Get all data once
        
        # Pagination logic
        page_size = 20
        total_pages = (len(found_filters) + page_size - 1) // page_size
        
        # Correctly display the first page
        filters_list = "\n".join(
            f"**{i+1}.** `{k}` - {len(all_filters_data.get(k, []))} files"
            for i, k in enumerate(found_filters[:page_size])
        )
        
        # NOTE: Pagination logic in the callback is complex for command handler. 
        # For simplicity in this fix, we'll only display the first page and remove the 
        # complex pagination buttons that don't have a supporting query handler.
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Done", callback_data="ignore_button")
            ]
        ])
        
        await message.reply_text(
            f"╔═══❰ 🔍 **SEARCH RESULTS** ❱═══╗\n\n"
            f"**Query:** `{search_text}`\n"
            f"**Found:** `{len(found_filters)}` filters\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{filters_list}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"╚══════════════════════════╝",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(f"❌ No filters found for: `{search_text}`", parse_mode=ParseMode.MARKDOWN)


# --- Mega Broadcast Feature ---

@app.on_message(filters.command("broadcast") & admin_only & filters.reply)
async def broadcast_handler(client: Client, message: Message):
    """Advanced Broadcast with Progress & Analytics"""
    
    replied_msg = message.reply_to_message
    if not replied_msg:
        return await message.reply_text("❌ Please reply to the message you want to broadcast.")
        
    status_msg = await message.reply_text(
        "╔═══❰ 📡 **BROADCAST** ❱═══╗\n\n"
        "⏳ **Initializing broadcast...**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 **Progress:** `0%`\n\n"
        "╚═════════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )
    
    user_ids_str = await STORAGE.get_all_users()
    total_users = len(user_ids_str)
    
    success_count = 0
    failed_count = 0
    removed_count = 0
    start_time = time.time()
    
    # Fix: Ensure status updates don't happen too fast for large user bases
    update_interval = max(1, total_users // 20) # Update every 5%
    
    for index, user_id_str in enumerate(user_ids_str, 1):
        try:
            user_id = int(user_id_str)
            await replied_msg.copy(user_id)
            success_count += 1
            await asyncio.sleep(0.05)
            
        except (UserIsBlocked, PeerIdInvalid):
            await STORAGE.remove_user(user_id)
            removed_count += 1
            failed_count += 1
            
        except FloodWait as e:
            await asyncio.sleep(e.value)
            
        except Exception as e:
            # print(f"Broadcast failed for user {user_id}: {e}") # Optional logging
            failed_count += 1
        
        # Update progress
        if index % update_interval == 0 or index == total_users:
            progress = (index / total_users) * 100
            
            # Fix: Handle possible FloodWait during edit
            try:
                await status_msg.edit_text(
                    f"╔═══❰ 📡 **BROADCASTING** ❱═══╗\n\n"
                    f"🔄 **Progress:** `{progress:.1f}%`\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"✅ **Sent:** `{success_count}`\n"
                    f"❌ **Failed:** `{failed_count}`\n"
                    f"🗑️ **Removed:** `{removed_count}`\n\n"
                    f"╚═════════════════════════╝",
                    parse_mode=ParseMode.MARKDOWN
                )
            except FloodWait as e:
                 await asyncio.sleep(e.value)
            except Exception:
                pass # Ignore if message is deleted or inaccessible
    
    end_time = time.time()
    duration = round(end_time - start_time, 2)
    
    await STORAGE.increment_stat('total_broadcasts')
    
    # Fix: Handle zero division if total_users is 0
    if total_users == 0:
        success_rate = 0
        avg_speed = 0
    else:
        success_rate = round((success_count / total_users) * 100, 2)
        avg_speed = round(total_users / max(duration, 0.01), 2)
    
    final_message = f"""
╔═══❰ ✅ **BROADCAST COMPLETE** ✅ ❱═══╗

**📊 STATISTICS:**
━━━━━━━━━━━━━━━━━━
- **Total Targeted:** `{total_users}`
- **Successfully Sent:** `{success_count}` 🟢
- **Failed:** `{failed_count}` 🔴
- **Removed from DB:** `{removed_count}` 🗑️

**⏱️ TIME TAKEN:**
━━━━━━━━━━━━━━━━━━
- **Duration:** `{duration}s`
- **Avg Speed:** `{avg_speed} users/sec`

**📈 SUCCESS RATE:**
━━━━━━━━━━━━━━━━━━
- **Rate:** `{success_rate}%`

╚════════════════════════════╝
"""
    
    await status_msg.edit_text(final_message, parse_mode=ParseMode.MARKDOWN)


# --- Enhanced Filter Management ---

@app.on_message(filters.command("addfilter") & admin_only & filters.reply)
async def add_filter_handler(client: Client, message: Message):
    """Add filter with confirmation"""
    if len(message.command) < 2:
        return await message.reply_text(
            "**❌ Invalid Usage!**\n\n"
            "**Correct Format:**\n"
            "`/addfilter <keyword>`\n\n"
            "**Note:** Reply to a message/media while using this command.",
            parse_mode=ParseMode.MARKDOWN
        )

    keyword = " ".join(message.command[1:]).strip()
    replied_msg = message.reply_to_message
    
    # Fix: Get the correct file type, especially for documents/videos/photos
    file_type = "text"
    if replied_msg.media:
        file_type = replied_msg.media.name.lower() # e.g., 'document', 'photo', 'video'
        
    file_data = {
        "chat_id": replied_msg.chat.id,
        "message_id": replied_msg.id,
        "added_by": message.from_user.id,
        "file_type": file_type
    }
    
    await STORAGE.add_filter(keyword, file_data)
    
    await message.reply_text(
        f"╔═══❰ ✅ **FILTER ADDED** ❱═══╗\n\n"
        f"**🔑 Keyword:** `{keyword}`\n"
        f"**📁 Type:** `{file_type}`\n"
        f"**👤 Added By:** {message.from_user.mention}\n"
        f"**💾 Storage:** `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`\n\n"
        f"╚════════════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


@app.on_message(filters.command("delfilter") & admin_only)
async def del_filter_handler(client: Client, message: Message):
    """Delete filter with confirmation"""
    if len(message.command) < 2:
        return await message.reply_text("**Usage:** `/delfilter <keyword>`", parse_mode=ParseMode.MARKDOWN)

    keyword = " ".join(message.command[1:]).strip()
    
    if await STORAGE.delete_filter(keyword):
        await message.reply_text(
            f"╔═══❰ 🗑️ **DELETED** ❱═══╗\n\n"
            f"**Filter `{keyword}` has been removed!**\n\n"
            f"╚═══════════════════════╝",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(f"❌ Filter `{keyword}` not found in database!", parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command("listfilters") & admin_only)
async def list_filters_handler(client: Client, message: Message):
    """List all filters with enhanced formatting"""
    all_filters = await STORAGE.get_all_filters()
    
    if not all_filters:
        return await message.reply_text("🚫 No filters currently saved in database!")

    # Sort by number of files
    sorted_filters = sorted(all_filters.items(), key=lambda x: len(x[1]), reverse=True)
    
    filters_list = "\n".join(
        f"**{i+1}.** `{k}` - **{len(v)}** files"
        for i, (k, v) in enumerate(sorted_filters[:50])  # Show top 50
    )
    
    total_files = sum(len(v) for v in all_filters.values())
    
    await message.reply_text(
        f"╔═══❰ 📚 **FILTER LIST** ❱═══╗\n\n"
        f"**📊 Summary:**\n"
        f"• **Total Keywords:** `{len(all_filters)}`\n"
        f"• **Total Files:** `{total_files}`\n"
        f"• **Storage:** `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{filters_list}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"╚════════════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


# --- Smart Keyword Matching (FIXED) ---

@app.on_message(filters.text & (filters.private | filters.group) & ~filters.edited & ~filters.command(["start", "help", "stats", "ping", "addfilter", "delfilter", "listfilters", "searchfilter", "broadcast"]))
async def keyword_match_handler(client: Client, message: Message):
    """Intelligent keyword matching with analytics"""
    
    # Store user/group info
    if message.chat.type == ChatType.PRIVATE:
        user_data = {
            'username': message.from_user.username or '',
            'first_name': message.from_user.first_name or '',
        }
        await STORAGE.add_user(message.chat.id, user_data)
        await STORAGE.increment_user_search(message.chat.id)
        
    elif message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        member_count = 0
        try:
            # FIX: Use try/except for member count to prevent crashes in private groups
            member_count = await client.get_chat_members_count(message.chat.id)
        except (ChatAdminRequired, UserNotParticipant, RPCError):
            pass # Fails silently if bot isn't an admin or participant

        chat_data = {
            'title': message.chat.title or '',
            'username': message.chat.username or '',
            'members_count': member_count
        }
        await STORAGE.add_group(message.chat.id, chat_data)
    
    text = message.text.lower()
    
    # FIX: Get filters ONCE before the loop
    all_filters = await STORAGE.get_all_filters()
    matched_keywords = []
    
    # Smart matching with word boundaries
    for keyword in all_filters.keys():
        # FIX: Ensure keywords are not empty, though unlikely with proper filter adding
        if not keyword:
            continue
            
        # The regex matching is good for smart search
        regex = r'\b' + re.escape(keyword) + r'\b'
        if re.search(regex, text):
            matched_keywords.append(keyword)
    
    if matched_keywords:
        await STORAGE.increment_stat('total_searches')
        
        for keyword in matched_keywords[:5]:  # Limit to 5 matches
            files_to_send = all_filters.get(keyword, [])
            
            for file_data in files_to_send[:10]:  # Limit to 10 files per keyword
                try:
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=file_data["chat_id"],
                        message_id=file_data["message_id"]
                    )
                    await asyncio.sleep(0.5)  # Anti-flood
                
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    # Catch file not found, permission errors, etc.
                    print(f"❌ Error forwarding message for keyword '{keyword}' from {file_data.get('chat_id')}/{file_data.get('message_id')}: {e}")


# --- Enhanced Callback Query Handler ---

@app.on_callback_query()
async def callback_query_handler(client: Client, callback_query: CallbackQuery):
    """Advanced callback handler with multiple features"""
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    # Helper to call the start command handler's logic
    async def get_start_message_content():
        caption = f"""
╔═══❰ 🎭 **TEAM NARZO ANIME BOT** 🎭 ❱═══╗

**👋 WELCOME BACK {callback_query.from_user.first_name}!**

**🌟 MOST ADVANCED AUTO-FILTER BOT! 🌟**

**⚡ PREMIUM FEATURES UNLOCKED ⚡**
━━━━━━━━━━━━━━━━━━━━
✨ **Lightning Fast Search**
🎯 **Smart Auto-Filter System**
🔥 **Unlimited Movie Collection**
📊 **Advanced Analytics**
🛡️ **24/7 Active Support**
━━━━━━━━━━━━━━━━━━━━

**💎 ADD ME TO YOUR GROUP & ENJOY PREMIUM EXPERIENCE! 💎**

**🔗 MAINTAINED BY:** [TEAM NARZO](https://t.me/{SUPPORT_CHAT})

╚═══════════════════════════╝
"""
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📚 Commands", callback_data="help_commands"),
                InlineKeyboardButton("💰 Earn Money", url="https://t.me/narzoxbot")
            ],
            [
                InlineKeyboardButton("👑 Premium", callback_data="premium_info"),
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
        return caption, keyboard

    try:
        if data == "help_commands":
            status = '🟢 MongoDB Connected' if STORAGE.use_mongo else '🟡 JSON Fallback Mode'
            help_text = f"""
╔═══❰ 🤖 **BOT COMMANDS** ❱═══╗

**👥 USER COMMANDS:**
━━━━━━━━━━━━━━━━━━
🔹 `/start` - Welcome message & menu
🔹 `/ping` - Check bot response time
🔹 `/help` - Show this help menu
🔹 `/myinfo` - Your statistics

**🛠️ ADMIN COMMANDS:**
━━━━━━━━━━━━━━━━━━
🔸 `/addfilter <keyword>` - Add new filter
   _(Reply to a message/media)_
🔸 `/delfilter <keyword>` - Delete filter
🔸 `/listfilters` - View all filters
🔸 `/searchfilter <text>` - Search filters
🔸 `/broadcast` - Broadcast message
   _(Reply to message to broadcast)_
🔸 `/stats` - Detailed bot statistics
🔸 `/cleandb` - Remove inactive users
🔸 `/backup` - Backup database

**🔥 ADVANCED FEATURES:**
━━━━━━━━━━━━━━━━━━
✨ Auto-filter in groups
🎯 Smart keyword matching
📊 User analytics tracking
🚀 High-speed file delivery
🛡️ Anti-spam protection
💾 Dual storage system

**⚡ STATUS:** {status}

╚════════════════════════════╝
"""
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🏠 Back to Start", callback_data="back_to_start"),
                    InlineKeyboardButton("📊 My Stats", callback_data="user_stats")
                ],
                [
                    InlineKeyboardButton("👑 Premium", callback_data="premium_info")
                ]
            ])
            
            await callback_query.edit_message_text(
                help_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await callback_query.answer("Help menu loaded! 📚")

        elif data == "back_to_start":
            caption, keyboard = await get_start_message_content()
            
            await callback_query.edit_message_text(
                caption,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await callback_query.answer("Welcome back! 🏠")

        elif data == "premium_info":
            premium_text = """
╔═══❰ 👑 **PREMIUM FEATURES** 👑 ❱═══╗

**🌟 UNLOCK EXCLUSIVE BENEFITS:**

**⚡ SPEED & PERFORMANCE:**
━━━━━━━━━━━━━━━━━━
✅ 10x Faster Response Time
✅ Priority Server Access
✅ Zero Downtime Guarantee
✅ Unlimited Concurrent Requests

**🎯 ADVANCED FEATURES:**
━━━━━━━━━━━━━━━━━━
✅ Custom Filter Sorting
✅ Advanced Search Algorithms
✅ Bulk Filter Management
✅ Auto-Delete Messages
✅ Custom Button Layouts
✅ Multi-Language Support

**📊 ANALYTICS & INSIGHTS:**
━━━━━━━━━━━━━━━━━━
✅ Detailed User Analytics
✅ Search Trend Reports
✅ Real-Time Statistics
✅ Export Data Options

**🛡️ SECURITY & SUPPORT:**
━━━━━━━━━━━━━━━━━━
✅ Dedicated 24/7 Support
✅ Priority Bug Fixes
✅ Custom Feature Requests
✅ Private Bot Deployment

**💰 PRICING:**
━━━━━━━━━━━━━━━━━━
- **Monthly:** ₹499/month
- **Quarterly:** ₹1299/3 months
- **Yearly:** ₹4999/year (Save 30%)

**📞 CONTACT FOR PREMIUM:**
Contact Admin: @{SUPPORT_CHAT}

╚════════════════════════════╝
"""
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💳 Purchase Now", url=f"https://t.me/{SUPPORT_CHAT}"),
                ],
                [
                    InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_start")
                ]
            ])
            
            await callback_query.edit_message_text(
                premium_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await callback_query.answer("Premium Features! 👑", show_alert=False)

        elif data == "about_info":
            all_filters = await STORAGE.get_all_filters()
            total_files = sum(len(v) for v in all_filters.values())
            users = await STORAGE.get_all_users()
            groups = await STORAGE.get_all_groups()
            
            about_text = f"""
╔═══❰ ℹ️ **ABOUT BOT** ℹ️ ❱═══╗

**🤖 BOT INFORMATION:**
━━━━━━━━━━━━━━━━━━
- **Name:** Team Narzo Anime Bot
- **Version:** 3.0 Advanced
- **Developer:** [TEAM NARZO](https://t.me/{SUPPORT_CHAT})
- **Language:** Python 3.11+
- **Framework:** Pyrogram

**📊 CURRENT STATISTICS:**
━━━━━━━━━━━━━━━━━━
- **Total Users:** `{len(users)}`
- **Total Groups:** `{len(groups)}`
- **Total Filters:** `{len(all_filters)}`
- **Total Files:** `{total_files}`

**🔧 TECHNOLOGY STACK:**
━━━━━━━━━━━━━━━━━━
- **Database:** MongoDB + JSON
- **API:** Pyrogram MTProto
- **Web Server:** FastAPI + Uvicorn
- **Deployment:** Render/Railway Ready
- **Storage:** Dual Mode (Cloud + Local)

**✨ KEY FEATURES:**
━━━━━━━━━━━━━━━━━━
✅ Smart Auto-Filter System
✅ Advanced Search Algorithm
✅ Real-Time Analytics
✅ Multi-Storage Support
✅ Anti-Flood Protection
✅ 24/7 Active Monitoring

**🌟 PREMIUM SERVICES:**
━━━━━━━━━━━━━━━━━━
- Custom Bot Deployment
- Unlimited File Storage
- Priority Support
- Advanced Features

**📞 CONTACT & SUPPORT:**
━━━━━━━━━━━━━━━━━━
💬 Support: @{SUPPORT_CHAT}
📢 Updates: @{UPDATE_CHANNEL}

╚════════════════════════════╝
"""
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💬 Support Chat", url=f"https://t.me/{SUPPORT_CHAT}"),
                    InlineKeyboardButton("📢 Updates", url=f"https://t.me/{UPDATE_CHANNEL}")
                ],
                [
                    InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_start")
                ]
            ])
            
            await callback_query.edit_message_text(
                about_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await callback_query.answer("About Bot Info! ℹ️")

        elif data == "user_stats":
            user_info = await STORAGE.get_user_info(user_id)
            
            if user_info:
                join_date = datetime.fromtimestamp(user_info.get('join_date', time.time()))
                last_seen = datetime.fromtimestamp(user_info.get('last_seen', time.time()))
                # Fix: Handle case where join_date is same as now to prevent division by zero for days_active
                time_diff = datetime.now() - join_date
                days_active = time_diff.days
                
                stats_text = f"""
╔═══❰ 📊 **YOUR STATISTICS** 📊 ❱═══╗

**👤 USER INFORMATION:**
━━━━━━━━━━━━━━━━━━
- **Name:** {callback_query.from_user.first_name}
- **Username:** @{user_info.get('username', 'Not Set')}
- **User ID:** `{user_id}`

**📈 ACTIVITY STATS:**
━━━━━━━━━━━━━━━━━━
- **Join Date:** {join_date.strftime('%d %b %Y')}
- **Days Active:** {days_active} days
- **Total Searches:** {user_info.get('search_count', 0)}
- **Last Seen:** {last_seen.strftime('%d %b %Y %H:%M')}

**🏆 ACHIEVEMENTS:**
━━━━━━━━━━━━━━━━━━
{'🌟 Active User' if user_info.get('search_count', 0) > 10 else ''}
{'🔥 Power User' if user_info.get('search_count', 0) > 50 else ''}
{'👑 Elite Member' if user_info.get('search_count', 0) > 100 else ''}

**💎 MEMBERSHIP:**
━━━━━━━━━━━━━━━━━━
- **Type:** Free User
- **Upgrade:** Available

╚════════════════════════════╝
"""
            else:
                stats_text = """
╔═══❰ 📊 **YOUR STATISTICS** 📊 ❱═══╗

**❌ No statistics found!**

Please use the bot more to see your stats.

╚════════════════════════════╝
"""
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("👑 Upgrade to Premium", callback_data="premium_info")
                ],
                [
                    InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_start")
                ]
            ])
            
            await callback_query.edit_message_text(
                stats_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await callback_query.answer("Your Statistics! 📊")

        elif data == "refresh_stats":
            # Refresh stats for admin (called from /stats handler)
            if user_id in ADMIN_IDS:
                # FIX: Check if the original message still exists
                if callback_query.message:
                    # Re-run the stats logic using the original message object
                    await stats_handler(client, callback_query.message)
                else:
                    # Fallback if message is too old or deleted
                    await callback_query.answer("Message is too old to refresh or has been deleted.", show_alert=True)
                
                await callback_query.answer("Stats refreshed! 🔄")
            else:
                await callback_query.answer("❌ Admin only feature!", show_alert=True)

        elif data == "detailed_stats":
            if user_id in ADMIN_IDS:
                users = await STORAGE.get_all_users()
                groups = await STORAGE.get_all_groups()
                
                detailed_text = f"""
╔═══❰ 📊 **DETAILED STATISTICS** 📊 ❱═══╗

**📈 GROWTH METRICS:**
━━━━━━━━━━━━━━━━━━
- **Users:** `{len(users)}`
- **Groups:** `{len(groups)}`
- **Storage Type:** `{'MongoDB' if STORAGE.use_mongo else 'JSON'}`

**🔥 TOP PERFORMING:**
━━━━━━━━━━━━━━━━━━
- **Most Searched:** `N/A (Requires advanced query)`
- **Most Active Group:** `N/A`
- **Top Keywords:** `N/A`

**💾 DATABASE INFO:**
━━━━━━━━━━━━━━━━━━
- **Collections:** `4`
- **Bot Timezone:** `IST (UTC+5:30)`
- **System Time:** `{datetime.now().strftime('%d %b %Y %H:%M:%S')}`

**⚡ PERFORMANCE:**
━━━━━━━━━━━━━━━━━━
- **Success Rate (Broadcast):** `N/A`
- **Anti-Flood:** `Active`
- **Workers:** `50`

╚════════════════════════════╝
"""
                
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="refresh_stats")]
                ])
                
                await callback_query.edit_message_text(
                    detailed_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
                await callback_query.answer("Detailed Stats Loaded! 📊")
            else:
                await callback_query.answer("❌ Admin only feature!", show_alert=True)
                
        elif data == "ignore_button":
            await callback_query.answer("I'm just a placeholder button.", show_alert=False)

        else:
            await callback_query.answer("❌ Invalid action!", show_alert=True)
            
    except Exception as e:
        print(f"❌ Callback Error: {e}")
        await callback_query.answer("❌ An error occurred! Try /start again.", show_alert=True)


# --- Additional Advanced Commands ---

@app.on_message(filters.command("myinfo"))
async def my_info_handler(client: Client, message: Message):
    """Show user's personal statistics"""
    user_id = message.from_user.id
    user_info = await STORAGE.get_user_info(user_id)
    
    if user_info:
        join_date = datetime.fromtimestamp(user_info.get('join_date', time.time()))
        time_diff = datetime.now() - join_date
        days_active = time_diff.days

        info_text = f"""
╔═══❰ 👤 **YOUR INFO** 👤 ❱═══╗

**📋 BASIC INFO:**
━━━━━━━━━━━━━━━━━━
- **Name:** {message.from_user.first_name}
- **Username:** @{message.from_user.username or 'Not Set'}
- **User ID:** `{user_id}`

**📊 STATISTICS:**
━━━━━━━━━━━━━━━━━━
- **Member Since:** {join_date.strftime('%d %b %Y')}
- **Days Active:** {days_active} days
- **Total Searches:** {user_info.get('search_count', 0)}

**🎖️ RANK:**
━━━━━━━━━━━━━━━━━━
- **Level:** {('Beginner' if user_info.get('search_count', 0) < 10 else 'Active' if user_info.get('search_count', 0) < 50 else 'Expert')}
- **Status:** {'Admin 👑' if user_id in ADMIN_IDS else 'Member'}

╚════════════════════════════╝
"""
    else:
        info_text = "❌ No information found. Use /start first!"
    
    await message.reply_text(info_text, parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command("cleandb") & admin_only)
async def clean_db_handler(client: Client, message: Message):
    """Remove inactive users from database"""
    status_msg = await message.reply_text("🧹 **Cleaning database...**", parse_mode=ParseMode.MARKDOWN)
    
    users = await STORAGE.get_all_users()
    removed = 0
    
    # Fix: Use a safer, more efficient way to check for blocked users
    for index, user_id_str in enumerate(users):
        user_id = int(user_id_str)
        try:
            # Check if bot can send a minimal action (typing)
            await client.send_chat_action(user_id, "typing")
        except (UserIsBlocked, PeerIdInvalid):
            await STORAGE.remove_user(user_id)
            removed += 1
        except Exception:
            # Catch other minor exceptions and keep the user
            pass 
        
        await asyncio.sleep(0.05) # Anti-flood delay
        
        # Update progress every 10%
        if index % max(1, len(users) // 10) == 0:
            await status_msg.edit_text(f"🔄 **Checking Users:** `{index}/{len(users)}`\n🗑️ **Removed:** `{removed}`", parse_mode=ParseMode.MARKDOWN)

    
    await status_msg.edit_text(
        f"╔═══❰ ✅ **DATABASE CLEANED** ❱═══╗\n\n"
        f"• **Checked:** `{len(users)}` users\n"
        f"• **Removed:** `{removed}` inactive users\n"
        f"• **Active:** `{len(users) - removed}` users\n\n"
        f"╚═════════════════════════════╝",
        parse_mode=ParseMode.MARKDOWN
    )


@app.on_message(filters.command("backup") & admin_only)
async def backup_handler(client: Client, message: Message):
    """Backup database to JSON file"""
    try:
        all_filters = await STORAGE.get_all_filters()
        users = await STORAGE.get_all_users()
        groups = await STORAGE.get_all_groups()
        
        backup_data = {
            "filters": all_filters,
            "users": users,
            "groups": groups,
            "timestamp": time.time(),
            "backup_date": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # Fix: Ensure filename is unique and in /tmp or current directory
        filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, indent=4, ensure_ascii=False)
        
        await message.reply_document(
            filename,
            caption=f"╔═══❰ 💾 **DATABASE BACKUP** ❱═══╗\n\n"
                   f"• **Filters:** `{len(all_filters)}`\n"
                   f"• **Users:** `{len(users)}`\n"
                   f"• **Groups:** `{len(groups)}`\n"
                   f"• **Date:** {datetime.now().strftime('%d %b %Y %H:%M')}\n\n"
                   f"╚═════════════════════════════╝",
            parse_mode=ParseMode.MARKDOWN
        )
        
        os.remove(filename)
        
    except Exception as e:
        await message.reply_text(f"❌ **Backup Failed:** {str(e)}", parse_mode=ParseMode.MARKDOWN)


@app.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    """Help command shortcut"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 View Commands", callback_data="help_commands")]
    ])
    
    await message.reply_text(
        "**👋 Need Help?**\n\n"
        "Click the button below to see all available commands!",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )


# --- FastAPI Health Check (Enhanced) ---

api = FastAPI(title="Team Narzo Bot API", version="3.0")

@api.get("/")
def health_check():
    """Enhanced health check with bot status"""
    uptime = time.time() - STORAGE.local_stats.get('bot_started', time.time())
    return {
        "status": "online",
        "bot": "Team Narzo Anime Bot",
        "version": "3.0 Advanced",
        "timestamp": datetime.now().isoformat(),
        "storage": "MongoDB" if STORAGE.use_mongo else "JSON",
        "uptime_seconds": uptime
    }

@api.get("/stats")
async def api_stats():
    """API endpoint for bot statistics"""
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
    # Fix: Get PORT from environment variables, default to 8000
    port = int(os.environ.get("PORT", 8000))
    # Fix: Use try/except in case uvicorn fails to start
    try:
        print(f"🚀 Starting FastAPI server on port {port}")
        uvicorn.run(api, host="0.0.0.0", port=port, log_level="info")
    except Exception as e:
        print(f"❌ Failed to start Uvicorn/FastAPI: {e}")


def start_bot():
    """Start Pyrogram bot"""
    print("🤖 Starting Team Narzo Bot...")
    print(f"💾 Storage Mode: {'MongoDB' if STORAGE.use_mongo else 'JSON Fallback'}")
    print(f"👑 Admins: {len(ADMIN_IDS)}")
    # Fix: Use try/except for app.run() in case of connection errors
    try:
        app.run()
    except Exception as e:
        print(f"❌ Pyrogram bot failed to start: {e}")


# --- Main Execution ---

if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════╗
    ║   🎭 TEAM NARZO ANIME BOT 🎭    ║
    ║      Advanced Edition v3.0       ║
    ╚══════════════════════════════════╝
    """)
    
    # Start web server in separate thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # Start bot
    start_bot()
