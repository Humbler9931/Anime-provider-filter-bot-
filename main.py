import os
import re
import json
import asyncio
import time
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from pyrogram.errors import UserIsBlocked, PeerIdInvalid, RPCError 
from motor.motor_asyncio import AsyncIOMotorClient

# New Imports for Web Server (Render Deployment)
from fastapi import FastAPI
import uvicorn
import threading

# --- Configuration and Setup ---

load_dotenv()

# Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
MONGO_URI = os.environ.get("MONGO_URI")
JSON_FILTER_FILE = os.environ.get("JSON_FILTER_FILE", "filters.json")
JSON_USER_FILE = os.environ.get("JSON_USER_FILE", "users.json")
START_PHOTO_URL = os.environ.get("START_PHOTO_URL", "https://i.imgur.com/example.png") 
ADMIN_IDS = []
try:
    ADMIN_IDS = [int(uid.strip()) for uid in os.environ.get("ADMIN_IDS", "").split(',') if uid.strip()]
except ValueError:
    print("Warning: ADMIN_IDS mein sirf numbers hone chahiye.")

# Pyrogram Client
app = Client(
    "filter_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


# --- Storage Abstraction (MongoDB + JSON Fallback) ---

class Storage:
    def __init__(self):
        self.use_mongo = False
        self.local_filters = {}
        self.local_users = set()
        
        if MONGO_URI:
            try:
                self.db_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
                self.db_client.admin.command('ping')
                self.filter_collection = self.db_client["filter_db"]["filters"]
                self.user_collection = self.db_client["filter_db"]["users"]
                self.use_mongo = True
                print("MongoDB connected successfully.")
            except Exception as e:
                print(f"MongoDB connection failed: {e}. Falling back to JSON file.")
        
        if not self.use_mongo:
            self._load_json()

    # JSON loading/saving methods (same as before)
    def _load_json(self):
        if os.path.exists(JSON_FILTER_FILE):
            try:
                with open(JSON_FILTER_FILE, 'r', encoding='utf-8') as f:
                    self.local_filters = json.load(f)
            except Exception:
                self.local_filters = {}
        
        if os.path.exists(JSON_USER_FILE):
            try:
                with open(JSON_USER_FILE, 'r', encoding='utf-8') as f:
                    self.local_users = set(json.load(f)) 
            except Exception:
                self.local_users = set()

    def _save_json(self):
        try:
            with open(JSON_FILTER_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.local_filters, f, indent=4, ensure_ascii=False)
            with open(JSON_USER_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.local_users), f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving JSON file: {e}")

    # --- Filter Methods (same as before) ---
    async def add_filter(self, keyword: str, file_data: dict):
        keyword = keyword.lower()
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

    async def get_all_filters(self):
        if self.use_mongo:
            filters_list = {}
            async for doc in self.filter_collection.find({}):
                filters_list[doc['keyword']] = doc['files']
            return filters_list
        else:
            return self.local_filters

    async def delete_filter(self, keyword: str):
        keyword = keyword.lower()
        if self.use_mongo:
            result = await self.filter_collection.delete_one({'keyword': keyword})
            return result.deleted_count > 0
        else:
            if keyword in self.local_filters:
                del self.local_filters[keyword]
                self._save_json()
                return True
            return False

    # --- User Methods (Updated for Last Seen) ---
    async def add_user(self, user_id: int):
        user_id_str = str(user_id)
        current_time = time.time()
        if self.use_mongo:
            await self.user_collection.update_one(
                {'_id': user_id_str},
                {'$set': {'last_seen': current_time}}, # Last seen update
                upsert=True
            )
        else:
            if user_id_str not in self.local_users:
                self.local_users.add(user_id_str)
                self._save_json()

    async def get_all_users(self):
        if self.use_mongo:
            # Sirf user IDs return karein
            return [doc['_id'] async for doc in self.user_collection.find({})]
        else:
            return [int(uid) for uid in self.local_users]

    async def remove_user(self, user_id: int):
        user_id_str = str(user_id)
        if self.use_mongo:
            await self.user_collection.delete_one({'_id': user_id_str})
        else:
            if user_id_str in self.local_users:
                self.local_users.remove(user_id_str)
                self._save_json()


# Storage instance
STORAGE = Storage()


# --- Custom Admin Filter (Better Method) ---
# Pyrogram v2 mein built-in filters hain, lekin hum custom check hi rakhenge
def is_admin(filter_instance, client, message: Message):
    return message.from_user and message.from_user.id in ADMIN_IDS

admin_only = filters.create(is_admin)


# --- Advanced Command Handlers ---

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Start command (Image, Caption, Buttons) aur user store/update karein."""
    
    if message.chat.type == "private":
        await STORAGE.add_user(message.chat.id)
    
    caption = """
**HEY 🤸🏻‍♀️ ꧁ WELCOME TO TEAM NARZO ANIME BOT  ༆  ꧂ 🇮🇳 , SEARCH ANIME 👋**
    
**I AM THE MOST POWERFUL AUTO FILTER BOT** WITH **PREMIUM FEATURES**, JUST **ADD ME TO YOUR GROUP AND ENJOY!**
    
▶️ **MAINTAINED BY :** <a href='https://t.me/teamrajweb'>TEAM NARZO </a> ❞
    
**ADD ME TO YOUR GROUP**
"""
    
    keyboard = InlineKeyboardMarkup(
        [
            [  
                InlineKeyboardButton("• COMMANDS •", callback_data="help_commands"),
                InlineKeyboardButton("• EARN MONEY •", url="https://t.me/narzoxbot")
            ],
            [  
                InlineKeyboardButton("• PREMIUM •", callback_data="premium_info"),
                InlineKeyboardButton("• ABOUT •", callback_data="about_info")
            ],
            [ 
                 InlineKeyboardButton("➕ Add Me To Your Group ➕", url=f"http://t.me/{client.me.username}?startgroup=true")
            ]
        ]
    )

    try:
        await client.send_photo(
            chat_id=message.chat.id,
            photo=START_PHOTO_URL,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        # Fallback to text if photo fails
        print(f"Error sending photo from URL, falling back to text: {e}")
        await message.reply_text(
            caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )

# --- Admin Stats Command ---
@app.on_message(filters.command("stats") & admin_only)
async def stats_handler(client: Client, message: Message):
    """Admin ke liye total users count dikhayein."""
    user_ids = await STORAGE.get_all_users()
    
    stats_msg = (
        f"📊 **Bot Statistics**\n\n"
        f"👤 **Total Users:** `{len(user_ids)}`\n"
        f"💾 **Storage Type:** `{'MongoDB' if STORAGE.use_mongo else 'JSON File'}`"
    )
    await message.reply_text(stats_msg)

# --- Ping Command ---
@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message):
    """Bot latency check karein."""
    start_time = time.time()
    sent_message = await message.reply_text("Pinging...")
    end_time = time.time()
    
    latency = round((end_time - start_time) * 1000)
    await sent_message.edit_text(f"🚀 **Pong!**\nLatency: `{latency} ms`")


# --- Admin Broadcast Feature (Improved) ---
@app.on_message(filters.command("broadcast") & admin_only & filters.reply)
async def broadcast_handler(client: Client, message: Message):
    """Broadcast aur blocked users ko DB se hatayein."""
    
    status_msg = await message.reply_text("📡 **Broadcast** shuru ho raha hai...")
    replied_msg = message.reply_to_message
    
    user_ids_str = await STORAGE.get_all_users() # IDs as strings
    total_users = len(user_ids_str)
    
    success_count = 0
    failed_count = 0
    removed_count = 0
    
    for user_id_str in user_ids_str:
        user_id = int(user_id_str)
        try:
            await replied_msg.copy(user_id)
            success_count += 1
            await asyncio.sleep(0.1)
        except (UserIsBlocked, PeerIdInvalid): 
            # User ne block kiya ya ID invalid, DB se remove karein
            await STORAGE.remove_user(user_id)
            removed_count += 1
            failed_count += 1
        except RPCError:
             failed_count += 1
        except Exception:
            failed_count += 1
        
    final_message = (
        f"✅ **Broadcast Complete!**\n\n"
        f"➡️ **Total Targeted:** `{total_users}`\n"
        f"🟢 **Success:** `{success_count}`\n"
        f"🔴 **Failed:** `{failed_count}`\n"
        f"🗑️ **Removed from DB:** `{removed_count}`"
    )
    
    await status_msg.edit_text(final_message)


# --- Filter Management (same as before) ---
@app.on_message(filters.command("addfilter") & admin_only & filters.reply)
async def add_filter_handler(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: Reply to a message/media and use `/addfilter keyword`")

    keyword = message.command[1].strip()
    replied_msg = message.reply_to_message
    
    file_data = {
        "chat_id": replied_msg.chat.id,
        "message_id": replied_msg.id
    }
    
    await STORAGE.add_filter(keyword, file_data)
    await message.reply_text(f"✅ Filter **`{keyword}`** successfully added.\n"
                             f"Storage: {'MongoDB' if STORAGE.use_mongo else 'JSON File'}")

@app.on_message(filters.command("delfilter") & admin_only)
async def del_filter_handler(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/delfilter keyword`")

    keyword = message.command[1].strip()
    
    if await STORAGE.delete_filter(keyword):
        await message.reply_text(f"🗑️ Filter **`{keyword}`** deleted.")
    else:
        await message.reply_text(f"❌ Filter **`{keyword}`** not found.")

@app.on_message(filters.command("listfilters") & admin_only)
async def list_filters_handler(client: Client, message: Message):
    all_filters = await STORAGE.get_all_filters()
    
    if not all_filters:
        return await message.reply_text("🚫 No filters currently saved.")

    filters_list = "\n".join(f"• `{k}` ({len(v)} items)" for k, v in all_filters.items())
    await message.reply_text(f"**Saved Filters:** ({'MongoDB' if STORAGE.use_mongo else 'JSON File'})\n\n{filters_list}")


# --- Keyword Matching Handler (Error Fix applied here) ---
# filters.incoming ko filters.private | filters.group se badla gaya
@app.on_message(filters.text & (filters.private | filters.group) & ~filters.edited) 
async def keyword_match_handler(client: Client, message: Message):
    """Chat mein keyword milne par stored file/message bhejein."""
    
    if message.chat.type == "private":
        await STORAGE.add_user(message.chat.id) # Last seen update
        
    text = message.text.lower()
    all_filters = await STORAGE.get_all_filters()
    matched_keywords = []
    
    for keyword in all_filters.keys():
        regex = r'\b' + re.escape(keyword) + r'\b'
        if re.search(regex, text):
            matched_keywords.append(keyword)
            
    if matched_keywords:
        for keyword in matched_keywords:
            files_to_send = all_filters.get(keyword, [])
            
            for file_data in files_to_send:
                try:
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=file_data["chat_id"],
                        message_id=file_data["message_id"]
                    )
                except Exception as e:
                    print(f"Error forwarding message for keyword '{keyword}': {e}")
                    
# --- Inline Query Button Handler (same as before) ---
@app.on_callback_query()
async def callback_query_handler(client, callback_query):
    data = callback_query.data
    
    if data == "help_commands":
        await callback_query.answer("Commands: /start, /ping. Admins: /addfilter, /delfilter, /listfilters, /broadcast, /stats", show_alert=True)
    elif data == "premium_info":
        await callback_query.answer("Premium features ke liye admin se contact karein.", show_alert=True)
    elif data == "about_info":
        await callback_query.answer("Yeh bot Pyrogram aur MongoDB/JSON par based hai.", show_alert=True)
    else:
        await callback_query.answer("Invalid action.")


# --- Deployment Ready: Web Server for Render ---

# FastAPI instance
api = FastAPI()

@api.get("/")
def health_check():
    """Render ka health check endpoint."""
    return {"status": "Bot is alive and running."}

def run_api():
    """FastAPI server ko alag thread mein chalao."""
    # Render $PORT environment variable use karta hai, default 8000
    port = int(os.environ.get("PORT", 8000)) 
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="info")

def start_bot():
    """Pyrogram bot ko start karo."""
    print(f"Pyrogram bot starting...")
    app.run()


if __name__ == "__main__":
    # Bot aur web server ko alag alag threads mein chalao
    api_thread = threading.Thread(target=run_api)
    api_thread.start()
    
    async def delete_filter(self, keyword):
    start_bot()
    if self.use_mongo:
        result = await self.filter_collection.delete_one({'keyword': keyword})
        return result.deleted_count > 0
    else:
        if keyword in self.local_filters:
            del self.local_filters[keyword]
            self._save_json()
            return True
        return False

    # --- User Methods (Broadcast ke liye) ---
    async def add_user(self, user_id: int):
        user_id = str(user_id)
        if self.use_mongo:
            await self.user_collection.update_one(
                {'_id': user_id},
                {'$set': {'_id': user_id, 'date': time.time()}}, 
                upsert=True
            )
        else:
            if user_id not in self.local_users:
                self.local_users.add(user_id)
                self._save_json()

    async def get_all_users(self):
        if self.use_mongo:
            # Sirf user IDs return karein
            return [doc['_id'] async for doc in self.user_collection.find({})]
        else:
            return [int(uid) for uid in self.local_users]

    async def remove_user(self, user_id: int):
        user_id = str(user_id)
        if self.use_mongo:
            await self.user_collection.delete_one({'_id': user_id})
        else:
            if user_id in self.local_users:
                self.local_users.remove(user_id)
                self._save_json()


# Storage instance
STORAGE = Storage()


# --- Custom Filters ---

def is_admin(filter_instance, client, message: Message):
    """Check karta hai ki user admin hai ya nahi."""
    return message.from_user and message.from_user.id in ADMIN_IDS

admin_only = filters.create(is_admin)


# --- Advanced Command Handlers ---

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Start command (Image, Caption, Buttons) aur user store karein."""
    
    if message.chat.type == "private":
        await STORAGE.add_user(message.chat.id)
    
    # Stylish Caption (HTML/Markdown)
    caption = """
**HEY 🤸🏻‍♀️ ꧁ 🇲 🇷 ༆ 🇮 🇷 🇦 🇯 ꧂ 🇮🇳 , GOOD MORNING 👋**
    
**I AM THE MOST POWERFUL AUTO FILTER BOT** WITH **PREMIUM FEATURES**, JUST **ADD ME TO YOUR GROUP AND ENJOY!**
    
▶️ **MAINTAINED BY :** <a href='https://t.me/Yash_Chaudhary_007'>Yash</a> ❞
    
**ADD ME TO YOUR GROUP**
"""
    
    # Inline Keyboard Buttons
    keyboard = InlineKeyboardMarkup(
        [
            [  # First row
                InlineKeyboardButton("• COMMANDS •", callback_data="help_commands"),
                InlineKeyboardButton("• EARN MONEY •", url="https://t.me/Yash_Chaudhary_007") # Example Link
            ],
            [  # Second row
                InlineKeyboardButton("• PREMIUM •", callback_data="premium_info"),
                InlineKeyboardButton("• ABOUT •", callback_data="about_info")
            ],
            [  # Third row: Add to Group button
                 InlineKeyboardButton("➕ Add Me To Your Group ➕", url=f"http://t.me/{client.me.username}?startgroup=true")
            ]
        ]
    )

    try:
        # Image URL se photo bhejein
        await client.send_photo(
            chat_id=message.chat.id,
            photo=START_PHOTO_URL,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML # Zaroori for the link in caption
        )
    except Exception as e:
        print(f"Error sending photo from URL, falling back to text: {e}")
        # Agar photo bhejne mein error aaye, toh sirf text aur buttons bhejein
        await message.reply_text(
            caption,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML
        )

# --- New Feature: Bot Stats ---
@app.on_message(filters.command("stats") & admin_only)
async def stats_handler(client: Client, message: Message):
    """Admin ke liye total users count dikhayein."""
    user_ids = await STORAGE.get_all_users()
    
    stats_msg = (
        f"📊 **Bot Statistics**\n\n"
        f"👤 **Total Users:** `{len(user_ids)}`\n"
        f"💾 **Storage Type:** `{'MongoDB' if STORAGE.use_mongo else 'JSON File'}`"
    )
    await message.reply_text(stats_msg)

# --- New Feature: Ping ---
@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message):
    """Bot latency check karein."""
    start_time = time.time()
    sent_message = await message.reply_text("Pinging...")
    end_time = time.time()
    
    latency = round((end_time - start_time) * 1000) # Milliseconds mein
    await sent_message.edit_text(f"🚀 **Pong!**\nLatency: `{latency} ms`")


# --- Admin Broadcast Feature (Improved) ---

@app.on_message(filters.command("broadcast") & admin_only & filters.reply)
async def broadcast_handler(client: Client, message: Message):
    """Broadcast aur blocked users ko DB se hatayein."""
    
    status_msg = await message.reply_text("📡 **Broadcast** shuru ho raha hai...")
    replied_msg = message.reply_to_message
    
    user_ids = await STORAGE.get_all_users()
    total_users = len(user_ids)
    
    success_count = 0
    failed_count = 0
    removed_count = 0
    
    for user_id in user_ids:
        try:
            await replied_msg.copy(user_id)
            success_count += 1
            await asyncio.sleep(0.1)
        except (UserIsBlocked, PeerIdInvalid): # Agar user ne block kiya ya ID invalid ho
            await STORAGE.remove_user(user_id)
            removed_count += 1
            failed_count += 1
        except RPCError:
             failed_count += 1
        except Exception:
            failed_count += 1
        
    final_message = (
        f"✅ **Broadcast Complete!**\n\n"
        f"➡️ **Total Targeted:** `{total_users}`\n"
        f"🟢 **Success:** `{success_count}`\n"
        f"🔴 **Failed:** `{failed_count}`\n"
        f"🗑️ **Removed from DB:** `{removed_count}`"
    )
    
    await status_msg.edit_text(final_message)


# --- Filter Management and Keyword Matching (Same as before) ---
# ... (add_filter, delfilter, listfilters handlers yahan copy karein) ...
@app.on_message(filters.command("addfilter") & admin_only & filters.reply)
async def add_filter_handler(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: Reply to a message/media and use `/addfilter keyword`")

    keyword = message.command[1].strip()
    replied_msg = message.reply_to_message
    
    file_data = {
        "chat_id": replied_msg.chat.id,
        "message_id": replied_msg.id
    }
    
    await STORAGE.add_filter(keyword, file_data)
    await message.reply_text(f"✅ Filter **`{keyword}`** successfully added.\n"
                             f"Storage: {'MongoDB' if STORAGE.use_mongo else 'JSON File'}")

@app.on_message(filters.command("delfilter") & admin_only)
async def del_filter_handler(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: `/delfilter keyword`")

    keyword = message.command[1].strip()
    
    if await STORAGE.delete_filter(keyword):
        await message.reply_text(f"🗑️ Filter **`{keyword}`** deleted.")
    else:
        await message.reply_text(f"❌ Filter **`{keyword}`** not found.")

@app.on_message(filters.command("listfilters") & admin_only)
async def list_filters_handler(client: Client, message: Message):
    all_filters = await STORAGE.get_all_filters()
    
    if not all_filters:
        return await message.reply_text("🚫 No filters currently saved.")

    filters_list = "\n".join(f"• `{k}` ({len(v)} items)" for k, v in all_filters.items())
    await message.reply_text(f"**Saved Filters:** ({'MongoDB' if STORAGE.use_mongo else 'JSON File'})\n\n{filters_list}")


@app.on_message(filters.text & filters.incoming & (filters.group | filters.private) & ~filters.edited)
async def keyword_match_handler(client: Client, message: Message):
    """Chat mein keyword milne par stored file/message bhejein."""
    
    if message.chat.type == "private":
        await STORAGE.add_user(message.chat.id)
        
    text = message.text.lower()
    all_filters = await STORAGE.get_all_filters()
    matched_keywords = []
    
    for keyword in all_filters.keys():
        regex = r'\b' + re.escape(keyword) + r'\b'
        if re.search(regex, text):
            matched_keywords.append(keyword)
            
    if matched_keywords:
        for keyword in matched_keywords:
            files_to_send = all_filters.get(keyword, [])
            
            for file_data in files_to_send:
                try:
                    await client.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=file_data["chat_id"],
                        message_id=file_data["message_id"]
                    )
                except Exception as e:
                    print(f"Error forwarding message for keyword '{keyword}': {e}")
                    
# --- New Feature: Inline Query Buttons ki functionality (Optional) ---
@app.on_callback_query()
async def callback_query_handler(client, callback_query):
    data = callback_query.data
    
    if data == "help_commands":
        await callback_query.answer("Commands: /start, /ping. Admins: /addfilter, /delfilter, /listfilters, /broadcast, /stats", show_alert=True)
    elif data == "premium_info":
        await callback_query.answer("Premium features ke liye admin se contact karein.", show_alert=True)
    elif data == "about_info":
        await callback_query.answer("Yeh bot Pyrogram aur MongoDB/JSON par based hai.", show_alert=True)
    else:
        await callback_query.answer("Invalid action.")


# --- Bot Run ---

if __name__ == "__main__":
    print(f"Bot starting... Storage: {'MongoDB' if STORAGE.use_mongo else 'JSON File (Fallback)'}")
    app.run()

