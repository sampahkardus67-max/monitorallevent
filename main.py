#!/usr/bin/env python3
"""
JKT48 Ticket & Show Monitor with Telegram Interactive Commands & Member Query Filtering
Monitors:
1. Exclusives (PHOTOCARD, TWO_SHOT, DIGITAL_PHOTOBOOK) - Purchases & Restocks
2. Shows (type=SHOW) - Performing Members Announcement Alerts

Interactive Commands (only responds to configured CHAT_ID):
- "vc [query]" / "/vc [query]": Returns Digital Photobook (video call) data.
- "pc [query]" / "/pc [query]" / "photocard [query]": Returns Photocard data.
- "2s [query]" / "/2s [query]" / "2shot [query]": Returns Two Shot data.
- "pb [query]" / "/pb [query]" / "photobook [query]": Returns Digital Photobook data.
- "show [query]" / "/show [query]": Returns theater show details and performing member names (filtered by show title, team, or member name).
"""

import os
import sys
import time
import requests
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

# Reconfigure stdout/stderr encoding for UTF-8 support on Windows terminals
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Configurations from Environment Variables
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL       = int(os.environ.get("MONITOR_INTERVAL", "5"))
HEARTBEAT_H    = int(os.environ.get("HEARTBEAT_HOURS", "6"))
MAX_FAIL       = 5
WATCH_MEMBERS  = [] # Add member names here to filter exclusives if desired

# Cache settings
LIST_CACHE_TTL = 300  # Refresh list of exclusives & shows every 5 minutes

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9",
    "Referer": "https://jkt48.com/",
    "Origin": "https://jkt48.com",
}

# Member Nicknames mapping to official JKT48 database names
MEMBER_NICKNAMES = {
    "nala": ["shabilqis naila"],
    "levi": ["michelle levia"],
    "freya": ["freya jayawardana"],
    "fiony": ["fiony alveria"],
    "gracia": ["shania gracia"],
    "indah": ["indah cahya"],
    "gracie": ["grace octaviani"],
    "fritzy": ["fritzy rosmerian"],
    "alya": ["alya amanda"],
    "lana": ["aurhel alana"],
    "aurhel": ["aurhel alana"],
    "celline": ["celline thefani"],
    "ralyne": ["ralyne van irwan"],
    "michelle": ["michelle alexandra", "michelle levia"]
}

# Global cache variables shared between the monitor loop and the telegram responder thread
active_exclusives = []
active_shows = []
exc_details = {}
show_details = {}
cache_lock = threading.Lock()

# WIB Timezone Helpers (UTC+7)
def wib():
    return datetime.now(timezone(timedelta(hours=7)))

def wib_str():
    return wib().strftime("%Y-%m-%d %H:%M:%S WIB")

def parse_wib_datetime(date_str, time_str):
    """
    Parses a date and time string into a timezone-aware WIB datetime object.
    Robustly handles various database date and time formats.
    """
    only_date = date_str[:10]
    time_part = time_str.strip()
    if len(time_part) == 5:
        time_part += ":00"
    dt = datetime.strptime(f"{only_date} {time_part}", "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone(timedelta(hours=7)))

def format_date_str(date_str):
    # date_str is e.g. "2026-06-28" or "2026-06-28T17:00:00.000Z"
    only_date = date_str[:10]
    try:
        parts = only_date.split("-")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    except Exception:
        pass
    return only_date

# Nickname matching logic
def matches_member_query(member_name, query):
    if not query:
        return True
    m_name_lower = member_name.lower()
    q_lower = query.lower().strip()
    
    # Get mapped official names for nickname or default to searching query itself
    targets = MEMBER_NICKNAMES.get(q_lower, [q_lower])
        
    for target in targets:
        if target in m_name_lower:
            return True
    return False

# Telegram Notifier
def telegram(msg, target_chat_id=None):
    to_chat = target_chat_id or CHAT_ID
    if not BOT_TOKEN or not to_chat:
        return print("❌ Telegram Token/Chat ID belum diset!")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": to_chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
        r.raise_for_status()
        print(f"  ✅ Telegram terkirim ke {to_chat}")
    except Exception as e:
        print(f"  ❌ Gagal kirim Telegram ke {to_chat}: {e}")

# Split and send long messages to handle Telegram limit (4096 chars)
def send_long_telegram_message(target_chat_id, msg):
    chunks = []
    current_chunk = ""
    for paragraph in msg.split("\n\n"):
        if len(current_chunk) + len(paragraph) + 2 > 4000:
            chunks.append(current_chunk.strip())
            current_chunk = paragraph + "\n\n"
        else:
            current_chunk += paragraph + "\n\n"
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    for chunk in chunks:
        telegram(chunk, target_chat_id=target_chat_id)

# Fetch List of Exclusives
def fetch_exclusives_list():
    url = "https://jkt48.com/api/v1/exclusives?lang=id"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") and "data" in data:
            target_categories = {"PHOTOCARD", "TWO_SHOT", "DIGITAL_PHOTOBOOK"}
            filtered = [
                item for item in data["data"]
                if item.get("category") in target_categories
            ]
            return filtered
    except Exception as e:
        print(f"  ⚠ Gagal fetch list exclusives: {e}")
    return []

# Fetch List of Shows for Current and Next Month
def fetch_shows_list():
    now = wib()
    months_to_check = []
    
    months_to_check.append((now.month, now.year))
    if now.month == 12:
        months_to_check.append((1, now.year + 1))
    else:
        months_to_check.append((now.month + 1, now.year))
        
    shows = []
    for month, year in months_to_check:
        url = f"https://jkt48.com/api/v1/schedules?lang=id&month={month}&year={year}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("status") and "data" in data:
                month_shows = [
                    item for item in data["data"]
                    if item.get("type") == "SHOW"
                ]
                shows.extend(month_shows)
        except Exception as e:
            print(f"  ⚠ Gagal fetch list show untuk {month}/{year}: {e}")
            
    unique_shows = {}
    for s in shows:
        sid = s.get("schedule_id")
        if sid:
            unique_shows[sid] = s
            
    return list(unique_shows.values())

# Fetch Single Exclusive Details
def fetch_exclusive_details(code):
    url = f"https://jkt48.com/api/v1/exclusives/{code}/bonus?lang=id"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") and "data" in data:
            return data["data"]
    except Exception:
        pass
    return None

# Fetch Theater Show Details (Performing members)
def fetch_theater_show_details(reference_code):
    url = f"https://jkt48.com/api/v1/theater-shows/{reference_code}?lang=id"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") and "data" in data:
            return data["data"]
    except Exception:
        pass
    return None

# Concurrently fetch details of active exclusives
def fetch_all_active_data(exclusives):
    exc_results = {}
    
    def fetch_exc(exc):
        code = exc["code"]
        res = fetch_exclusive_details(code)
        return code, res

    with ThreadPoolExecutor(max_workers=5) as executor:
        exc_futures = [executor.submit(fetch_exc, e) for e in exclusives]
        
        for f in exc_futures:
            code, res = f.result()
            if res is not None:
                exc_results[code] = res
                
    return exc_results

# Interactive Telegram Commands Handlers
def handle_exclusive_by_category(target_chat_id, category, query):
    global active_exclusives
    
    with cache_lock:
        excs = list(active_exclusives)
        
    if not excs:
        excs = fetch_exclusives_list()
        
    filtered_excs = [e for e in excs if e.get("category") == category]
    
    cat_label = {
        "DIGITAL_PHOTOBOOK": "DIGITAL PHOTOBOOK (VIDEO CALL)",
        "PHOTOCARD": "PHOTOCARD (MEET & GREET)",
        "TWO_SHOT": "TWO SHOT (2S)"
    }.get(category, category)
    
    if not filtered_excs:
        telegram(f"ℹ️ <b>Tidak ada event {cat_label} aktif saat ini.</b>", target_chat_id)
        return
        
    response_msg = f"🃏 <b>DATA {cat_label}</b>\n"
    if query:
        response_msg += f"🔍 Pencarian member: \"{query}\"\n"
    response_msg += "\n"
    
    current_time = wib()
    found_any = False
    
    for e in filtered_excs:
        code = e["code"]
        title = e.get("title", cat_label)
        
        sessions = fetch_exclusive_details(code)
        if not sessions:
            continue
            
        event_msg = f"<b>{title}</b>\n"
        event_has_sessions = False
        
        for s in sessions:
            try:
                s_time = s.get("end_time") or s.get("start_time") or "23:59:59"
                session_dt = parse_wib_datetime(s["date"], s_time)
                if session_dt < current_time:
                    continue
            except Exception:
                pass
                
            label = s.get("label", "?")
            stime = s.get("start_time", "")[:5]
            s_date = s.get("date", "")[:10]
            s_date_formatted = format_date_str(s_date)
            
            member_lines = []
            for m in s.get("session_members", []):
                name = m.get("member_name", "")
                jalur = m.get("label", "")
                quota = m.get("quota", 0)
                price = m.get("price", 0)
                
                # Check member query filter
                if query and not matches_member_query(name, query):
                    continue
                    
                if WATCH_MEMBERS and name not in WATCH_MEMBERS:
                    continue
                    
                icon = "🔴" if quota == 0 else "🟢"
                member_lines.append(f"• {name} ({jalur}) | {icon} Sisa: {quota} | Rp{price:,}")
                
            if member_lines:
                event_has_sessions = True
                found_any = True
                event_msg += f"📅 {s_date_formatted} | 📋 {label} ({stime} WIB):\n" + "\n".join(member_lines) + "\n\n"
                
        if event_has_sessions:
            response_msg += event_msg
            
    if not found_any:
        telegram(f"ℹ️ <b>Tidak ada sesi mendatang untuk {cat_label} yang cocok dengan pencarian Anda.</b>", target_chat_id)
    else:
        send_long_telegram_message(target_chat_id, response_msg)

def handle_show_command(target_chat_id, query):
    global active_shows, show_details
    
    with cache_lock:
        shows = list(active_shows)
        
    current_time = wib()
    future_shows = []
    
    for s in shows:
        try:
            t_str = s.get("end_time") or s.get("start_time") or "23:59:59"
            show_dt = parse_wib_datetime(s["date"], t_str)
            if show_dt >= current_time:
                future_shows.append((s, show_dt))
        except Exception:
            future_shows.append((s, None))
            
    if not future_shows:
        telegram("ℹ️ <b>Tidak ada jadwal Theater Show aktif mendatang saat ini.</b>", target_chat_id)
        return
        
    # Sort shows by date
    future_shows.sort(key=lambda x: x[1] if x[1] else datetime.max.replace(tzinfo=timezone(timedelta(hours=7))))
    
    response_msg = "🎭 <b>JADWAL THEATER SHOW MENDATANG</b>\n"
    if query:
        response_msg += f"🔍 Pencarian show: \"{query}\"\n"
    response_msg += "\n"
    
    found_any = False
    
    for s, show_dt in future_shows:
        sid = s["schedule_id"]
        ref_code = s.get("reference_code", "")
        title = s.get("title", "Show Theater")
        date_str = s.get("date", "")[:10]
        stime = s.get("start_time", "")[:5]
        team = s.get("jkt48_member_type") or "TBA"
        birthday = s.get("birthday_member")
        
        # Fetch detailed info (either from global cache or fetch fresh)
        with cache_lock:
            details = show_details.get(ref_code)
            
        if not details and ref_code:
            details = fetch_theater_show_details(ref_code)
            
        members_str = "Belum diumumkan"
        members = []
        if details:
            members = [m.get("name", "") for m in details.get("jkt48_member", []) if m.get("name")]
            if members:
                members_str = ", ".join(members)
                
        # Match query with title, team, birthday, or performing member names
        if query:
            q_lower = query.lower().strip()
            title_lower = title.lower()
            team_lower = team.lower()
            birthday_lower = (birthday or "").lower()
            members_lower = members_str.lower()
            
            # Check nickname mappings for show member search as well
            matches_member = False
            for target in MEMBER_NICKNAMES.get(q_lower, [q_lower]):
                if target in members_lower:
                    matches_member = True
                    break
            
            if (q_lower not in title_lower and 
                q_lower not in team_lower and 
                q_lower not in birthday_lower and 
                not matches_member):
                continue
                
        found_any = True
        show_info = f"<b>{title}</b>\n"
        show_info += f"📅 {format_date_str(date_str)} ({stime} WIB)\n"
        show_info += f"👥 Cast/Team: {team}\n"
        if birthday:
            show_info += f"🎂 Birthday Show: {birthday}\n"
        show_info += f"👥 Member Tampil:\n<i>{members_str}</i>\n"
        
        purchase_url = f"https://jkt48.com/theater/schedule/id/{sid}?lang=id"
        show_info += f"🔗 <a href='{purchase_url}'>Beli Tiket →</a>\n\n"
        
        response_msg += show_info
        
    if not found_any:
        telegram("ℹ️ <b>Tidak ada jadwal Theater Show yang cocok dengan pencarian Anda.</b>", target_chat_id)
    else:
        send_long_telegram_message(target_chat_id, response_msg)

def get_exclusive_sessions(code):
    global exc_details
    with cache_lock:
        sessions = exc_details.get(code)
    if not sessions:
        sessions = fetch_exclusive_details(code)
    return sessions or []

def get_member_exclusives(category, query):
    global active_exclusives
    with cache_lock:
        excs = list(active_exclusives)
    if not excs:
        excs = fetch_exclusives_list()
        
    filtered_excs = [e for e in excs if e.get("category") == category]
    
    current_time = wib()
    results = []
    
    for e in filtered_excs:
        code = e["code"]
        title = e.get("title", category)
        
        sessions = get_exclusive_sessions(code)
        
        for s in sessions:
            try:
                s_time = s.get("end_time") or s.get("start_time") or "23:59:59"
                session_dt = parse_wib_datetime(s["date"], s_time)
                if session_dt < current_time:
                    continue
            except Exception:
                pass
                
            label = s.get("label", "?")
            stime = s.get("start_time", "")[:5]
            s_date = s.get("date", "")[:10]
            s_date_formatted = format_date_str(s_date)
            
            for m in s.get("session_members", []):
                name = m.get("member_name", "")
                jalur = m.get("label", "")
                quota = m.get("quota", 0)
                price = m.get("price", 0)
                
                if matches_member_query(name, query):
                    icon = "🔴" if quota == 0 else "🟢"
                    results.append({
                        "title": title,
                        "date": s_date_formatted,
                        "label": label,
                        "stime": stime,
                        "jalur": jalur,
                        "quota": quota,
                        "price": price,
                        "icon": icon
                    })
    return results

def get_member_shows(query):
    global active_shows, show_details
    with cache_lock:
        shows = list(active_shows)
        
    current_time = wib()
    results = []
    
    for s in shows:
        try:
            t_str = s.get("end_time") or s.get("start_time") or "23:59:59"
            show_dt = parse_wib_datetime(s["date"], t_str)
            if show_dt < current_time:
                continue
        except Exception:
            pass
            
        sid = s["schedule_id"]
        ref_code = s.get("reference_code", "")
        title = s.get("title", "Show Theater")
        date_str = s.get("date", "")[:10]
        stime = s.get("start_time", "")[:5]
        team = s.get("jkt48_member_type") or "TBA"
        birthday = s.get("birthday_member")
        
        with cache_lock:
            details = show_details.get(ref_code)
            
        if not details and ref_code:
            details = fetch_theater_show_details(ref_code)
            
        members = []
        if details:
            members = [m.get("name", "") for m in details.get("jkt48_member", []) if m.get("name")]
            
        members_str = ", ".join(members) if members else "Belum diumumkan"
        
        matches_member = False
        q_lower = query.lower().strip()
        members_lower = members_str.lower()
        
        for target in MEMBER_NICKNAMES.get(q_lower, [q_lower]):
            if target in members_lower:
                matches_member = True
                break
                
        if matches_member:
            purchase_url = f"https://jkt48.com/theater/schedule/id/{sid}?lang=id"
            results.append({
                "title": title,
                "date": format_date_str(date_str),
                "stime": stime,
                "team": team,
                "birthday": birthday,
                "members_str": members_str,
                "purchase_url": purchase_url
            })
    return results

def handle_full_data_command(target_chat_id, query):
    if not query:
        telegram("ℹ️ <b>Format salah. Contoh penggunaan:</b> <code>full data nala</code> atau <code>full data Levi</code>", target_chat_id)
        return
        
    q_lower = query.lower().strip()
    official_names = MEMBER_NICKNAMES.get(q_lower, [])
    resolved_name = official_names[0].title() if official_names else query.title()
    
    vcs = get_member_exclusives("DIGITAL_PHOTOBOOK", query)
    pcs = get_member_exclusives("PHOTOCARD", query)
    tss = get_member_exclusives("TWO_SHOT", query)
    shows = get_member_shows(query)
    
    if not vcs and not pcs and not tss and not shows:
        telegram(f"ℹ️ <b>Tidak ada data event mendatang untuk member: \"{resolved_name}\"</b>", target_chat_id)
        return
        
    msg = f"🌟 <b>FULL DATA: {resolved_name.upper()}</b> 🌟\n\n"
    
    # 1. Video Call (DIGITAL_PHOTOBOOK)
    msg += "📹 <b>DIGITAL PHOTOBOOK (VIDEO CALL)</b>\n"
    if vcs:
        for v in vcs:
            msg += f"• <b>{v['title']}</b>\n  📅 {v['date']} | 📋 {v['label']} ({v['stime']} WIB)\n  🚪 {v['jalur']} | {v['icon']} Sisa: {v['quota']} | Rp{v['price']:,}\n"
    else:
        msg += "<i>Tidak ada sesi video call mendatang.</i>\n"
    msg += "\n"
    
    # 2. Meet & Greet (PHOTOCARD)
    msg += "🤝 <b>PHOTOCARD (MEET & GREET)</b>\n"
    if pcs:
        for p in pcs:
            msg += f"• <b>{p['title']}</b>\n  📅 {p['date']} | 📋 {p['label']} ({p['stime']} WIB)\n  🚪 {p['jalur']} | {p['icon']} Sisa: {p['quota']} | Rp{p['price']:,}\n"
    else:
        msg += "<i>Tidak ada sesi meet & greet mendatang.</i>\n"
    msg += "\n"
    
    # 3. Two Shot
    msg += "📸 <b>TWO SHOT (2S)</b>\n"
    if tss:
        for t in tss:
            msg += f"• <b>{t['title']}</b>\n  📅 {t['date']} | 📋 {t['label']} ({t['stime']} WIB)\n  🚪 {t['jalur']} | {t['icon']} Sisa: {t['quota']} | Rp{t['price']:,}\n"
    else:
        msg += "<i>Tidak ada sesi two shot mendatang.</i>\n"
    msg += "\n"
    
    # 4. Theater Shows
    msg += "🎭 <b>THEATER SHOWS</b>\n"
    if shows:
        for s in shows:
            msg += f"• <b>{s['title']}</b>\n  📅 {s['date']} ({s['stime']} WIB) | Cast: {s['team']}\n"
            if s['birthday']:
                msg += f"  🎂 Birthday: {s['birthday']}\n"
            msg += f"  🔗 <a href='{s['purchase_url']}'>Beli Tiket →</a>\n"
    else:
        msg += "<i>Tidak ada jadwal teater mendatang.</i>\n"
    msg += "\n"
    
    msg += f"🕐 <i>Data per: {wib_str()}</i>"
    
    send_long_telegram_message(target_chat_id, msg)

# Telegram Updates Poller (Runs in background thread)
def poll_telegram_updates():
    offset = None
    print("🤖 Telegram interactivity polling started...")
    
    while True:
        if not BOT_TOKEN or not CHAT_ID:
            time.sleep(10)
            continue
            
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
                
            r = requests.get(url, params=params, timeout=35)
            r.raise_for_status()
            data = r.json()
            
            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue
                        
                    chat = message.get("chat", {})
                    chat_id = str(chat.get("id", ""))
                    
                    # Security boundary: Only respond to the configured Telegram Chat ID
                    if chat_id != str(CHAT_ID):
                        continue
                        
                    text = message.get("text", "").strip()
                    if not text:
                        continue
                        
                    # Helper to strip slash
                    clean_text = text
                    if clean_text.startswith("/"):
                        clean_text = clean_text[1:]
                    clean_text_lower = clean_text.lower().strip()
                    
                    print(f"🤖 Telegram menerima pesan: '{text}' | Dari {chat_id}")
                    
                    # Match commands
                    if clean_text_lower.startswith("full data"):
                        query = clean_text[9:].strip()
                        handle_full_data_command(chat_id, query)
                    elif clean_text_lower.startswith("meet and greet"):
                        query = clean_text[14:].strip()
                        handle_exclusive_by_category(chat_id, "PHOTOCARD", query)
                    elif clean_text_lower.startswith("meet & greet"):
                        query = clean_text[12:].strip()
                        handle_exclusive_by_category(chat_id, "PHOTOCARD", query)
                    elif clean_text_lower.startswith("two shot"):
                        query = clean_text[8:].strip()
                        handle_exclusive_by_category(chat_id, "TWO_SHOT", query)
                    elif clean_text_lower.startswith("2 shot"):
                        query = clean_text[6:].strip()
                        handle_exclusive_by_category(chat_id, "TWO_SHOT", query)
                    elif clean_text_lower.startswith("video call"):
                        query = clean_text[10:].strip()
                        handle_exclusive_by_category(chat_id, "DIGITAL_PHOTOBOOK", query)
                    elif clean_text_lower.startswith("digital photobook"):
                        query = clean_text[17:].strip()
                        handle_exclusive_by_category(chat_id, "DIGITAL_PHOTOBOOK", query)
                    else:
                        parts = clean_text.split(maxsplit=1)
                        cmd = parts[0].lower()
                        query = parts[1] if len(parts) > 1 else ""
                        
                        if cmd in ["vc", "pb", "photobook", "videocall"]:
                            handle_exclusive_by_category(chat_id, "DIGITAL_PHOTOBOOK", query)
                        elif cmd in ["pc", "mng", "photocard", "meetandgreet"]:
                            handle_exclusive_by_category(chat_id, "PHOTOCARD", query)
                        elif cmd in ["2s", "2shot", "twoshot"]:
                            handle_exclusive_by_category(chat_id, "TWO_SHOT", query)
                        elif cmd in ["show"]:
                            handle_show_command(chat_id, query)
                        
        except Exception as e:
            print(f"🤖 [Warning] Poller Telegram mengalami error: {e}")
            time.sleep(5)

# Periodic Heartbeat Reporter
def heartbeat(active_excs, active_shs, exc_dets, show_dets, run_count, last_hb):
    if last_hb and (wib() - last_hb).total_seconds() < HEARTBEAT_H * 3600:
        return last_hb
        
    now = wib()
    
    # Stats for exclusives
    exc_total_slots = 0
    exc_avail_slots = 0
    for code, sessions in exc_dets.items():
        for s in sessions:
            for m in s.get("session_members", []):
                exc_total_slots += 1
                if m.get("quota", 0) > 0:
                    exc_avail_slots += 1
                    
    # Stats for shows
    show_total = len(active_shs)
    show_with_members = sum(1 for d in show_dets.values() if d and d.get("jkt48_member"))
    
    msg = (
        f"💓 <b>Laporan Berkala JKT48 Monitor</b>\n\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M WIB')} | ⚡ Interval: {INTERVAL}s\n"
        f"📈 Run Count: {run_count:,}x\n\n"
        f"🃏 <b>Exclusives Monitored:</b> {len(active_excs)}\n"
        f"└─ Total Slots: {exc_total_slots} | Tersedia: {exc_avail_slots}\n\n"
        f"🎭 <b>Theater Shows Monitored:</b> {show_total}\n"
        f"└─ Dengan Member Diumumkan: {show_with_members}\n\n"
        f"⏰ Heartbeat berikutnya: {(now + timedelta(hours=HEARTBEAT_H)).strftime('%H:%M WIB')}"
    )
    telegram(msg)
    print("   Heartbeat terkirim")
    return now

def main():
    global active_exclusives, active_shows, exc_details, show_details
    
    print(f"{'='*60}\nJKT48 Ticket & Show Monitor | Interval: {INTERVAL}s\n{'='*60}")
    
    # Start the interactive Telegram long poller in the background
    poller_thread = threading.Thread(target=poll_telegram_updates, daemon=True)
    poller_thread.start()
    
    # Cache management variables
    last_list_fetch = 0
    prev_quota = {}
    prev_show_members = {} # Tracks: reference_code -> list of member names
    is_first_run = True
    
    run_count = 0
    fail_count = 0
    last_hb = wib()
    
    telegram(
        f"🚀 <b>JKT48 Monitor Aktif!</b>\n"
        f"Memantau exclusives & theater show\n"
        f"Cek setiap: <b>{INTERVAL}s</b>\n"
        f"Interactive mode aktif (vc / pc / 2s / show)\n"
        f"Waktu mulai: 🕐 {wib_str()}"
    )
    
    while True:
        now_ts = time.time()
        
        # 1. Update cache of active lists if expired
        if now_ts - last_list_fetch > LIST_CACHE_TTL:
            print(f"\n[{wib().strftime('%H:%M:%S')}] Memperbarui cache daftar exclusives & shows...")
            
            all_excs = fetch_exclusives_list()
            all_shows = fetch_shows_list()
            
            current_time = wib()
            filtered_shows = []
            
            for s in all_shows:
                try:
                    t_str = s.get("end_time") or s.get("start_time") or "23:59:59"
                    show_dt = parse_wib_datetime(s["date"], t_str)
                    if show_dt >= current_time:
                        filtered_shows.append(s)
                except Exception:
                    filtered_shows.append(s)
                    
            with cache_lock:
                active_exclusives = all_excs
                active_shows = filtered_shows
                
            last_list_fetch = now_ts
            print(f"   Cache diperbarui: {len(active_exclusives)} exclusives | {len(active_shows)} shows aktif mendatang")
            
        # 2. Poll detailed data
        run_count += 1
        print(f"[{wib().strftime('%H:%M:%S')}] Cek #{run_count}...", end=" ", flush=True)
        
        with cache_lock:
            excs_snapshot = list(active_exclusives)
            shows_snapshot = list(active_shows)
            
        exc_dets = fetch_all_active_data(excs_snapshot)
        
        # Poll show details (only for unannounced shows to save API bandwidth)
        show_dets = {}
        shows_api_errors = 0
        shows_ok = 0
        
        for s in shows_snapshot:
            ref_code = s.get("reference_code", "")
            if not ref_code:
                continue
                
            # If already announced and cached, reuse it
            if ref_code in prev_show_members and prev_show_members[ref_code]:
                show_dets[ref_code] = {
                    "jkt48_member": [{"name": name} for name in prev_show_members[ref_code]]
                }
                shows_ok += 1
                continue
                
            # Otherwise fetch (polite 0.2s delay on first run)
            if is_first_run:
                time.sleep(0.2)
                
            details = fetch_theater_show_details(ref_code)
            if details is not None:
                show_dets[ref_code] = details
                shows_ok += 1
            else:
                shows_api_errors += 1
                
        # Share details with global cache
        with cache_lock:
            exc_details = exc_dets
            show_details = show_dets
            
        # Check if exclusives poll failed entirely when exclusives are active
        if not exc_dets and len(excs_snapshot) > 0:
            fail_count += 1
            print(f"gagal ({fail_count}x)")
            if fail_count == MAX_FAIL:
                telegram(f"⚠️ <b>API JKT48 Bermasalah</b> — Gagal {MAX_FAIL}x berturut-turut\n🕐 {wib_str()}")
            time.sleep(INTERVAL)
            continue
            
        fail_count = 0
        notif_count = 0
        
        new_quota = {}
        current_time = wib()
        
        # --- PROCESS EXCLUSIVES ---
        for e in excs_snapshot:
            code = e["code"]
            category = e.get("category", "EXCLUSIVE")
            exc_title = e.get("title", "Exclusive Event")
            purchase_url = f"https://jkt48.com/purchase/exclusive?code={code}"
            
            sessions = exc_dets.get(code, [])
            for s in sessions:
                try:
                    s_time = s.get("end_time") or s.get("start_time") or "23:59:59"
                    session_dt = parse_wib_datetime(s["date"], s_time)
                    if session_dt < current_time:
                        continue
                except Exception:
                    pass
                    
                label = s.get("label", "?")
                stime = s.get("start_time", "")[:5]
                
                for m in s.get("session_members", []):
                    name = m.get("member_name", "")
                    jalur = m.get("label", "")
                    quota = m.get("quota", 0)
                    price = m.get("price", 0)
                    did = str(m.get("session_detail_id", ""))
                    
                    if WATCH_MEMBERS and name not in WATCH_MEMBERS:
                        continue
                        
                    flat_key = f"exc_{code}_{did}"
                    new_quota[flat_key] = quota
                    
                    if is_first_run:
                        continue
                        
                    prev = prev_quota.get(flat_key)
                    if prev is None:
                        continue
                        
                    selisih = prev - quota
                    if selisih > 0:
                        icon = "🔴" if quota == 0 else ("🟡" if quota / (quota + selisih) < 0.3 else "🟢")
                        s_date = s.get("date", "")[:10]
                        s_date_formatted = format_date_str(s_date)
                        print(f"\n  🛒 EXCLUSIVE TERBELI: {name} | {category} | {exc_title[:30]} | {s_date_formatted} | {label} ({stime}) | -{selisih} -> sisa {quota}")
                        telegram(
                            f"🛒 <b>TIKET EXCLUSIVE TERBELI!</b>\n\n"
                            f"ℹ️ <b>[{category}]</b> {exc_title}\n"
                            f"👤 <b>{name}</b> | 📅 {s_date_formatted} | 📋 {label} ({stime} WIB)\n"
                            f"🚪 {jalur} | 💰 Rp{price:,}\n"
                            f"📉 {prev} → {quota} <i>(-{selisih})</i> | {icon} Sisa: {quota}"
                            + (" <i>(SOLD OUT!)</i>" if quota == 0 else "") +
                            f"\n🕐 {wib_str()}\n🔗 <a href='{purchase_url}'>Lihat tiket →</a>"
                        )
                        notif_count += 1
                    elif quota > prev:
                        icon = "🟢"
                        s_date = s.get("date", "")[:10]
                        s_date_formatted = format_date_str(s_date)
                        print(f"\n  ♻️ EXCLUSIVE BERTAMBAH: {name} | {category} | {exc_title[:30]} | {s_date_formatted} | {label} ({stime}) | +{quota - prev} -> sisa {quota}")
                        telegram(
                            f"♻️ <b>TIKET EXCLUSIVE BERTAMBAH (RESTOCK)!</b>\n\n"
                            f"ℹ️ <b>[{category}]</b> {exc_title}\n"
                            f"👤 <b>{name}</b> | 📅 {s_date_formatted} | 📋 {label} ({stime} WIB)\n"
                            f"🚪 {jalur} | 💰 Rp{price:,}\n"
                            f"📈 {prev} → {quota} <i>(+{quota - prev})</i> | {icon} Sisa: {quota}"
                            f"\n🕐 {wib_str()}\n🔗 <a href='{purchase_url}'>Lihat tiket →</a>"
                        )
                        notif_count += 1

        # --- PROCESS SHOWS (Member update alerts) ---
        for s in shows_snapshot:
            ref_code = s.get("reference_code", "")
            if not ref_code:
                continue
                
            title = s.get("title", "Show Theater")
            date_str = s.get("date", "")[:10]
            stime = s.get("start_time", "")[:5]
            team = s.get("jkt48_member_type") or "TBA"
            
            purchase_url = f"https://jkt48.com/theater/schedule/id/{s['schedule_id']}?lang=id"
            
            details = show_dets.get(ref_code)
            if details is None:
                continue
                
            # Extract performing member names
            members = [m.get("name", "") for m in details.get("jkt48_member", []) if m.get("name")]
            
            if is_first_run:
                prev_show_members[ref_code] = members
                continue
                
            if ref_code in prev_show_members:
                prev_members = prev_show_members[ref_code]
                
                # Check if member list transitioned from empty/unannounced to announced
                if not prev_members and members:
                    print(f"\n📢 UPDATE MEMBER SHOW: {title} | {len(members)} member diumumkan")
                    member_list_str = "\n".join(f"• {name}" for name in members)
                    telegram(
                        f"📢 <b>UPDATE MEMBER TAMPIL!</b>\n\n"
                        f"🎭 <b>{title}</b>\n"
                        f"📅 {format_date_str(date_str)} ({stime} WIB) | 👥 Cast/Team: {team}\n\n"
                        f"👥 <b>Member Tampil ({len(members)}):</b>\n"
                        f"{member_list_str}\n\n"
                        f"🕐 {wib_str()}\n🔗 <a href='{purchase_url}'>Lihat detail show →</a>"
                    )
                    notif_count += 1
                    
                # Always track the latest members list
                prev_show_members[ref_code] = members
            else:
                prev_show_members[ref_code] = members
                    
        # Update baseline quota tracker
        if is_first_run:
            prev_quota = new_quota
            is_first_run = False
            print(f"Data awal diinisialisasi: {len(prev_quota)} slot exclusives dan {len(prev_show_members)} shows dipantau.")
        else:
            for k, v in new_quota.items():
                prev_quota[k] = v
                
        last_hb = heartbeat(excs_snapshot, shows_snapshot, exc_dets, show_dets, run_count, last_hb)
        
        status_line = f"OK ({len(exc_dets)} exc, {shows_ok} shows checked"
        if shows_api_errors > 0:
            status_line += f" | {shows_api_errors} shows API error"
        status_line += ")"
        
        print(f"  📨 {notif_count} notif" if notif_count else (f"OK {status_line}" if run_count % 10 == 0 or notif_count else "OK"))
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
