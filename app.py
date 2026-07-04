import streamlit as st
import asyncio
import re
import threading
from playwright.async_api import async_playwright
from telegram import Update, ReactionTypeEmoji
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# --- 1. GLOBAL STATES & SIGNALS ---
bot_stop_signal = threading.Event()

global_browser_instance = None
telegram_app_instance = None
global_live_config = {}

group_processing_status = {"w16": False, "zn": False}

search_pages = {"w16": None, "zn": None}
password_pages = {"w16": None, "zn": None}

search_locks = {"w16": asyncio.Lock(), "zn": asyncio.Lock()}
password_locks = {"w16": asyncio.Lock(), "zn": asyncio.Lock()}

disconnect_msg_ids = {"w16": None, "zn": None}

BANK_MAP = {
    "KBANK": ["กสิกร", "kbank", "kasikorn"],
    "SCB": ["ไทยพาณิชย์", "scb", "siamcommercial"],
    "BBL": ["กรุงเทพ", "bbl", "bangkok"],
    "TTB": ["ทหารไทย", "ทีทีบี", "ttb", "tmb"],
    "KTB": ["กรุงไทย", "ktb", "krungthai"],
    "BAY": ["กรุงศรี", "bay", "ayudhya"],
    "GSB": ["ออมสิน", "gsb"],
    "GHB": ["อาคารสงเคราะห์", "ghb"],
    "UOB": ["ยูโอบี", "uob"],
    "BAAC": ["ธกส", "baac"],
    "KKP": ["เกียรตินาคิน", "kkp"],
    "CIMBT": ["ซีไอเอ็มบี", "cimbt"]
}

# --- 2. UTILITY FUNCTIONS ---
def mask_phone_number(phone_str):
    clean_num = re.sub(r'\D', '', phone_str)
    if len(clean_num) >= 9:
        return f"{clean_num[:3]}xxxxxx{clean_num[-2:]}"
    return "xxxxxx"

def detect_bank_keyword(text):
    text_lower = text.lower()
    for bank_code, keywords in BANK_MAP.items():
        for kw in keywords:
            if kw in text_lower:
                return bank_code
    return None

def generate_middle_aa_password(phone_str):
    clean_num = re.sub(r'\D', '', phone_str)
    mid_index = len(clean_num) // 2
    return f"{clean_num[:mid_index]}aa{clean_num[mid_index:]}"

async def delay_and_mask_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, username: str, masked_phone: str):
    await asyncio.sleep(300) 
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"เปลี่ยนเบอร์สำเร็จ\n\n"
                f"👉ยูสเซอร์ : {masked_phone}\n"
                f"👉ลูกค้าใช้รหัสผ่านเดิมในการเข้าเล่นได้เลยนะคะ\n"
                f"( น้องแนะนำให้เข้าจากเบราเซอร์ Google Chrome นะคะ)❤️"
            )
        )
    except: pass

async def clear_alert_popup(page):
    try:
        swal_ok_btn = page.locator(".swal2-container button.swal2-confirm, .swal2-container button:has-text('ตกลง')").first
        if await swal_ok_btn.is_visible(timeout=500):
            await swal_ok_btn.click()
            await asyncio.sleep(0.5)
    except: pass

async def force_click_manage_user(page):
    try:
        manage_user_btn = page.locator("button:has-text('จัดการผู้ใช้'), a:has-text('จัดการผู้ใช้'), .btn-outline-danger:has-text('จัดการผู้ใช้'), button.btn-outline-danger").first
        if await manage_user_btn.is_visible(timeout=1500):
            await manage_user_btn.click()
            await asyncio.sleep(1.0)
            await clear_alert_popup(page)
    except: pass

async def force_clear_input(page, locator):
    try:
        await locator.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await locator.fill("")
        await asyncio.sleep(0.2)
    except: pass

async def wait_until_table_loaded(page):
    try:
        loading_box = page.locator(":has-text('Loading...'), .loading, [class*='loading']")
        for _ in range(40):
            if not await loading_box.first.is_visible():
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.0)
    except: pass

async def verify_row_security(first_row, target_acc, target_bank_code):
    try:
        row_text = await first_row.inner_text()
        clean_row_text = row_text.replace("-", "").replace(" ", "")
        
        if target_acc and target_acc not in clean_row_text:
            return False, "❌ เลขบัญชีไม่ตรงกับในระบบ"
            
        if target_bank_code:
            img_locator = first_row.locator("img")
            img_count = await img_locator.count()
            bank_verified = False
            for i in range(img_count):
                src = await img_locator.nth(i).get_attribute("src")
                if src and target_bank_code.upper() in src.upper():
                    bank_verified = True
                    break
            if not bank_verified:
                return False, f"❌ ธนาคารในระบบไม่ตรงกับที่ระบุ ({target_bank_code})"
                
        return True, "ผ่านการตรวจสอบ"
    except Exception as e:
        return False, "❌ เกิดข้อผิดพลาดในการตรวจสอบข้อมูลหลังบ้าน"

async def check_and_recover_session(page, web_key, is_password_gate=False):
    try:
        pass_input = page.locator("input[type='password'], input[name='password']").first
        if await pass_input.is_visible(timeout=800):
            await notify_disconnection(web_key)
            
            user_input = page.locator("input[type='text'], input[placeholder*='User'], input[name='username']").first
            cfg = global_live_config[web_key]
            
            if is_password_gate:
                await user_input.fill(cfg["pwd_user"])
                await pass_input.fill(cfg["pwd_pass"])
            else:
                await user_input.fill(cfg["user"])
                await pass_input.fill(cfg["pass"])
                
            await page.locator("button[type='submit'], button:has-text('เข้าสู่ระบบ'), button.btn-primary").first.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)
            await clear_alert_popup(page)
            await force_click_manage_user(page)
            
            await clear_disconnection_notice(web_key)
    except: pass

async def notify_disconnection(web_key):
    try:
        if telegram_app_instance and not disconnect_msg_ids[web_key]:
            chat_id = global_live_config[web_key]["chat_id"]
            msg = await telegram_app_instance.bot.send_message(
                chat_id=chat_id, 
                text="⚠️ คุณย่าเคยพุดเอาไว้ว่า.. กำลังขาดการเชื่อมต่อ"
            )
            disconnect_msg_ids[web_key] = msg.message_id
    except: pass

async def clear_disconnection_notice(web_key):
    try:
        if telegram_app_instance and disconnect_msg_ids[web_key]:
            chat_id = global_live_config[web_key]["chat_id"]
            await telegram_app_instance.bot.delete_message(chat_id=chat_id, message_id=disconnect_msg_ids[web_key])
            disconnect_msg_ids[web_key] = None
    except: pass

# --- BACKGROUND LOGIN ENGINE ---
async def login_search_backend(web_key, config_data):
    page = search_pages[web_key]
    cfg = config_data[web_key]
    try:
        if not cfg["url"]: return
        await page.goto(cfg["url"], timeout=45000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)
        user_input = page.locator("input[type='text'], input[placeholder*='User'], input[name='username']").first
        pass_input = page.locator("input[type='password'], input[name='password']").first
        if await user_input.is_visible():
            await user_input.fill(cfg["user"])
            await pass_input.fill(cfg["pass"])
            await page.locator("button[type='submit'], button:has-text('เข้าสู่ระบบ'), button.btn-primary").first.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(4)
        await clear_alert_popup(page)
        await force_click_manage_user(page)
        await telegram_app_instance.bot.send_message(chat_id=cfg["chat_id"], text="🤖 ฉันก้แค่ไรเดอร์ที่ผ่านมารีเซ็ตรหัสผ่าน..")
    except: pass

async def login_password_backend(web_key, config_data):
    page = password_pages[web_key]
    cfg = config_data[web_key]
    try:
        if not cfg["url"]: return
        await page.goto(cfg["url"], timeout=45000)
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)
        user_input = page.locator("input[type='text'], input[placeholder*='User'], input[name='username']").first
        pass_input = page.locator("input[type='password'], input[name='password']").first
        if await user_input.is_visible():
            await user_input.fill(cfg["pwd_user"])
            await pass_input.fill(cfg["pwd_pass"])
            await page.locator("button[type='submit'], button:has-text('เข้าสู่ระบบ'), button.btn-primary").first.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(4)
        await clear_alert_popup(page)
        await force_click_manage_user(page)
    except: pass

# --- 3. CORE WORKERS ---
async def process_clear_turn_by_username(web_key, username_target, config_data):
    async with password_locks[web_key]:  
        page = password_pages[web_key]   
        try:
            await check_and_recover_session(page, web_key, is_password_gate=True)
            await force_click_manage_user(page)
            
            phone_input = page.locator("input[placeholder*='user123'], input[placeholder*='เบอร์โทรศัพท์']").first
            if await phone_input.is_visible(timeout=500): await force_clear_input(page, phone_input)
            
            search_input = page.locator("input[placeholder*='ZAB'], input[placeholder*='รหัสผู้ใช้']").first
            await force_clear_input(page, search_input)
            await search_input.fill(username_target)
            await asyncio.sleep(0.2)
            
            await page.locator("button:has-text('ค้นหา'), .btn-purple:has-text('ค้นหา'), button.btn-success, button.btn-purple").first.click()
            
            await wait_until_table_loaded(page)
            
            found_target = False
            for _ in range(80):
                rows = page.locator("table tbody tr")
                if await rows.count() > 0:
                    first_row_text = await rows.first.inner_text()
                    if username_target in first_row_text or "ไม่พบ" in first_row_text or "No data" in first_row_text:
                        if "ไม่พบ" not in first_row_text and "No data" not in first_row_text:
                            found_target = True
                        break
                await asyncio.sleep(0.3)
                
            if not found_target: return "ไม่พบข้อมูล"
            
            first_row = page.locator("table tbody tr").first
            
            promo_cell = first_row.locator("td").nth(8)
            promo_text = await promo_cell.text_content(timeout=2000)
            
            credit_cell = first_row.locator("td").nth(10)
            credit_text = await credit_cell.text_content(timeout=2000)
            clean_credit_str = re.sub(r'[^\d.]', '', credit_text.strip())
            credit_val = float(clean_credit_str) if clean_credit_str else 0.0
            
            if credit_val >= 5.0:
                return f"เครดิตมากกว่า 5 บาท"
                
            gift_btn = first_row.locator("td").nth(9).locator("button").first
            if not promo_text.strip() or not await gift_btn.is_visible(timeout=1000):
                return "NOT_STUCK"
            
            await gift_btn.click()
            await asyncio.sleep(1.2)
         
            clear_turn_menu = page.locator(".dropdown-menu.show a:has-text('ล้างเทิร์น'), .dropdown-menu.show li:has-text('ล้างเทิร์น'), div[class*='dropdown'] a:has-text('ล้างเทิร์น')").first
            if await clear_turn_menu.is_visible():
                await clear_turn_menu.click(force=True)
            else:
                await page.locator(".dropdown-menu.show i.fa-sync, .dropdown-menu.show i.fa-refresh").last.click(force=True)
            await asyncio.sleep(1.5)
            
            remark_input = page.locator(".modal-content input[type='text'], .modal-body input, input#comment, .modal-dialog input").first
            await force_clear_input(page, remark_input)
            await remark_input.fill("ล้างเทิร์น")
            await asyncio.sleep(0.3)
            
            save_btn = page.locator(".modal-content button:has-text('บันทึก'), .modal-footer button:has-text('บันทึก'), button.btn-success:has-text('บันทึก')").first
            await save_btn.click()
            await asyncio.sleep(2.0)
            
            close_confirm_btn = page.locator(".swal2-container button:has-text('ปิด'), button:has-text('ปิด'), .btn-secondary, button.swal2-confirm").first
            if await close_confirm_btn.is_visible(timeout=3000): await close_confirm_btn.click()
            
            await force_click_manage_user(page)
            return "✅ สำเร็จ"
        except:
            return "ไม่พบข้อมูล"

async def search_phone_number(web_key, search_phone, config_data):
    async with search_locks[web_key]:
        page = search_pages[web_key]
        try:
            await check_and_recover_session(page, web_key, is_password_gate=False)
            await force_click_manage_user(page)
            
            search_input = page.locator("input[placeholder*='ZAB'], input[placeholder*='รหัสผู้ใช้']").first
            if await search_input.is_visible(timeout=500): await force_clear_input(page, search_input)

            phone_input = page.locator("input[placeholder*='user123'], input[placeholder*='เบอร์โทรศัพท์']").first
            await force_clear_input(page, phone_input)
            await phone_input.fill(search_phone)
            await asyncio.sleep(0.2)
            
            await page.locator("button:has-text('ค้นหา'), .btn-purple:has-text('ค้นหา'), button.btn-success").first.click()
            
            await wait_until_table_loaded(page)
  
            found_target = False
            for _ in range(80):
                rows = page.locator("table tbody tr")
                if await rows.count() > 0:
                    first_row_text = await rows.first.inner_text()
                    if search_phone in first_row_text.replace("-","") or "ไม่พบ" in first_row_text or "No data" in first_row_text:
                        if "ไม่พบ" not in first_row_text and "No data" not in first_row_text:
                            found_target = True
                        break
                await asyncio.sleep(0.3)
                
            if not found_target: return "ไม่พบข้อมูล"
            
            first_row = page.locator("table tbody tr").first
            phone_cell = first_row.locator("td").nth(3)
            cell_text = await phone_cell.text_content(timeout=2000)
            clean_numbers = re.findall(r'0\d{8,9}', cell_text.replace("-", "").replace(" ", ""))
            return clean_numbers[0] if clean_numbers else "❌ อ่านเบอร์ไม่สำเร็จ"
        except: return "ไม่พบข้อมูล"

async def search_username_by_phone(web_key, search_phone, config_data):
    async with search_locks[web_key]:
        page = search_pages[web_key]
        try:
            await check_and_recover_session(page, web_key, is_password_gate=False)
            await force_click_manage_user(page)
            
            user_search_input = page.locator("input[placeholder*='ZAB'], input[placeholder*='รหัสผู้ใช้']").first
            if await user_search_input.is_visible(timeout=500): await force_clear_input(page, user_search_input)

            phone_search_input = page.locator("input[placeholder*='user123'], input[placeholder*='เบอร์โทรศัพท์']").first
            await force_clear_input(page, phone_search_input)
            await phone_search_input.fill(search_phone)
            await asyncio.sleep(0.2)
            
            await page.locator("button:has-text('ค้นหา'), .btn-purple:has-text('ค้นหา'), button.btn-success").first.click()
            
            await wait_until_table_loaded(page)
            
            found_target = False
            for _ in range(80):
                rows = page.locator("table tbody tr")
                if await rows.count() > 0:
                    first_row_text = await rows.first.inner_text()
                    clean_row_text = first_row_text.replace("-", "").replace(" ", "")
                    if search_phone[:5] in clean_row_text or "ไม่พบ" in first_row_text or "No data" in first_row_text:
                        if "ไม่พบ" not in first_row_text and "No data" not in first_row_text:
                            found_target = True
                        break
                await asyncio.sleep(0.3)
                
            if not found_target: return "ไม่พบข้อมูล"
            
            first_row = page.locator("table tbody tr").first
            username_cell = first_row.locator("td").nth(4)
            raw_username = await username_cell.text_content(timeout=2000)
            clean_username = raw_username.strip()
            if "ZAB" in clean_username:
                found_zabs = re.findall(r'ZAB\w+', clean_username)
                return found_zabs[0] if found_zabs else clean_username
            return "❌ อ่านรหัส ZAB ไม่สำเร็จ"
        except: return "ไม่พบข้อมูล"

async def change_user_phone(web_key, search_phone, target_acc, target_bank_code, new_phone, config_data):
    async with search_locks[web_key]:
        page = search_pages[web_key]
        try:
            await check_and_recover_session(page, web_key, is_password_gate=False)
            await force_click_manage_user(page)
            
            search_input = page.locator("input[placeholder*='ZAB'], input[placeholder*='รหัสผู้ใช้']").first
            if await search_input.is_visible(timeout=500): await force_clear_input(page, search_input)

            phone_input = page.locator("input[placeholder*='user123'], input[placeholder*='เบอร์โทรศัพท์']").first
            await force_clear_input(page, phone_input)
            await phone_input.fill(search_phone)
            await asyncio.sleep(0.2)
            await page.locator("button:has-text('ค้นหา'), .btn-purple:has-text('ค้นหา'), button.btn-success").first.click()
            
            await wait_until_table_loaded(page)
            await asyncio.sleep(1.5)
            
            first_row = page.locator("table tbody tr").first
            
            is_safe, error_msg = await verify_row_security(first_row, target_acc, target_bank_code)
            if not is_safe: return error_msg
            
            green_edit_btn = first_row.locator("button:has-text('แก้ไขผู้ใช้'), .btn-success:has-text('แก้ไขผู้ใช้'), button.btn-outline-purple").first
            await green_edit_btn.wait_for(state="visible", timeout=12000)
            await green_edit_btn.click()
            await asyncio.sleep(0.8) 
         
            dropdown_sub_btn = page.locator(".dropdown-menu.show a, .dropdown-toggle, .dropdown-menu.show span").filter(has_text="แก้ไขผู้ใช้").last
            await dropdown_sub_btn.click(force=True, timeout=2000)
            await asyncio.sleep(2.0) 
            
            phone_modal_input = page.locator(".modal-body input[type='text'], .modal-content input[placeholder*='เบอร์'], input#mobile").first
            await force_clear_input(page, phone_modal_input)
            await phone_modal_input.fill(new_phone)
            await asyncio.sleep(0.2)
            
            await page.locator(".modal-content button:has-text('บันทึก'), .modal-footer button:has-text('บันทึก'), button:has-text('บันทึก')").first.click()
            await asyncio.sleep(2.0)
            
            grey_close_btn = page.locator(".swal2-confirm, button:has-text('ปิด'), .btn-secondary:has-text('ปิด')").first
            if await grey_close_btn.is_visible(timeout=2000): await grey_close_btn.click()
            
            await force_click_manage_user(page)
            return "✅ สำเร็จ"
        except: return "ไม่พบข้อมูล"

async def change_user_password_fast(web_key, search_phone, target_acc, target_bank_code, config_data):
    new_password_str = generate_middle_aa_password(search_phone)
    async with password_locks[web_key]:
        page = password_pages[web_key]
        if not page: return "ไม่พบข้อมูล"
        try:
            await check_and_recover_session(page, web_key, is_password_gate=True)
            await force_click_manage_user(page)
            
            search_input = page.locator("input[placeholder*='ZAB'], input[placeholder*='รหัสผู้ใช้']").first
            if await search_input.is_visible(timeout=500): await force_clear_input(page, search_input)

            phone_input = page.locator("input[placeholder*='user123'], input[placeholder*='เบอร์โทรศัพท์']").first
            await force_clear_input(page, phone_input)
            await phone_input.fill(search_phone)
            await asyncio.sleep(0.2)
            await page.locator("button:has-text('ค้นหา'), .btn-purple:has-text('ค้นหา'), button.btn-success").first.click()
            
            await wait_until_table_loaded(page)
            await asyncio.sleep(1.5)
            
            first_row = page.locator("table tbody tr").first
            
            is_safe, error_msg = await verify_row_security(first_row, target_acc, target_bank_code)
            if not is_safe: return error_msg
            
            green_edit_btn = first_row.locator("button:has-text('แก้ไขผู้ใช้'), .btn-success:has-text('แก้ไขผู้ใช้'), button.btn-outline-purple").first
            await green_edit_btn.wait_for(state="visible", timeout=12000)
            await green_edit_btn.click()
            await asyncio.sleep(0.8)
            
            dropdown_pwd_btn = page.locator(".dropdown-menu.show a, .dropdown-toggle, .dropdown-menu.show span").filter(has_text="เปลี่ยนรหัสผ่าน").last
            await dropdown_pwd_btn.click(force=True, timeout=2000)
            await asyncio.sleep(2.0)
            
            pwd_fields = page.locator("input[type='password'], .modal-content input[type='password']")
            for i in range(2):
                await force_clear_input(page, pwd_fields.nth(i))
                await pwd_fields.nth(i).fill(new_password_str)
                await asyncio.sleep(0.1)
                
            await page.locator(".modal-content button:has-text('บันทึก'), .modal-footer button:has-text('บันทึก')").first.click()
            await asyncio.sleep(2.0)
            
            grey_close_btn = page.locator(".swal2-confirm, button:has-text('ปิด'), .btn-secondary:has-text('ปิด')").first
            if await grey_close_btn.is_visible(timeout=3000): await grey_close_btn.click()
    
            await force_click_manage_user(page)
            return "✅ สำเร็จ"
        except: return "ไม่พบข้อมูล"

# --- 4. TELEGRAM PACKET RECEIVER ---
async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    
    message_text = (st_text if (st_text := update.message.text) else "") + "\n" + (st_cap if (st_cap := update.message.caption) else "")
    message_text = message_text.strip()
    if not message_text: return
    
    chat_id = str(update.effective_chat.id)
    current_config = context.bot_data.get("config")
    
    target_web = None
    for k in ["w16", "zn"]:
        if chat_id == str(current_config[k]["chat_id"]):
            target_web = k
            break
    if not target_web or group_processing_status[target_web]: return

    clean_msg_for_nums = re.sub(r'[^\d\n]', ' ', message_text)
    all_found_numbers = re.findall(r'\d{8,12}', clean_msg_for_nums)
    
    target_bank_code = detect_bank_keyword(message_text)
    
    search_phone = None
    target_acc = None
    
    for num in all_found_numbers:
        if num.startswith("0") and len(num) in [9, 10]:
            if not search_phone:
                search_phone = num
            else:
                target_acc = num
        else:
            target_acc = num

    all_found_zabs = re.findall(r'ZAB\w+', message_text)
    target_username_from_chat = all_found_zabs[0] if all_found_zabs else None

    # โหมด ล้างเทิร์น
    is_clear_turn_cmd = any(kw in message_text for kw in ["ล้างเทิร์น", "ล้าง เทิร์น", "ลท"])
    if is_clear_turn_cmd:
        final_clear_user = target_username_from_chat
        
        if not final_clear_user:
            for num in all_found_numbers:
                if len(num) >= 7:
                    if target_web == "zn": final_clear_user = f"ZAB1LUY{num}"
                    else: final_clear_user = f"ZAB1AP8{num}"
                    break

        if final_clear_user:
            group_processing_status[target_web] = True
            try:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="⚡")])
            except: pass
            
            status_msg = await update.message.reply_text(f"⏳ [{target_web.upper()}] กำลังประมวลผลคำสั่งขอล้างเทิร์นของรหัสยูสเซอร์ {final_clear_user}... ⚙️")
            turn_result = await process_clear_turn_by_username(target_web, final_clear_user, current_config)
            
            if turn_result == "NOT_STUCK":
                try:
                    await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
                except: pass
                await update.message.reply_text("จากการตรวจสอบลูกค้าไม่ติดเทิร์นใดๆ แล้วนะคะ รบกวนลูกค้ารีระบบหน้าเว็บแล้วตรวจสอบอีกครั้งนะคะ")
            elif "✅" in turn_result or "สำเร็จ" in turn_result:
                try:
                    await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
                except: pass
                await update.message.reply_text(f"ล้างเทิร์นลูกค้า ยูสเซอร์ {final_clear_user} สำเร็จเรียบร้อยแล้วนะคะ 👌 OK")
            else:
                try:
                    await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="❌")])
                except: pass
                await update.message.reply_text(f"{turn_result}")
                
            try: await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            except: pass
            group_processing_status[target_web] = False
            return

    # โหมด เปลี่ยนเบอร์
    if "เปลี่ยนเบอร์" in message_text and search_phone:
        group_processing_status[target_web] = True 
        new_phone = all_found_numbers[-1] if len(all_found_numbers) > 1 and all_found_numbers[-1].startswith("0") else search_phone

        try:
            await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="⚡")])
        except: pass

        status_msg = await update.message.reply_text(f"⏳ [{target_web.upper()}] กำลังดำเนินการเปลี่ยนเบอร์ค้นจากเบอร์ {search_phone}... ⚙️")
        edit_result = await change_user_phone(target_web, search_phone, target_acc, target_bank_code, new_phone, current_config)
        
        if "✅" in edit_result or "สำเร็จ" in edit_result:
            try:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except: pass
            final_msg = await update.message.reply_text(f"เปลี่ยนเบอร์สำเร็จ 👌 OK\n\n👉ยูสเซอร์ : {new_phone}\n👉ลูกค้าใช้รหัสผ่านเดิมในการเข้าเล่นได้เลยนะคะ\n( น้องแนะนำให้เข้าจากเบราเซอร์ Google Chrome นะคะ)❤️")
            asyncio.create_task(delay_and_mask_message(context, chat_id, final_msg.message_id, search_phone, mask_phone_number(new_phone)))
        else:
            try:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="❌")])
            except: pass
            await update.message.reply_text(f"{edit_result}")
        
        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except: pass
        group_processing_status[target_web] = False
        return

    # โหมด เปลี่ยนรหัส/รีรหัสผ่าน
    is_reset_password_cmd = any(kw in message_text for kw in ["เปลี่ยนรหัส", "รีรหัส", "รี รหัส", "ลืมรหัสผ่าน"])
    if is_reset_password_cmd and search_phone:
        group_processing_status[target_web] = True
        try:
            await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="⚡")])
        except: pass

        status_msg = await update.message.reply_text(f"⏳ [{target_web.upper()}] กำลังประมวลผลเปลี่ยนรหัสผ่าน... ⚙️")
        pwd_result = await change_user_password_fast(target_web, search_phone, target_acc, target_bank_code, current_config)
        
        if "✅" in pwd_result or "สำเร็จ" in pwd_result:
            try:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="👍")])
            except: pass
            custom_pwd = generate_middle_aa_password(search_phone)
            await update.message.reply_text(
                f"🎮 เข้าเล่นทางยูสเซอร์นี้ได้เลยนะคะ \n"
                f"👤ยูสเซอร์ : {search_phone}\n"
                f"🔒รหัสผ่าน : {custom_pwd}\n\n"
                f"น้องแนะนำให้เข้าเล่นผ่าน 🌐 Google Chrome 🌐 เพื่อประสิทธิภาพระบบที่เสถียรยิ่งขึ้นนะคะ 👌 OK"
            )
        else:
            try:
                await update.message.set_reaction(reaction=[ReactionTypeEmoji(emoji="❌")])
            except: pass
            await update.message.reply_text(f"{pwd_result}")
        
        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except: pass
        group_processing_status[target_web] = False
        return

    # โหมด ขอเบอร์
    if "ขอเบอร์" in message_text and search_phone:
        group_processing_status[target_web] = True
        status_msg = await update.message.reply_text(f"⏳ [{target_web.upper()}] กำลังตรวจสอบหาเบอร์โทรศัพท์... ⚙️")
        phone_result = await search_phone_number(target_web, search_phone, current_config)
        
        if phone_result == "ไม่พบข้อมูล" or "❌" in phone_result: 
            await update.message.reply_text(f"{phone_result}")
        else:
            final_msg = await update.message.reply_text(f"📱 **ข้อมูลสถานะเบอร์โทรศัพท์ลูกค้า (มีเวลาดู 5 นาที)**\n📞 เบอร์โทรศัพท์: {phone_result}\n🔒 ตรวจสอบความปลอดภัยผ่านแล้ว 👌 OK")
            async def hide_search_msg():
                await asyncio.sleep(300)
                try: await context.bot.edit_message_text(chat_id=chat_id, message_id=final_msg.message_id, text=f"📱 **ข้อมูลเบอร์โทรศัพท์ลูกค้า (หมดเวลาดู)**\n📞 เบอร์โทรศัพท์: {mask_phone_number(phone_result)}")
                except: pass
            asyncio.create_task(hide_search_msg())
        
        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except: pass
        group_processing_status[target_web] = False
        return

    # โหมด ขอยูส
    if "ขอยูส" in message_text and search_phone:
        group_processing_status[target_web] = True
        status_msg = await update.message.reply_text(f"⏳ [{target_web.upper()}] กำลังตรวจสอบค้นหายูสเซอร์... ⚙️")
        user_result = await search_username_by_phone(target_web, search_phone, current_config)
        
        if user_result == "ไม่พบข้อมูล" or "❌" in user_result: 
            await update.message.reply_text(f"{user_result}")
        else: 
            await update.message.reply_text(f"👤 **ข้อมูลรหัสยูสเซอร์ของลูกค้า**\n📞 เบอร์โทรศัพท์: {search_phone}\n🆔 รหัสผู้ใช้ (User): {user_result} 👌 OK")
        
        try: await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except: pass
        group_processing_status[target_web] = False
        return

# --- 5. SYSTEM KERNEL ---
async def run_bot_async(config_data, telegram_token):
    global global_browser_instance, telegram_app_instance
    app = Application.builder().token(telegram_token).build()
    telegram_app_instance = app
    app.bot_data["config"] = config_data
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Caption) & (~filters.COMMAND), handle_telegram_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        global_browser_instance = browser
        for k in config_data.keys():
            if not config_data[k]["url"]: continue
            search_ctx = await browser.new_context()
            search_pages[k] = await search_ctx.new_page()
            asyncio.create_task(login_search_backend(web_key=k, config_data=config_data))
            pwd_ctx = await browser.new_context()
            password_pages[k] = await pwd_ctx.new_page()
            asyncio.create_task(login_password_backend(web_key=k, config_data=config_data))
        while not bot_stop_signal.is_set(): await asyncio.sleep(0.5)

def launch_bot(config_data, telegram_token):
    global global_live_config
    global_live_config = config_data
    bot_stop_signal.clear()
    asyncio.run(run_bot_async(config_data, telegram_token))

def stop_bot_and_kill_chrome():
    global search_pages, password_pages
    bot_stop_signal.set()
    async def kill_all():
        try:
            if global_browser_instance: await global_browser_instance.close()
            if telegram_app_instance:
                await telegram_app_instance.updater.stop()
                await telegram_app_instance.stop()
        except: pass
    loop = asyncio.new_event_loop()
    loop.run_until_complete(kill_all())
    loop.close()
    search_pages = {"w16": None, "zn": None}
    password_pages = {"w16": None, "zn": None}

# --- 6. STREAMLIT FRONTEND UI ---
st.set_page_config(page_title="MATRIX SYSTEM CENTRAL", layout="centered")

st.markdown("""<style>
.stApp { 
    background-color: #050a07 !important;
} 
h1, h2, h3, p, label { 
    color: #2ecc71 !important; 
    font-weight: bold !important;
    text-shadow: none !important;
} 
.stTabs [data-baseweb="tab-list"] { 
    background-color: #0b140f; 
    padding: 10px; 
    border-radius: 15px;
} 
.glass-card { 
    background: #0e1a14 !important; 
    border: 1px solid #2ecc71; 
    padding: 25px; 
    border-radius: 20px;
    box-shadow: none !important;
    margin-top: 20px; 
} 
.stTextInput input { 
    background-color: #16261e !important; 
    color: #2ecc71 !important; 
    font-weight: bold;
    border: 1px solid #2ecc71 !important;
    border-radius: 10px; 
}
</style>""", unsafe_allow_html=True)

st.title("⚡ FLAT GREEN SECURITY CONTROL v21.0")
if "active" not in st.session_state: st.session_state.active = False

tab1, tab2, tab3 = st.tabs(["🕹️ ควบคุมระบบ", "🔑 หลังบ้าน & ไอดีพนักงาน", "💬 แก้ไขแชทไอดีกลุ่ม"])

with tab1:
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.header("🕹️ OPERATIONAL CONTROL")
    status = "🟢 ONLINE" if st.session_state.active else "🔴 OFFLINE"
    st.subheader(f"BOT STATUS: {status}")
    
    # ✅ ย้ายกล่องกรอก Token มาให้ตั้งค่าได้แบบอิสระ เลือกสลับบอทได้ที่นี่หน้างานเลยครับ
    bot_token_input = st.text_input("🤖 TELEGRAM BOT TOKEN", value="8723484793:AAHPl1LEEf7PBJtG0XhbDAbvW9NbWeZRV78", type="password", key="tg_bot_token")
    
    if st.button("▶️ START BOT", use_container_width=True, type="primary"):
        if not st.session_state.active and bot_token_input:
            st.session_state.active = True
        
            live_cfg = {
                "w16": {"url": st.session_state.w16_url, "user": st.session_state.w16_user, "pass": st.session_state.w16_pass, "pwd_user": st.session_state.pwd_user, "pwd_pass": st.session_state.pwd_pass, "chat_id": st.session_state.w16_chat},
                "zn": {"url": st.session_state.zn_url, "user": st.session_state.zn_user, "pass": st.session_state.zn_pass, "pwd_user": st.session_state.pwd_user, "pwd_pass": st.session_state.pwd_pass, "chat_id": st.session_state.zn_chat}
            }
            threading.Thread(target=launch_bot, args=(live_cfg, bot_token_input), daemon=True).start()
            st.rerun()
    if st.button("🛑 STOP BOT", use_container_width=True):
        if st.session_state.active:
            stop_bot_and_kill_chrome()
            st.session_state.active = False
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

with tab2:
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.header("🔑 BACKEND DATABASE SETUP")
    st.text_input("ไอดีพิเศษเปลี่ยนพาส", value="NumberBot", key="pwd_user")
    st.text_input("รหัสผ่านพิเศษเปลี่ยนพาส", value="bb123456", type="password", key="pwd_pass")
    st.divider()
    
    st.subheader("🟢 ค่าย W16")
    # ✅ ลบค่าเริ่มต้นออกทั้งหมดแล้ว ปล่อยว่างไว้ให้คุณมากรอกเองได้แบบคลีนๆ ครับ
    st.text_input("ลิงก์รายงาน W16", value="", key="w16_url", placeholder="วางลิงก์รายงาน W16 ที่นี่...")
    st.text_input("ไอดีพนักงาน W16", value="", key="w16_user", placeholder="กรอกไอดีพนักงาน...")
    st.text_input("รหัสพนักงาน W16", value="", type="password", key="w16_pass", placeholder="กรอกรหัสผ่าน...")
    st.divider()
    
    st.subheader("🔵 ค่าย ZN")
    # ✅ ลบค่าเริ่มต้นออกตามสั่ง ปล่อยว่างพร้อมให้คีย์ข้อมูลใหม่หน้างานครับ
    st.text_input("ลิงก์รายงาน ZN", value="", key="zn_url", placeholder="วางลิงก์รายงาน ZN ที่นี่...")
    st.text_input("ไอดีพนักงาน ZN", value="", key="zn_user", placeholder="กรอกไอดีพนักงาน...")
    st.text_input("รหัสพนักงาน ZN", value="", type="password", key="zn_pass", placeholder="กรอกรหัสผ่าน...")
    st.markdown('</div>', unsafe_allow_html=True)

with tab3:
    st.markdown('<div class="glass-card">', unsafe_allow_html=True)
    st.header("💬 TELEGRAM GROUP CHAT IDS")
    st.text_input("Chat ID: W16 กลุ่มแชท", value="-1004479946676", key="w16_chat")
    st.text_input("Chat ID: ZN กลุ่มแชท", value="-1003751218313", key="zn_chat")
    st.markdown('</div>', unsafe_allow_html=True)

st.caption("Flat Green Security Framework v21.0 - Matrix Dynamic-Token Configured")