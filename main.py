#!/usr/bin/env python3
"""
JKT48 All-in-One Monitor — Railway.app
========================================
Fungsi 1: Pantau pembelian tiket EXCLUSIVE (PHOTOCARD/TWO_SHOT/DIGITAL_PHOTOBOOK)
           → notif saat quota berkurang (ada yang beli) atau restock
           → hanya event yang tanggalnya belum lewat

Fungsi 2: Pantau jadwal SHOW yang akan datang
           → notif saat show baru muncul
           → notif saat quota show berkurang (ada yang beli tiket)
           → ambil data show dari /api/v1/schedules bulan berjalan + bulan depan

Fungsi 3: Heartbeat setiap 6 jam
"""

import requests
import os
import time
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────────

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

INTERVAL       = 10   # detik antar pengecekan
HEARTBEAT_H    = 6    # jam antar laporan berkala
MAX_FAIL       = 10   # max gagal berturut-turut sebelum alert

BASE_URL       = "https://jkt48.com/api/v1"
EXCLUSIVE_LIST = f"{BASE_URL}/exclusives?lang=id"
SCHEDULES_URL  = f"{BASE_URL}/schedules?lang=id"

# Kategori exclusive yang dipantau
WATCH_CATEGORIES = {"TWO_SHOT", "PHOTOCARD", "DIGITAL_PHOTOBOOK"}

CATEGORY_LABEL = {
    "TWO_SHOT":          "📸 2Shot",
    "PHOTOCARD":         "🃏 Meet & Greet",
    "DIGITAL_PHOTOBOOK": "📖 Video Call / Photobook",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9",
    "Referer": "https://jkt48.com/",
    "Origin": "https://jkt48.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# ─────────────────────────────────────────────
#  WAKTU
# ─────────────────────────────────────────────

def now_wib() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))

def now_str() -> str:
    return now_wib().strftime("%Y-%m-%d %H:%M:%S WIB")

def parse_date(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None

def is_future(iso: str) -> bool:
    """Return True jika tanggal belum lewat (masih akan datang)."""
    dt = parse_date(iso)
    if dt is None:
        return True
    return dt > datetime.now(timezone.utc)

def fmt_date(iso: str) -> str:
    dt = parse_date(iso)
    if not dt:
        return iso
    wib = dt.astimezone(timezone(timedelta(hours=7)))
    return wib.strftime("%d %b %Y, %H:%M WIB")

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────

def tg(msg: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN / CHAT_ID belum diset!")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        r.raise_for_status()
        print("  ✅ Telegram terkirim")
        return True
    except Exception as e:
        print(f"  ❌ Gagal kirim Telegram: {e}")
        return False

# ─────────────────────────────────────────────
#  FETCH HELPER
# ─────────────────────────────────────────────

def fetch_json(url: str, retries: int = 2) -> dict | list | None:
    for i in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            r.raise_for_status()
            text = r.content.decode("utf-8", errors="replace").strip()
            if not text or not (text.startswith("{") or text.startswith("[")):
                raise ValueError(f"Bukan JSON: {text[:60]!r}")
            return r.json()
        except Exception as e:
            if i < retries:
                print(f"  ⚠ Attempt {i}/{retries} gagal, retry 3s: {e}")
                time.sleep(3)
            else:
                print(f"  ❌ Fetch gagal ({url}): {e}")
                return None

# ─────────────────────────────────────────────
#  FUNGSI 1 — EXCLUSIVE MONITOR
#  Pantau pembelian & restock tiket exclusive
# ─────────────────────────────────────────────

def fetch_exclusive_list() -> list:
    """
    Ambil daftar exclusive dari API.
    Filter hanya kategori yang dipantau.
    Tidak filter tanggal di sini karena valid_date_from adalah
    tanggal mulai penjualan, bukan tanggal event.
    Event bisa masih aktif meski valid_date_from sudah lewat.
    """
    data = fetch_json(EXCLUSIVE_LIST)
    if not data or not data.get("status"):
        return []
    result = []
    for e in data.get("data", []):
        cat = e.get("category", "")
        if cat not in WATCH_CATEGORIES:
            continue
        result.append(e)
    return result

def fetch_exclusive_bonus(code: str) -> list | None:
    """Ambil data quota per member untuk satu exclusive."""
    data = fetch_json(f"{BASE_URL}/exclusives/{code}/bonus?lang=id")
    if not data or not data.get("status"):
        return None
    return data.get("data", [])

def extract_exclusive_quota(sessions: list) -> dict:
    """Ubah sessions menjadi {session_detail_id: {quota, name, jalur, sesi, price}}"""
    result = {}
    for s in sessions:
        sesi_label = s.get("label", "?")
        sesi_time  = s.get("start_time", "")[:5]
        for m in s.get("session_members", []):
            did = str(m.get("session_detail_id", ""))
            result[did] = {
                "quota": m.get("quota", 0),
                "name":  m.get("member_name", ""),
                "jalur": m.get("label", ""),
                "sesi":  f"{sesi_label} ({sesi_time} WIB)",
                "price": m.get("price", 0),
            }
    return result

# ─────────────────────────────────────────────
#  FUNGSI 2 — SHOW MONITOR
#  Pantau jadwal show yang akan datang
# ─────────────────────────────────────────────

def fetch_shows() -> list:
    """Ambil show yang akan datang dari bulan ini dan bulan depan."""
    now   = now_wib()
    shows = []

    for delta in [0, 1]:
        target = now + timedelta(days=32 * delta)
        url    = f"{SCHEDULES_URL}&month={target.month}&year={target.year}"
        data   = fetch_json(url)
        if not data or not data.get("status"):
            continue
        for s in data.get("data", []):
            if s.get("type") != "SHOW":
                continue
            if not is_future(s.get("date", "")):
                continue
            shows.append(s)

    # Deduplikasi berdasarkan schedule_id
    seen = set()
    unique = []
    for s in shows:
        sid = s.get("schedule_id")
        if sid not in seen:
            seen.add(sid)
            unique.append(s)
    return unique

def fetch_show_detail(link: str) -> dict | None:
    """Ambil detail show termasuk quota tiket."""
    data = fetch_json(f"{BASE_URL}/schedules/{link}?lang=id")
    if not data or not data.get("status"):
        return None
    return data.get("data")

def extract_show_quota(detail: dict) -> dict:
    """Ambil quota tiket dari detail show. {kategori: quota}"""
    result = {}
    for ticket in detail.get("tickets", []):
        label = ticket.get("label", ticket.get("type", "?"))
        result[label] = {
            "quota":    ticket.get("quota", 0),
            "total":    ticket.get("total", 0),
            "sold":     ticket.get("sold", 0),
            "price":    ticket.get("price", 0),
        }
    return result

# ─────────────────────────────────────────────
#  HEARTBEAT
# ─────────────────────────────────────────────

def send_heartbeat(
    run_count: int,
    fail_total: int,
    exclusive_list: list,
    show_list: list,
    last_hb: datetime,
) -> datetime:
    now     = now_wib()
    next_hb = now + timedelta(hours=HEARTBEAT_H)

    ex_lines = ""
    for e in exclusive_list[:8]:  # max 8 item agar tidak terlalu panjang
        cat  = CATEGORY_LABEL.get(e.get("category", ""), "🎫")
        code = e.get("code", "")
        title = e.get("title", "")[:40]
        ex_lines += f"   • [{code}] {cat} — {title}\n"
    if not ex_lines:
        ex_lines = "   (tidak ada)\n"

    sh_lines = ""
    for s in show_list[:8]:
        title = s.get("title", "")[:35]
        date  = fmt_date(s.get("date", ""))
        sh_lines += f"   • {title} | {date}\n"
    if not sh_lines:
        sh_lines = "   (tidak ada)\n"

    tg(
        f"💓 <b>Laporan Berkala — JKT48 Monitor</b>\n\n"
        f"✅ Sistem berjalan normal\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M WIB')}\n"
        f"⚡ Interval: {INTERVAL}s | Heartbeat: {HEARTBEAT_H}h\n\n"
        f"🎫 <b>Exclusive dipantau ({len(exclusive_list)}):</b>\n{ex_lines}\n"
        f"🎭 <b>Show mendatang ({len(show_list)}):</b>\n{sh_lines}\n"
        f"📈 Total cek: {run_count:,}x | Gagal: {fail_total}x\n"
        f"🔁 Berikutnya: {next_hb.strftime('%H:%M WIB')}"
    )
    print("  💓 Heartbeat terkirim")
    return now

# ─────────────────────────────────────────────
#  INISIALISASI
# ─────────────────────────────────────────────

def init():
    print("🔄 Inisialisasi data awal...")

    # Ambil daftar exclusive
    exclusive_list = []
    for attempt in range(5):
        exclusive_list = fetch_exclusive_list()
        if exclusive_list is not None:
            break
        print(f"  ⚠ Exclusive API gagal (attempt {attempt+1}/5), retry 5s...")
        time.sleep(5)
    if not exclusive_list:
        print("  ⚠ Tidak ada exclusive yang ditemukan, lanjut tanpa exclusive...")
        exclusive_list = []

    # Ambil quota awal tiap exclusive
    ex_quotas = {}
    for e in exclusive_list:
        code     = e.get("code", "")
        sessions = fetch_exclusive_bonus(code)
        if sessions:
            ex_quotas[code] = extract_exclusive_quota(sessions)
            total = len(ex_quotas[code])
            avail = sum(1 for v in ex_quotas[code].values() if v["quota"] > 0)
            print(f"  ✅ [{code}] {e.get('title','')[:40]} — {total} slot, {avail} tersedia")

    # Ambil daftar show
    show_list = fetch_shows()
    print(f"  ✅ {len(show_list)} show mendatang ditemukan")

    # Ambil quota awal tiap show
    show_quotas  = {}
    known_shows  = set()
    for s in show_list:
        sid  = s.get("schedule_id")
        link = s.get("link", "")
        known_shows.add(sid)
        detail = fetch_show_detail(link)
        if detail:
            show_quotas[sid] = extract_show_quota(detail)
        else:
            show_quotas[sid] = {}

    return exclusive_list, ex_quotas, show_list, show_quotas, known_shows

# ─────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("JKT48 All-in-One Monitor — Railway.app")
    print(f"Interval : {INTERVAL}s | Heartbeat: {HEARTBEAT_H}h")
    print(f"Kategori : {', '.join(WATCH_CATEGORIES)}")
    print("=" * 55)

    exclusive_list, ex_quotas, show_list, show_quotas, known_shows = init()

    tg(
        f"✅ <b>JKT48 Monitor aktif!</b>\n\n"
        f"⚡ Interval: <b>{INTERVAL} detik</b>\n"
        f"🎫 Exclusive dipantau: {len(exclusive_list)}\n"
        f"🎭 Show mendatang: {len(show_list)}\n"
        f"🕐 Mulai: {now_str()}"
    )

    run_count  = 0
    fail_count = 0
    fail_total = 0
    last_hb    = now_wib()

    # Counter refresh data — refresh daftar setiap 5 menit
    REFRESH_EVERY = 300 // INTERVAL

    while True:
        time.sleep(INTERVAL)
        run_count += 1
        ts = now_wib().strftime("%H:%M:%S")
        print(f"[{ts}] Cek #{run_count}...", end=" ", flush=True)

        any_ok = False

        # ── Refresh daftar exclusive & show setiap ~5 menit ──
        if run_count % REFRESH_EVERY == 0:
            new_excl = fetch_exclusive_list()
            if new_excl:
                # Deteksi exclusive baru
                existing_codes = {e.get("code") for e in exclusive_list}
                for e in new_excl:
                    code = e.get("code", "")
                    if code not in existing_codes:
                        cat   = CATEGORY_LABEL.get(e.get("category", ""), "🎫")
                        title = e.get("title", "")
                        vdate = fmt_date(e.get("valid_date_from", ""))
                        print(f"\n  🆕 EXCLUSIVE BARU: [{code}] {title}")
                        tg(
                            f"🆕 <b>EXCLUSIVE BARU!</b>\n\n"
                            f"🏷 <b>Kategori:</b> {cat}\n"
                            f"📌 <b>Judul:</b> {title}\n"
                            f"🔑 <b>Kode:</b> <code>{code}</code>\n"
                            f"📅 <b>Mulai:</b> {vdate}\n\n"
                            f"🔗 https://jkt48.com/purchase/exclusive?code={code}"
                        )
                        # Init quota untuk exclusive baru
                        sessions = fetch_exclusive_bonus(code)
                        if sessions:
                            ex_quotas[code] = extract_exclusive_quota(sessions)
                exclusive_list = new_excl

            new_shows = fetch_shows()
            if new_shows:
                # Deteksi show baru
                for s in new_shows:
                    sid = s.get("schedule_id")
                    if sid not in known_shows:
                        title = s.get("title", "")
                        date  = fmt_date(s.get("date", ""))
                        mtype = s.get("jkt48_member_type", "")
                        ref   = s.get("reference_code", "")
                        print(f"\n  🆕 SHOW BARU: {title} | {date}")
                        tg(
                            f"🎭 <b>SHOW BARU!</b>\n\n"
                            f"🎵 <b>Judul:</b> {title}\n"
                            f"👥 <b>Team:</b> {mtype}\n"
                            f"📅 <b>Tanggal:</b> {date}\n"
                            f"🔑 <b>Kode:</b> <code>{ref}</code>\n\n"
                            f"🔗 https://jkt48.com/schedule/detail/{s.get('link','')}"
                        )
                        known_shows.add(sid)
                        link   = s.get("link", "")
                        detail = fetch_show_detail(link)
                        show_quotas[sid] = extract_show_quota(detail) if detail else {}
                show_list = new_shows

        # ── Cek quota exclusive ──
        notif_count = 0
        for e in exclusive_list:
            code  = e.get("code", "")
            title = e.get("title", "")
            cat   = CATEGORY_LABEL.get(e.get("category", ""), "🎫")
            url   = f"https://jkt48.com/purchase/exclusive?code={code}"

            sessions = fetch_exclusive_bonus(code)
            if sessions is None:
                continue

            any_ok    = True
            new_quota = extract_exclusive_quota(sessions)
            prev      = ex_quotas.get(code, {})

            for did, info in new_quota.items():
                name   = info["name"]
                jalur  = info["jalur"]
                sesi   = info["sesi"]
                quota  = info["quota"]
                price  = info["price"]
                pquota = prev.get(did, {}).get("quota", 0)
                selisih = pquota - quota

                if selisih > 0:
                    # Ada pembelian
                    icon = "🔴" if quota == 0 else ("🟡" if quota <= 2 else "🟢")
                    print(f"\n  🛒 TERBELI [{code}]: {name} | {sesi} | {jalur} | -{selisih} → sisa {quota}")
                    tg(
                        f"🛒 <b>TIKET TERBELI!</b>\n\n"
                        f"🏷 {cat} | <code>{code}</code>\n"
                        f"📌 {title}\n\n"
                        f"👤 <b>{name}</b>\n"
                        f"📋 {sesi} | 🚪 {jalur}\n"
                        f"💰 Rp{price:,}\n"
                        f"📉 {pquota} → {quota} <i>(-{selisih})</i> {icon}"
                        + (" <b>SOLD OUT!</b>" if quota == 0 else f" sisa: {quota}") +
                        f"\n🕐 {now_str()}\n🔗 <a href='{url}'>Lihat →</a>"
                    )
                    notif_count += 1

                elif quota > pquota and pquota >= 0:
                    # Restock
                    print(f"\n  ♻️  RESTOCK [{code}]: {name} | {sesi} | {jalur} | +{quota - pquota} → {quota}")
                    tg(
                        f"♻️ <b>RESTOCK TIKET!</b>\n\n"
                        f"🏷 {cat} | <code>{code}</code>\n"
                        f"👤 <b>{name}</b>\n"
                        f"📋 {sesi} | 🚪 {jalur}\n"
                        f"📈 {pquota} → {quota} <i>(+{quota - pquota})</i>\n"
                        f"🕐 {now_str()}\n🔗 <a href='{url}'>Beli →</a>"
                    )
                    notif_count += 1

            ex_quotas[code] = new_quota

        # ── Cek quota show ──
        for s in show_list:
            sid   = s.get("schedule_id")
            link  = s.get("link", "")
            title = s.get("title", "")
            date  = fmt_date(s.get("date", ""))
            ref   = s.get("reference_code", "")
            mtype = s.get("jkt48_member_type", "")
            url   = f"https://jkt48.com/schedule/detail/{link}"

            detail = fetch_show_detail(link)
            if not detail:
                continue

            any_ok    = True
            new_sq    = extract_show_quota(detail)
            prev_sq   = show_quotas.get(sid, {})

            for label, info in new_sq.items():
                quota  = info.get("quota", 0)
                sold   = info.get("sold", 0)
                total  = info.get("total", 0)
                price  = info.get("price", 0)
                pquota = prev_sq.get(label, {}).get("quota", total)
                selisih = pquota - quota

                if selisih > 0:
                    icon = "🔴" if quota == 0 else ("🟡" if quota <= 5 else "🟢")
                    print(f"\n  🎟 TIKET SHOW TERBELI: {title} | {label} | -{selisih} → sisa {quota}")
                    tg(
                        f"🎟 <b>TIKET SHOW TERBELI!</b>\n\n"
                        f"🎭 <b>{title}</b>\n"
                        f"👥 Team: {mtype} | 📅 {date}\n"
                        f"🏷 Kategori: {label} | 💰 Rp{price:,}\n"
                        f"📉 {pquota} → {quota} <i>(-{selisih})</i> {icon}"
                        + (" <b>HABIS!</b>" if quota == 0 else f" sisa: {quota}") +
                        f"\n🕐 {now_str()}\n🔗 <a href='{url}'>Lihat show →</a>"
                    )
                    notif_count += 1

            show_quotas[sid] = new_sq

        if not any_ok:
            fail_count += 1
            fail_total += 1
            print(f"semua fetch gagal ({fail_count}x)")
            if fail_count == MAX_FAIL:
                tg(
                    f"⚠️ <b>Monitor Bermasalah</b>\n\n"
                    f"Semua fetch API gagal {MAX_FAIL}x berturut-turut.\n"
                    f"🕐 {now_str()}"
                )
        else:
            fail_count = 0

        # Heartbeat
        if (now_wib() - last_hb).total_seconds() >= HEARTBEAT_H * 3600:
            last_hb = send_heartbeat(run_count, fail_total, exclusive_list, show_list, last_hb)

        if notif_count:
            print(f"  📨 {notif_count} notif")
        elif any_ok:
            print("OK" if run_count % 20 != 0 else f"OK ({run_count}x cek)")

if __name__ == "__main__":
    main()
