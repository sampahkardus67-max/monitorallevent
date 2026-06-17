#!/usr/bin/env python3
"""
JKT48 Ticket & Show Monitor with Telegram Interactive Commands & Member Query Filtering
Monitors:
1. Exclusives (PHOTOCARD, TWO_SHOT, DIGITAL_PHOTOBOOK) - Purchases & Restocks
2. Shows (type=SHOW) - Available/Sold Ticket Count & Purchases

Interactive Commands (only responds to configured CHAT_ID):
- "vc [query]" / "/vc [query]": Returns Digital Photobook (video call) data.
- "pc [query]" / "/pc [query]" / "photocard [query]": Returns Photocard data.
- "2s [query]" / "/2s [query]" / "2shot [query]": Returns Two Shot data.
- "pb [query]" / "/pb [query]" / "photobook [query]": Returns Digital Photobook data.
- "show [query]" / "/show [query]": Returns theater show details and ticket availability (filtered by show title/team).
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
HEARTBEAT_H    = int(os.environ.get("HEBEAT_HOURS", "6"))
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

# Fetch Single Show Ticket Details
def fetch_show_ticket_details(schedule_id):
    url = f"https://jkt48.com/api/v1/schedules/ticket?schedule_id={schedule_id}&lang=id"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") and "data" in data and data["data"]:
            return data["data"]
    except Exception:
        pass
        
    url = f"https://jkt48.com/api/v1/schedules/ticket-info?schedule_id={schedule_id}&lang=id"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") and "data" in data and data["data"]:
            return data["data"]
    except Exception:
        pass
        
    return None

# Parsers
def parse_show_tickets(data):
    tickets = []
    if not data:
        return tickets
        
    if isinstance(data, dict):
        for key in ['tickets', 'ticket', 'data', 'details']:
            if key in data and isinstance(data[key], (list, dict)):
                data = data[key]
                break
                
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("ticket_type") or item.get("type") or "Ticket"
                price = item.get("price") or 0
                avail = item.get("available") or item.get("quota") or item.get("tickets_available") or 0
                sold = item.get("tickets_sold") or item.get("sold") or 0
                tid = str(item.get("ticket_type_id") or item.get("id") or name)
                tickets.append({
                    "id": tid,
                    "name": name,
                    "price": price,
                    "available": int(avail),
                    "sold": int(sold),
                    "total": int(avail) + int(sold)
                })
    elif isinstance(data, dict):
        name = data.get("name") or "Ticket"
        price = data.get("price") or 0
        avail = data.get("available") or data.get("quota") or data.get("tickets_available") or 0
        sold = data.get("tickets_sold") or data.get("sold") or 0
        tid = str(data.get("ticket_type_id") or data.get("id") or name)
        tickets.append({
            "id": tid,
            "name": name,
            "price": price,
            "available": int(avail),
            "sold": int(sold),
            "total": int(avail) + int(sold)
        })
    return tickets

# Concurrently fetch details of active exclusives and shows
def fetch_all_active_data(exclusives, shows):
    exc_results = {}
    show_results = {}
    
    def fetch_exc(exc):
        code = exc["code"]
        res = fetch_exclusive_details(code)
        return code, res

    def fetch_show(s):
        sid = s["schedule_id"]
        res = fetch_show_ticket_details(sid)
        return sid, res

    with ThreadPoolExecutor(max_workers=8) as executor:
        exc_futures = [executor.submit(fetch_exc, e) for e in exclusives]
        show_futures = [executor.submit(fetch_show, s) for s in shows]
        
        for f in exc_futures:
            code, res = f.result()
            if res is not None:
                exc_results[code] = res
                
        for f in show_futures:
            sid, res = f.result()
            if res is not None:
                show_results[sid] = res
                
    return exc_results, show_results

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
    global active_shows
    
    with cache_lock:
        shows = list(active_shows)
        
    if not shows:
        shows = fetch_shows_list()
        
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
        title = s.get("title", "Show Theater")
        date_str = s.get("date", "")[:10]
        stime = s.get("start_time", "")[:5]
        team = s.get("jkt48_member_type") or "TBA"
        birthday = s.get("birthday_member")
        
        # Match query with title or team or birthday member
        if query:
            q_lower = query.lower().strip()
            title_lower = title.lower()
            team_lower = team.lower()
            birthday_lower = (birthday or "").lower()
            if q_lower not in title_lower and q_lower not in team_lower and q_lower not in birthday_lower:
                continue
                
        found_any = True
        show_info = f"<b>{title}</b>\n"
        show_info += f"📅 {date_str} ({stime} WIB)\n"
        show_info += f"👥 Cast/Team: {team}\n"
        if birthday:
            show_info += f"🎂 Birthday Show: {birthday}\n"
            
        raw_tickets = fetch_show_ticket_details(sid)
        tickets = parse_show_tickets(raw_tickets)
        
        if tickets:
            t_lines = []
            for t in tickets:
                tname = t["name"]
                price = t["price"]
                avail = t["available"]
                sold = t["sold"]
                icon = "🔴" if avail == 0 else "🟢"
                t_lines.append(f"  └─ {tname}: {icon} Sisa {avail} | Terjual: {sold} (Rp{price:,})")
            show_info += "\n".join(t_lines) + "\n"
        else:
            show_info += "  ⚠️ <i>Detail tiket tidak tersedia atau API error</i>\n"
            
        purchase_url = f"https://jkt48.com/theater/schedule/id/{sid}?lang=id"
        show_info += f"🔗 <a href='{purchase_url}'>Beli Tiket →</a>\n\n"
        
        response_msg += show_info
        
    if not found_any:
        telegram("ℹ️ <b>Tidak ada jadwal Theater Show yang cocok dengan pencarian Anda.</b>", target_chat_id)
    else:
        send_long_telegram_message(target_chat_id, response_msg)

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
                        
                    # Split command and query parameter
                    parts = text.split(maxsplit=1)
                    cmd = parts[0].lower()
                    query = parts[1] if len(parts) > 1 else ""
                    
                    print(f"🤖 Telegram menerima command: '{cmd}' | Query: '{query}' | Dari {chat_id}")
                    if cmd in ["vc", "/vc"]:
                        handle_exclusive_by_category(chat_id, "DIGITAL_PHOTOBOOK", query)
                    elif cmd in ["pb", "/pb", "photobook", "/photobook"]:
                        handle_exclusive_by_category(chat_id, "DIGITAL_PHOTOBOOK", query)
                    elif cmd in ["pc", "/pc", "photocard", "/photocard"]:
                        handle_exclusive_by_category(chat_id, "PHOTOCARD", query)
                    elif cmd in ["2s", "/2s", "2shot", "/2shot"]:
                        handle_exclusive_by_category(chat_id, "TWO_SHOT", query)
                    elif cmd in ["show", "/show"]:
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
    show_with_tickets = len(show_dets)
    
    msg = (
        f"💓 <b>Laporan Berkala JKT48 Monitor</b>\n\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M WIB')} | ⚡ Interval: {INTERVAL}s\n"
        f"📈 Run Count: {run_count:,}x\n\n"
        f"🃏 <b>Exclusives Monitored:</b> {len(active_excs)}\n"
        f"└─ Total Slots: {exc_total_slots} | Tersedia: {exc_avail_slots}\n\n"
        f"🎭 <b>Theater Shows Monitored:</b> {show_total}\n"
        f"└─ Dengan Data Tiket: {show_with_tickets}\n\n"
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
            
        # 2. Poll detailed ticket data in parallel
        run_count += 1
        print(f"[{wib().strftime('%H:%M:%S')}] Cek #{run_count}...", end=" ", flush=True)
        
        with cache_lock:
            excs_snapshot = list(active_exclusives)
            shows_snapshot = list(active_shows)
            
        exc_dets, show_dets = fetch_all_active_data(excs_snapshot, shows_snapshot)
        
        # Share details with global cache
        with cache_lock:
            exc_details = exc_dets
            show_details = show_dets
            
        # Check if BOTH failed entirely
        if not exc_dets and not show_dets and len(excs_snapshot) + len(shows_snapshot) > 0:
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

        # --- PROCESS SHOWS ---
        shows_api_errors = 0
        shows_ok = 0
        
        for s in shows_snapshot:
            sid = s["schedule_id"]
            title = s.get("title", "Show Theater")
            date_str = s.get("date", "")[:10]
            stime = s.get("start_time", "")[:5]
            
            purchase_url = f"https://jkt48.com/theater/schedule/id/{sid}?lang=id"
            
            raw_tickets = show_dets.get(sid)
            if raw_tickets is None:
                shows_api_errors += 1
                continue
                
            shows_ok += 1
            tickets = parse_show_tickets(raw_tickets)
            
            for t in tickets:
                tid = t["id"]
                tname = t["name"]
                price = t["price"]
                avail = t["available"]
                sold = t["sold"]
                
                flat_key = f"show_{sid}_{tid}"
                new_quota[flat_key] = avail
                
                if is_first_run:
                    continue
                    
                prev = prev_quota.get(flat_key)
                if prev is None:
                    continue
                    
                selisih = prev - avail
                if selisih > 0:
                    icon = "🔴" if avail == 0 else ("🟡" if avail / (avail + selisih) < 0.3 else "🟢")
                    print(f"\n  🛒 TIKET SHOW TERBELI: {title} | {tname} | -{selisih} -> sisa {avail}")
                    telegram(
                        f"🛒 <b>TIKET SHOW TERBELI!</b>\n\n"
                        f"🎭 <b>{title}</b>\n"
                        f"📅 {date_str} ({stime} WIB) | 🎟️ {tname}\n"
                        f"💰 Rp{price:,} | Terjual: {sold}\n"
                        f"📉 {prev} → {avail} <i>(-{selisih})</i> | {icon} Sisa: {avail}"
                        + (" <i>(SOLD OUT!)</i>" if avail == 0 else "") +
                        f"\n🕐 {wib_str()}\n🔗 <a href='{purchase_url}'>Lihat tiket →</a>"
                    )
                    notif_count += 1
                    
        # Update baseline quota tracker
        if is_first_run:
            prev_quota = new_quota
            is_first_run = False
            print(f"Data awal diinisialisasi: {len(prev_quota)} slot tiket dipantau.")
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
