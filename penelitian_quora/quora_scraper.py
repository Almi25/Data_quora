"""
quora_scraper.py
Pengumpul jawaban Quora (halaman publik) untuk penelitian sentimen MBG & Kopdes Merah Putih.

CATATAN PENTING
- Skrip ini TIDAK diuji langsung terhadap Quora. Struktur HTML Quora sering berubah,
  jadi selector di bagian KONFIGURASI mungkin perlu disesuaikan (lihat bagian "Debug").
- Ketentuan layanan Quora membatasi scraping. Gunakan dengan kecepatan wajar, hanya untuk
  konten publik, dan tulis keterbatasan ini di bagian metodologi/etika.
- Nama pengguna TIDAK disimpan; hanya id_konten acak berurutan.

INSTALASI
    pip install playwright beautifulsoup4 pandas
    playwright install chromium

PENGGUNAAN
1) Buat urls.csv OTOMATIS (setelah login, langkah 2):
     python quora_scraper.py --discover --urls urls.csv
   Kata kunci bisa diubah di dict KEYWORDS. Periksa hasilnya dan hapus URL yang tidak relevan.
   (Alternatif manual) siapkan urls.csv dengan kolom: url,program
   (program = "MBG" atau "Koperasi Desa Merah Putih")
   URL dikumpulkan manual lewat Google, mis.:
     site:quora.com "makan bergizi gratis"
     site:quora.com "koperasi desa merah putih"

2) Login sekali (browser terbuka, login manual, lalu tekan Enter di terminal):
     python quora_scraper.py --login

3) Scraping:
     python quora_scraper.py --urls urls.csv --out quora_data.csv

   OPSI: pakai Chrome pribadi (login di browser sendiri) -> lihat bagian --cdp:
     1. Tutup semua jendela Chrome, lalu di PowerShell:
        & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\chrome_quora"
     2. Di jendela Chrome itu, login ke quora.com seperti biasa. Biarkan terbuka.
     3. Jalankan perintah apa pun dengan tambahan --cdp, mis.:
        python quora_scraper.py --discover --urls urls.csv --cdp

4) Jika hasil kosong, jalankan mode debug untuk menyimpan HTML halaman:
     python quora_scraper.py --urls urls.csv --debug
   lalu periksa file debug_*.html dan sesuaikan SELECTOR_* di bawah.
"""

import argparse
import hashlib
import random
import re
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------------------------------------------------------
# KONFIGURASI (sesuaikan jika Quora mengubah struktur halaman)
# ----------------------------------------------------------------------------
PROFILE_DIR = "quora_profile"          # menyimpan sesi login
MIN_WORDS = 5                          # sesuai kriteria eksklusi (<5 kata dibuang)
MAX_SCROLLS = 25                       # batas scroll per halaman
DELAY_RANGE = (4, 9)                   # jeda acak antarhalaman (detik)

# Teks tombol untuk memuat konten tersembunyi (Quora versi Indonesia / Inggris)
EXPAND_TEXTS = ["Selengkapnya", "Lihat selengkapnya", "(lebih banyak)",
                "more", "(more)", "Continue Reading", "Lanjutkan Membaca"]
MORE_ANSWER_TEXTS = ["Lihat jawaban lainnya", "Lihat semua", "View more answers",
                     "More answers", "Lihat lainnya"]

# Selector CSS untuk blok teks jawaban. Quora memakai class yang diacak,
# jadi pendekatan paling stabil adalah mengambil blok teks "q-text" yang panjang.
SELECTOR_TEXT_BLOCK = "div.q-text, span.q-text"
SELECTOR_ANSWER_BOX = "div.q-box"       # kontainer umum; dipakai hanya sebagai cadangan
MIN_CHARS_BLOCK = 60                    # blok pendek (menu, tombol, dsb.) diabaikan

# Kata/frasa antarmuka yang harus dibuang bila muncul sebagai teks blok
UI_NOISE = [
    "Upvote", "Downvote", "Share", "Bagikan", "Komentar", "Comments",
    "Ikuti", "Follow", "Sponsor", "Promosi", "Log in", "Masuk", "Sign up",
    "Related questions", "Pertanyaan terkait",
]



# ----------------------------------------------------------------------------
# KONTEKS BROWSER (profil Playwright sendiri, ATAU sambung ke Chrome pribadi via --cdp)
# ----------------------------------------------------------------------------
CDP_URL = None   # diisi lewat argumen --cdp, mis. http://localhost:9222


def get_context(p, headless):
    if CDP_URL:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        return browser.contexts[0]
    return p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=headless, locale="id-ID",
        viewport={"width": 1280, "height": 900})


def close_context(ctx):
    if CDP_URL:
        return          # biarkan Chrome pribadi tetap terbuka
    ctx.close()

# ----------------------------------------------------------------------------
# FUNGSI BANTU
# ----------------------------------------------------------------------------
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t


def is_noise(t: str) -> bool:
    low = t.lower()
    return any(low == n.lower() for n in UI_NOISE)


def word_count(t: str) -> int:
    return len(re.findall(r"\w+", t))


def click_texts(page, texts, max_clicks=60):
    """Klik tombol/tautan yang teksnya cocok (untuk memperluas jawaban)."""
    clicked = 0
    for txt in texts:
        loc = page.get_by_text(txt, exact=False)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, max_clicks)):
            try:
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=1500)
                    clicked += 1
                    page.wait_for_timeout(300)
            except Exception:
                continue
    return clicked


def scroll_page(page):
    """Scroll bertahap sampai tinggi halaman tidak bertambah."""
    last_h = 0
    stable = 0
    for _ in range(MAX_SCROLLS):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(random.randint(900, 1600))
        click_texts(page, MORE_ANSWER_TEXTS, max_clicks=5)
        h = page.evaluate("document.body.scrollHeight")
        if h == last_h:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_h = h


def extract_blocks(html: str):
    """Ambil blok teks panjang sebagai kandidat jawaban/komentar."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    blocks = []
    for el in soup.select(SELECTOR_TEXT_BLOCK):
        # lewati blok yang membungkus blok lain yang juga cocok (hindari duplikat bertingkat)
        if el.select(SELECTOR_TEXT_BLOCK):
            continue
        t = clean_text(el.get_text(" "))
        if len(t) >= MIN_CHARS_BLOCK and not is_noise(t):
            blocks.append(t)

    # fallback: jika tidak ada hasil, ambil semua paragraf panjang
    if not blocks:
        for el in soup.find_all(["p", "div", "span"]):
            if el.find(["p", "div"]):
                continue
            t = clean_text(el.get_text(" "))
            if len(t) >= MIN_CHARS_BLOCK and not is_noise(t):
                blocks.append(t)

    # buang duplikat dan blok yang merupakan potongan dari blok lain
    uniq = []
    for t in sorted(set(blocks), key=len, reverse=True):
        if not any(t in u for u in uniq):
            uniq.append(t)
    return uniq


def guess_type(text: str, idx: int):
    """Tebakan kasar: jawaban umumnya panjang, komentar pendek.
    Periksa ulang secara manual / sesuaikan ambangnya."""
    return "answer" if word_count(text) >= 40 else "comment"



# ----------------------------------------------------------------------------
# EKSTRAKSI JAWABAN BERDASARKAN TOMBOL "Dukung Naik" / "Upvote"
# (hanya blok yang punya tombol upvote = jawaban; daftar "pertanyaan terkait" otomatis terbuang)
# ----------------------------------------------------------------------------
JS_EXTRACT = """
() => {
  const labels = ['dukung naik', 'upvote'];
  const isLabel = e => {
    const t = (e.innerText || '').trim().toLowerCase();
    return t.length < 40 && labels.some(l => t.startsWith(l));
  };
  const all = [...document.querySelectorAll('button, div[role="button"], span, div')]
      .filter(e => isLabel(e) && ![...e.children].some(isLabel));
  const stopWords = ['pertanyaan terkait', 'related questions'];
  const out = []; const seen = new Set();
  for (const b of all) {
    let best = b.parentElement, el = b;
    while (el.parentElement && el.parentElement !== document.body) {
      const p = el.parentElement;
      const n = all.filter(x => p.contains(x)).length;
      const txt = (p.innerText || '').toLowerCase();
      if (n > 1 || txt.length > 20000 || stopWords.some(w => txt.includes(w))) break;
      best = p; el = p;
    }
    if (best && !seen.has(best)) { seen.add(best); out.push(best.innerText || ''); }
  }
  return out;
}
"""

UI_LINE = re.compile(
    r"^(dukung naik|upvote|downvote|dukung turun|bagikan|share|ikuti|follow|"
    r"\(more\)|\(selengkapnya\)|selengkapnya|lihat selengkapnya|lihat semua|"
    r"komentar|comments?|\d+[\d.,kKrb ]*|·|\.\.\.)\b.*$", re.I)


def clean_answer(raw: str) -> str:
    lines = [clean_text(x) for x in raw.split("\n")]
    lines = [x for x in lines if x]
    # buang baris antarmuka (tombol, angka, dsb.)
    lines = [x for x in lines if not (len(x) < 40 and UI_LINE.match(x))]
    # buang kepala jawaban (nama penulis, kredensial, tanggal): baris pendek di awal
    while lines and word_count(lines[0]) < 8:
        lines.pop(0)
    # buang ekor pendek (sisa tombol)
    while lines and word_count(lines[-1]) < 3:
        lines.pop()
    return clean_text(" ".join(lines))


def extract_answers(page):
    try:
        raws = page.evaluate(JS_EXTRACT)
    except Exception as e:
        print("   gagal ekstraksi JS:", e)
        return []
    out = []
    for r in raws:
        t = clean_answer(r)
        if word_count(t) >= MIN_WORDS:
            out.append(t)
    # buang duplikat & potongan dari teks lain
    uniq = []
    for t in sorted(set(out), key=len, reverse=True):
        if not any(t in u for u in uniq):
            uniq.append(t)
    return uniq


# ----------------------------------------------------------------------------
# MODE POSTCLEAN (buang sisa kredensial penulis di awal teks)
# Contoh: "Founder di Forex Trading Mentor Indonesia (2015-sekarang) Saya ..."
#         "S.P di Agribisnis, Universitas Siliwangi (Lulus 2019) Saya ..."
# ----------------------------------------------------------------------------
CRED_RE = re.compile(
    r"^.{5,170}?\((?:lulus[^)]*|(?:19|20)\d{2}\s*[-\u2013\u2014]\s*[^)]*|[^)]*(?:sekarang|saat ini)[^)]*)\)\s*",
    re.I)


# sisa penanda waktu di awal teks, mis. "· 1thn", "· 3 bln", "· 2 minggu"
RESIDUE_RE = re.compile(
    r"^[\s]*[\u00b7\u2022|][\s\u00b7\u2022|]*(?:\d+\s*(?:thn|th|tahun|bln|bulan|mgg|minggu|hr|hari|jam|mnt|menit|y|w|d|h|mo)\b[\s\u00b7\u2022|]*)+",
    re.I)


def strip_head(t: str) -> str:
    m = CRED_RE.match(t)
    if m and word_count(t[m.end():]) >= MIN_WORDS:
        t = t[m.end():]
    m2 = RESIDUE_RE.match(t)
    if m2 and word_count(t[m2.end():]) >= MIN_WORDS:
        t = t[m2.end():]
    t = re.sub(r"^[\s\u00b7\u2022|]+", "", t)    # sisa titik tengah di awal
    return t.strip()


def postclean(in_csv: str, out_csv: str):
    df = pd.read_csv(in_csv)
    new_txt, changed = [], []
    for t in df["teks"].astype(str):
        nt = strip_head(t)
        new_txt.append(nt)
        changed.append(nt != t.strip())
    df["teks_asli"] = df["teks"]
    df["teks"] = new_txt
    df["diubah"] = changed
    df = df[df["teks"].map(word_count) >= MIN_WORDS]
    df = df.drop_duplicates(subset="teks").reset_index(drop=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"{int(df['diubah'].sum())} baris dibersihkan dari {len(df)}. Disimpan ke {out_csv}")
    print("Contoh perubahan (periksa apakah wajar):")
    for _, r in df[df["diubah"]].head(8).iterrows():
        print("  SEBELUM:", r["teks_asli"][:110])
        print("  SESUDAH:", r["teks"][:110])
        print()
    print("Kolom teks_asli & diubah hanya untuk pemeriksaan; hapus sebelum pelabelan.")

# ----------------------------------------------------------------------------
# MODE LOGIN
# ----------------------------------------------------------------------------
def do_login():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = ctx.new_page()
        page.goto("https://www.quora.com/")
        input("Login ke Quora di jendela browser, lalu tekan Enter di sini... ")
        ctx.close()
    print("Sesi login tersimpan di folder:", PROFILE_DIR)


# ----------------------------------------------------------------------------
# MODE DISCOVER (cari URL pertanyaan secara otomatis)
# ----------------------------------------------------------------------------
KEYWORDS = {
    "MBG": ["makan bergizi gratis", "MBG", "program makan siang gratis"],
    "Koperasi Desa Merah Putih": ["koperasi desa merah putih", "kopdes merah putih",
                                  "koperasi merah putih"],
}
SEARCH_SCROLLS = 15
# Path yang bukan halaman pertanyaan
NON_QUESTION = ("/profile", "/search", "/topic", "/unanswered", "/answer",
                "/about", "/careers", "/contact", "/settings", "/notifications",
                "/spaces", "/share", "/partners", "/press", "/privacy", "/tos")


def looks_like_question(href: str) -> bool:
    m = re.match(r"https?://(?:www|id)\.quora\.com(/[^?#]*)", href)
    if not m:
        return False
    path = m.group(1)
    if path.count("/") != 1 or len(path) < 15:   # slug pertanyaan panjang & satu segmen
        return False
    return not path.startswith(NON_QUESTION)


def discover(out_csv: str, headless: bool):
    found = {}
    if Path(out_csv).exists():
        try:
            old_df = pd.read_csv(out_csv)
            for u, pg in zip(old_df["url"], old_df["program"]):
                found[str(u)] = pg
            print(f"{len(found)} URL lama di {out_csv} dipertahankan; URL baru akan ditambahkan.")
        except Exception as e:
            print("Gagal membaca file lama, dibuat baru:", e)
    with sync_playwright() as p:
        ctx = get_context(p, headless)
        page = ctx.new_page()
        for program, kws in KEYWORDS.items():
            for kw in kws:
                q = kw.replace(" ", "+")
                url = f"https://id.quora.com/search?q={q}&type=question"
                print(f"Cari: {kw}")
                try:
                    page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    for _ in range(SEARCH_SCROLLS):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(random.randint(900, 1500))
                    hrefs = page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")
                    # bahan diagnosa: simpan tangkapan layar & HTML halaman pencarian
                    tag = re.sub(r"\W+", "_", kw)[:30]
                    page.screenshot(path=f"debug_search_{tag}.png")
                    Path(f"debug_search_{tag}.html").write_text(
                        page.content(), encoding="utf-8")
                    print(f"   judul halaman: {page.title()!r}")
                    print(f"   total link di halaman: {len(hrefs)}")
                    for h in [x for x in hrefs if "quora.com" in x][:8]:
                        print("     contoh:", h)
                except Exception as e:
                    print("   gagal:", e)
                    continue
                n = 0
                for h in hrefs:
                    h = h.split("?")[0].split("#")[0]
                    if looks_like_question(h) and h not in found:
                        found[h] = program
                        n += 1
                print(f"   +{n} URL baru")
                time.sleep(random.uniform(*DELAY_RANGE))
        close_context(ctx)

    df = pd.DataFrame({"url": list(found.keys()), "program": list(found.values())})
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"{len(df)} URL disimpan ke {out_csv}")
    print(df["program"].value_counts())
    print("Periksa urls.csv: hapus URL yang tidak relevan sebelum scraping.")



# ----------------------------------------------------------------------------
# MODE CLEAN (saring urls.csv berdasarkan kata di URL pertanyaan)
# ----------------------------------------------------------------------------
def slug_text(url: str) -> str:
    path = re.sub(r"https?://[^/]+/", "", url.split("?")[0])
    return path.replace("-", " ").lower()


def relevant(program: str, slug: str) -> bool:
    if program == "MBG":
        keys = ["mbg", "makan bergizi", "bergizi gratis", "makan siang gratis",
                "makan gratis", "makan siang bergizi"]
        return any(k in slug for k in keys)
    # Koperasi Desa Merah Putih
    if "kopdes" in slug or "kdmp" in slug:
        return True
    return "koperasi" in slug and ("merah" in slug or "desa" in slug)


def clean_urls(in_csv: str):
    df = pd.read_csv(in_csv)
    keep = [relevant(p, slug_text(u)) for u, p in zip(df["url"], df["program"])]
    ok = df[keep].drop_duplicates(subset="url")
    out = in_csv.replace(".csv", "_bersih.csv")
    drop = in_csv.replace(".csv", "_dibuang.csv")
    ok.to_csv(out, index=False, encoding="utf-8-sig")
    df[[not k for k in keep]].to_csv(drop, index=False, encoding="utf-8-sig")
    print(f"{len(df)} URL -> {len(ok)} dipertahankan ({out}), "
          f"{len(df) - len(ok)} dibuang ({drop})")
    print(ok["program"].value_counts())
    print("Buka file *_dibuang.csv; jika ada yang sebenarnya relevan, pindahkan ke *_bersih.csv.")

# ----------------------------------------------------------------------------
# MODE SCRAPING
# ----------------------------------------------------------------------------
def scrape(urls_csv: str, out_csv: str, debug: bool, headless: bool):
    urls = pd.read_csv(urls_csv)
    assert {"url", "program"}.issubset(urls.columns), "urls.csv harus punya kolom: url,program"

    rows = []
    counter = 0
    done_urls = set()
    # jika file hasil sudah ada: cadangkan, lalu lanjutkan (lewati URL yang sudah diambil)
    if Path(out_csv).exists():
        import shutil
        backup = out_csv.replace(".csv", "_backup.csv")
        shutil.copy(out_csv, backup)
        print(f"File lama dicadangkan ke {backup}")
        try:
            prev = pd.read_csv(out_csv)
            rows = prev.to_dict("records")
            if "sumber_url" in prev.columns:
                done_urls = set(prev["sumber_url"].dropna())
            ids = prev["id_konten"].astype(str).str.extract(r"(\d+)")[0].dropna().astype(int)
            counter = int(ids.max()) if len(ids) else 0
        except Exception as e:
            print("Gagal membaca data lama:", e)

    with sync_playwright() as p:
        ctx = get_context(p, headless)
        page = ctx.new_page()

        for i, r in urls.iterrows():
            url, program = r["url"], r["program"]
            if url in done_urls:
                print(f"[{i + 1}/{len(urls)}] sudah diambil, dilewati: {url}")
                continue
            print(f"[{i + 1}/{len(urls)}] {url}")
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                scroll_page(page)
                click_texts(page, EXPAND_TEXTS)
                page.wait_for_timeout(1000)
                html = page.content()
            except PWTimeout:
                print("   timeout, dilewati")
                continue
            except Exception as e:
                print("   error:", e)
                continue

            if debug:
                name = "debug_" + hashlib.md5(url.encode()).hexdigest()[:8] + ".html"
                Path(name).write_text(html, encoding="utf-8")
                print("   HTML disimpan:", name)

            blocks = extract_answers(page)
            print(f"   {len(blocks)} jawaban ditemukan")
            if not blocks:
                name = "debug_" + hashlib.md5(url.encode()).hexdigest()[:8] + ".html"
                Path(name).write_text(html, encoding="utf-8")
                print("   0 jawaban terdeteksi; HTML disimpan untuk diagnosa:", name)

            for j, t in enumerate(blocks):
                if word_count(t) < MIN_WORDS:
                    continue
                counter += 1
                rows.append({
                    "id_konten": f"Q{counter:05d}",          # tanpa nama pengguna
                    "program": program,
                    "platform": "Quora",
                    "jenis_konten": "answer",
                    "teks": t,
                    "tanggal": "",                            # isi manual / dari parsing tambahan
                    "label_sentimen": "",                     # diisi saat pelabelan
                    "aspek": "",
                    "sumber_url": url,                        # hapus kolom ini sebelum dipublikasikan
                })

            time.sleep(random.uniform(*DELAY_RANGE))

            # simpan bertahap agar data tidak hilang bila terhenti
            if rows:
                pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8-sig")

        close_context(ctx)

    df = pd.DataFrame(rows)
    if df.empty:
        print("Tidak ada data. Jalankan dengan --debug dan sesuaikan selector.")
        return

    # buang duplikat teks persis
    before = len(df)
    df = df.drop_duplicates(subset="teks").reset_index(drop=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"Selesai. {before} -> {len(df)} baris setelah dedup. Disimpan ke {out_csv}")
    print(df.groupby(["program", "jenis_konten"]).size())


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="login manual & simpan sesi")
    ap.add_argument("--postclean", action="store_true",
                    help="bersihkan kredensial penulis: --urls quora_data.csv --out quora_data_bersih.csv")
    ap.add_argument("--clean", action="store_true",
                    help="saring urls.csv -> urls_bersih.csv (+ urls_dibuang.csv)")
    ap.add_argument("--discover", action="store_true",
                    help="cari URL pertanyaan otomatis -> urls.csv")
    ap.add_argument("--urls", default="urls.csv")
    ap.add_argument("--out", default="quora_data.csv")
    ap.add_argument("--debug", action="store_true", help="simpan HTML tiap halaman")
    ap.add_argument("--cdp", nargs="?", const="http://localhost:9222",
                    help="sambung ke Chrome pribadi yang dibuka dengan --remote-debugging-port=9222")
    ap.add_argument("--headless", action="store_true", help="tanpa jendela (lebih mudah terdeteksi)")
    a = ap.parse_args()
    if a.cdp:
        CDP_URL = a.cdp

    if a.login:
        do_login()
    elif a.postclean:
        postclean(a.urls, a.out)
    elif a.clean:
        clean_urls(a.urls)
    elif a.discover:
        discover(a.urls, a.headless)
    else:
        scrape(a.urls, a.out, a.debug, a.headless)
