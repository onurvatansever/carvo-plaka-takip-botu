import base64
import hmac
import io
import json
import os
import re
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError


s3_client = boto3.client("s3")
rekognition_client = boto3.client("rekognition")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
S3_BUCKET = os.environ.get("S3_BUCKET")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
S3_FILE_KEY = "yikama_takip.csv"


def integer_env_set(name):
    """Comma-separated integer environment variable values as a set."""
    values = set()
    for raw_value in os.environ.get(name, "").split(","):
        value = raw_value.strip()
        if not value:
            continue
        try:
            values.add(int(value))
        except ValueError:
            raise RuntimeError(f"{name} contains an invalid chat ID")
    return values


ALLOWED_CHAT_IDS = integer_env_set("ALLOWED_CHAT_IDS")
TURKIYE_TZ = timezone(timedelta(hours=3))
_WEBHOOK_KONTROL_EDILDI = False

DIGIT_FIX = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8"}
LETTER_FIX = {"0": "D", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}
YASAKLI_HARFLER = frozenset("QWX")

GORSEL_BENZERLER = {
    "0": "ODQCGU86", "1": "IL7TJ", "2": "Z", "3": "8BE5", "4": "AH", "5": "S368",
    "6": "GB805E", "7": "1ITL", "8": "B06359S", "9": "8GP0", "A": "4HR", "B": "863PRDE",
    "C": "0OGDQ", "D": "0OCQB", "E": "F38B", "F": "EP", "G": "60C8O", "H": "4MN",
    "I": "1L7TJ", "J": "1IL", "K": "R", "L": "1I7", "M": "NH", "N": "MH",
    "O": "0DQCGU", "P": "RFB9", "R": "PKAB", "S": "58", "T": "71I", "U": "0OVY",
    "V": "UY", "Y": "VU", "Z": "2",
}
TELEFON_VEYA_GURULTU_REGEX = re.compile(
    r"(?:^|\b)(?:021[26]|0312|0232|0224|0242|05[0345]\d|0850|\+90)\d{5,}"
    r"|WWW\.|\.COM\b|\.NET\b|HTTP|TEL[:\s]|GSM[:\s]",
    re.IGNORECASE,
)
IL_34_BOZUK_ONEKLER = ("TR34", "T34", "R34", "I34", "134", "L34", "034", "IR34", "1R34", "7R34")
IL_34_TEK_HATA = frozenset({"84", "B4", "S4", "E4", "J4", "3A", "3H"})


class S3TabloHatasi(RuntimeError):
    """The table could not be read or written safely."""


def bugunun_tarihi():
    return datetime.now(TURKIYE_TZ).strftime("%d.%m.%Y")


def telegram_webhook_gecerli_mi(event):
    if not TELEGRAM_WEBHOOK_SECRET:
        print("Security configuration error: TELEGRAM_WEBHOOK_SECRET is missing.")
        return False
    headers = event.get("headers") or {}
    normalized = {str(k).lower(): str(v) for k, v in headers.items()}
    return hmac.compare_digest(normalized.get("x-telegram-bot-api-secret-token", ""), TELEGRAM_WEBHOOK_SECRET)


def chat_yetkili_mi(chat_id):
    return bool(ALLOWED_CHAT_IDS) and chat_id in ALLOWED_CHAT_IDS


def komutu_ayir(text):
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].split("@", 1)[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


def karakterleri_duzelt(il, harf, sayi):
    return (
        "".join(DIGIT_FIX.get(c, c) for c in il),
        "".join(LETTER_FIX.get(c, c) for c in harf),
        "".join(DIGIT_FIX.get(c, c) for c in sayi),
    )


def tr_plaka_gecerli_mi(il, harf, sayi, sadece_34=False):
    if not (il.isdigit() and sayi.isdigit() and harf.isalpha()) or any(c in YASAKLI_HARFLER for c in harf):
        return False
    if (il != "34") if sadece_34 else not (1 <= int(il) <= 81):
        return False
    return (
        (len(harf) == 1 and len(sayi) in (4, 5))
        or (len(harf) == 2 and len(sayi) in (3, 4))
        or (len(harf) == 3 and len(sayi) in (2, 3))
    )


def _bolum_degerlendir(il_ham, harf_ham, sayi_ham, sadece_34=True):
    gercek_harf = sum(1 for c in harf_ham if c.isalpha() and c not in YASAKLI_HARFLER)
    gercek_rakam = sum(1 for c in sayi_ham if c.isdigit())
    harf_duzeltme = sum(1 for c in harf_ham if not c.isalpha())
    sayi_duzeltme = len(sayi_ham) - gercek_rakam
    max_grup_hata = 2 if il_ham == "34" else 1
    if gercek_harf == 0 or gercek_rakam == 0 or harf_duzeltme > max_grup_hata or sayi_duzeltme > max_grup_hata:
        return None
    if il_ham != "34" and gercek_rakam < max(1, len(sayi_ham) - 1):
        return None

    if il_ham == "34":
        il_temiz, il_duzeltme = "34", 0
    elif sadece_34 and il_ham in IL_34_TEK_HATA:
        il_temiz, il_duzeltme = "34", 1
    else:
        if not any(c.isdigit() for c in il_ham):
            return None
        il_temiz = "".join(DIGIT_FIX.get(c, c) for c in il_ham)
        il_duzeltme = sum(1 for a, b in zip(il_ham, il_temiz) if a != b)
        if il_duzeltme > 1:
            return None

    _, harf_temiz, sayi_temiz = karakterleri_duzelt(il_temiz, harf_ham, sayi_ham)
    toplam_duzeltme = il_duzeltme + harf_duzeltme + sayi_duzeltme
    if toplam_duzeltme > 2 or not tr_plaka_gecerli_mi(il_temiz, harf_temiz, sayi_temiz, sadece_34=sadece_34):
        return None
    return f"{il_temiz} {harf_temiz} {sayi_temiz}", toplam_duzeltme


def _cekirdek_adaylari_uret(k, sadece_34=True, eksik_34_izin_ver=False, onek_cezasi=0.0, beklenen_h_len=None):
    sonuclar = []
    if 6 <= len(k) <= 9:
        il_ham, kalan = k[:2], k[2:]
        gecerli = {}
        for h_len in (1, 2, 3):
            if h_len < len(kalan):
                deger = _bolum_degerlendir(il_ham, kalan[:h_len], kalan[h_len:], sadece_34=sadece_34)
                if deger:
                    gecerli[h_len] = (kalan[:h_len], deger[0], deger[1])
        for h_len, (harf_ham, plaka, duzeltme) in gecerli.items():
            if beklenen_h_len in gecerli and h_len != beklenen_h_len:
                continue
            if harf_ham[-1].isdigit() and (h_len - 1) in gecerli and beklenen_h_len != h_len:
                continue
            if (h_len + 1) in gecerli and gecerli[h_len + 1][2] <= duzeltme and h_len == 1 and beklenen_h_len != 1:
                continue
            hiza_bonusu = -0.35 if beklenen_h_len == h_len else 0.0
            sonuclar.append((plaka, max(0.0, duzeltme + onek_cezasi + hiza_bonusu), False))

    if eksik_34_izin_ver and 5 <= len(k) <= 6:
        gecerli_kismi = {}
        for h_len in (1, 2, 3):
            if h_len < len(k):
                if k[:h_len] == "TR":
                    continue
                deger = _bolum_degerlendir("34", k[:h_len], k[h_len:], sadece_34=True)
                if deger and deger[1] <= 1:
                    gecerli_kismi[h_len] = (k[:h_len], deger[0], deger[1])
        for h_len, (harf_ham, plaka, duzeltme) in gecerli_kismi.items():
            if not (harf_ham[-1].isdigit() and (h_len - 1) in gecerli_kismi):
                sonuclar.append((plaka, duzeltme + onek_cezasi, True))
    return sonuclar


def plaka_adaylari_coz(metin, sadece_34=True, eksik_34_izin_ver=False):
    if not metin or not metin.strip():
        return []
    temiz = metin.upper().strip()
    if TELEFON_VEYA_GURULTU_REGEX.search(temiz):
        return []
    bloklar = re.findall(r"[A-Z0-9]+", temiz)
    if not bloklar:
        return []

    aday_sozluk = {}

    def _ekle(plaka, ceza, eksik_34):
        mevcut = aday_sozluk.get(plaka)
        if mevcut is None or (eksik_34, ceza) < (mevcut[1], mevcut[0]):
            aday_sozluk[plaka] = (ceza, eksik_34)

    for bas in range(len(bloklar)):
        for bitis in range(bas + 1, min(len(bloklar), bas + 5) + 1):
            dilim = bloklar[bas:bitis]
            birlestirilmis = "".join(dilim)
            beklenen_h = None
            if len(dilim) == 3 and dilim[0] in ("34", "TR34", "84") and 1 <= len(dilim[1]) <= 3:
                beklenen_h = len(dilim[1])
            elif len(dilim) == 4 and dilim[0] in ("TR", "IR", "1R") and dilim[1] == "34" and 1 <= len(dilim[2]) <= 3:
                beklenen_h = len(dilim[2])

            varyantlar = [(birlestirilmis, 0.0, beklenen_h)]
            if birlestirilmis.startswith("TR") and len(birlestirilmis) >= 7:
                varyantlar.append((birlestirilmis[2:], 0.0, beklenen_h))
            for bozuk in IL_34_BOZUK_ONEKLER:
                if birlestirilmis.startswith(bozuk) and len(birlestirilmis) >= len(bozuk) + 4:
                    varyantlar.append(("34" + birlestirilmis[len(bozuk):], 0.4, beklenen_h))

            # Handle bolt/screw read as a character right after '34' (e.g. '34 1 PBD 510' or 'TR 34 0 PBD 510').
            idx34 = birlestirilmis.find("34")
            if 0 <= idx34 <= 3:
                sonrasi = birlestirilmis[idx34 + 2:]
                if 5 <= len(sonrasi) <= 7:
                    varyantlar.append(("34" + sonrasi, 0.1 if idx34 > 0 else 0.0, beklenen_h))
                if 6 <= len(sonrasi) <= 8 and sonrasi[0] in "01IO8B":
                    varyantlar.append(("34" + sonrasi[1:], 0.45, None))

            # Handle '3' merged into the blue TR box so only '4 PBD 510' remains.
            if eksik_34_izin_ver and birlestirilmis.startswith(("4", "TR4")) and len(birlestirilmis) >= 6:
                kesik = birlestirilmis[3:] if birlestirilmis.startswith("TR4") else birlestirilmis[1:]
                varyantlar.append(("34" + kesik, 0.5, None))

            for dizgi, ceza_ek, bh in varyantlar:
                for plaka, ceza, eksik_34 in _cekirdek_adaylari_uret(dizgi, sadece_34, eksik_34_izin_ver, ceza_ek, bh):
                    _ekle(plaka, ceza, eksik_34)

    return [(p, b[0], b[1]) for p, b in sorted(aday_sozluk.items(), key=lambda x: (x[1][1], x[1][0]))]


def plaka_coz(metin, sadece_34=False, eksik_34_tamamla=True):
    for s34, eksik in ((True, False), (sadece_34, False), (True, eksik_34_tamamla)):
        adaylar = plaka_adaylari_coz(metin, sadece_34=s34, eksik_34_izin_ver=eksik)
        if adaylar:
            return adaylar[0][0]
    return None


def filo_plakalarini_cikar(satirlar):
    bilinen_plakalar, harf_serileri = {}, {}
    for row in (satirlar or [])[1:]:
        for hucre in row:
            deger = (hucre or "").strip().upper()
            if not deger:
                continue
            parcalar = deger.split()
            if not (len(parcalar) == 3 and tr_plaka_gecerli_mi(*parcalar)):
                cozulen = plaka_coz(deger, sadece_34=False, eksik_34_tamamla=False)
                if not cozulen:
                    continue
                parcalar = cozulen.split()
            kanonik = f"{parcalar[0]} {parcalar[1]} {parcalar[2]}"
            anahtar = "".join(parcalar)
            if anahtar in bilinen_plakalar:
                bilinen_plakalar[anahtar]["frekans"] += 1
            else:
                bilinen_plakalar[anahtar] = {
                    "plaka": kanonik, "il": parcalar[0], "harf": parcalar[1], "sayi": parcalar[2], "frekans": 1
                }
            harf_serileri[parcalar[1]] = harf_serileri.get(parcalar[1], 0) + 1
    return bilinen_plakalar, harf_serileri


def _dizgi_gorsel_mesafe(s1, s2, sadece_gorsel=False):
    if len(s1) != len(s2) or sum(1 for a, b in zip(s1, s2) if a != b) > 2:
        return 99.0
    toplam = 0.0
    for a, b in zip(s1, s2):
        if a == b:
            continue
        if b in GORSEL_BENZERLER.get(a, "") or a in GORSEL_BENZERLER.get(b, ""):
            toplam += 0.4
        elif sadece_gorsel:
            return 99.0
        else:
            toplam += 1.0
    return toplam


def filo_eslesmesi_bul(plaka, bilinen_plakalar, dusuk_guven=False):
    if not bilinen_plakalar:
        return None, 99.0
    parcalar = plaka.split()
    if len(parcalar) != 3:
        return None, 99.0
    il_a, harf_a, sayi_a = parcalar
    anahtar = f"{il_a}{harf_a}{sayi_a}"
    if anahtar in bilinen_plakalar:
        return bilinen_plakalar[anahtar], 0.0

    en_iyi_kayit, en_iyi_mesafe = None, 99.0
    for kayit in bilinen_plakalar.values():
        if kayit["il"] != il_a:
            continue
        harf_f, sayi_f, mesafe = kayit["harf"], kayit["sayi"], 99.0
        if len(harf_a) == len(harf_f) and len(sayi_a) == len(sayi_f):
            if harf_a == harf_f:
                mesafe = _dizgi_gorsel_mesafe(sayi_a, sayi_f, sadece_gorsel=not dusuk_guven)
            elif sayi_a == sayi_f:
                mesafe = _dizgi_gorsel_mesafe(harf_a, harf_f, sadece_gorsel=False)
            else:
                hm = _dizgi_gorsel_mesafe(harf_a, harf_f, sadece_gorsel=True)
                sm = _dizgi_gorsel_mesafe(sayi_a, sayi_f, sadece_gorsel=True)
                if hm < 10 and sm < 10:
                    mesafe = hm + sm
        elif harf_a == harf_f and len(harf_a) >= 2 and abs(len(sayi_a) - len(sayi_f)) == 1:
            kisa, uzun = (sayi_a, sayi_f) if len(sayi_a) < len(sayi_f) else (sayi_f, sayi_a)
            if uzun.startswith(kisa) or uzun.endswith(kisa):
                mesafe = 0.65
        elif sayi_a == sayi_f and len(sayi_a) >= 3 and abs(len(harf_a) - len(harf_f)) == 1:
            kisa, uzun = (harf_a, harf_f) if len(harf_a) < len(harf_f) else (harf_f, harf_a)
            if uzun.startswith(kisa) or uzun.endswith(kisa):
                mesafe = 0.65

        if mesafe < en_iyi_mesafe or (mesafe == en_iyi_mesafe and en_iyi_kayit and kayit["frekans"] > en_iyi_kayit["frekans"]):
            en_iyi_mesafe, en_iyi_kayit = mesafe, kayit

    return (en_iyi_kayit, en_iyi_mesafe) if en_iyi_mesafe <= 1.0 else (None, 99.0)


def kismi_filo_kurtar(metin, bilinen_plakalar):
    if not bilinen_plakalar or not metin or TELEFON_VEYA_GURULTU_REGEX.search(metin):
        return []
    s = re.sub(r"[^A-Z0-9]", "", metin.upper())
    if s.startswith("TR") and len(s) >= 6:
        s = s[2:]
    for bozuk in IL_34_BOZUK_ONEKLER:
        if s.startswith(bozuk) and len(s) >= len(bozuk) + 3:
            s = "34" + s[len(bozuk):]
            break
    if len(s) < 4 or not (any(c.isalpha() for c in s) and any(c.isdigit() for c in s)):
        return []

    bulunanlar = []
    for anahtar, kayit in bilinen_plakalar.items():
        if s == f"{kayit['harf']}{kayit['sayi']}":
            bulunanlar.append((kayit, 0.25))
        elif len(s) >= 6 and (anahtar.startswith(s) or anahtar.endswith(s)):
            bulunanlar.append((kayit, 0.55))
    return sorted(bulunanlar, key=lambda x: (x[1], -x[0]["frekans"]))


def kelimeleri_satirlara_ayir(detections):
    words = []
    for item in detections:
        if item.get("Type") != "WORD":
            continue
        box = item.get("Geometry", {}).get("BoundingBox", {})
        text = item.get("DetectedText", "").strip()
        if not text or "Top" not in box or "Left" not in box:
            continue
        h = box.get("Height", 0.02)
        words.append({
            "text": text, "left": box["Left"], "center_y": box["Top"] + h / 2,
            "height": h, "confidence": float(item.get("Confidence", 80.0)),
        })

    words.sort(key=lambda w: w["center_y"])
    lines = []
    for word in words:
        if not lines:
            lines.append([word])
            continue
        last = lines[-1]
        avg_y = sum(i["center_y"] for i in last) / len(last)
        avg_h = sum(i["height"] for i in last) / len(last)
        if abs(word["center_y"] - avg_y) <= max(avg_h, word["height"], 0.015) * 0.9:
            last.append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda i: i["left"]) for line in lines]


def plaka_tespit_et(img_bytes, satirlar=None):
    sonuc_detay = {"alternatif": None, "filo_duzeltmesi": None, "yeni_arac": False}
    try:
        bilinen_plakalar, harf_serileri = filo_plakalarini_cikar(satirlar or [])
        response = rekognition_client.detect_text(
            Image={"Bytes": img_bytes},
            Filters={"WordFilter": {"MinConfidence": 30}},
        )
        detections = response.get("TextDetections", [])
        tum_satirlar = [i.get("DetectedText", "") for i in detections if i.get("Type") == "LINE"]
        kelime_satirlari = kelimeleri_satirlara_ayir(detections)

        yukseklikler = [
            i.get("Geometry", {}).get("BoundingBox", {}).get("Height", 0.0)
            for i in detections if any(c.isalnum() for c in i.get("DetectedText", ""))
        ]
        max_height = max(yukseklikler, default=0.04) or 0.04

        parcalar = [
            (
                i.get("DetectedText", "").strip(),
                float(i.get("Confidence", 80.0)),
                i.get("Geometry", {}).get("BoundingBox", {}).get("Height", 0.03),
            )
            for i in detections if i.get("Type") == "LINE" and i.get("DetectedText", "").strip()
        ]
        for satir in kelime_satirlari:
            for bas in range(len(satir)):
                for n in (1, 2, 3, 4, 5):
                    if bas + n <= len(satir):
                        dilim = satir[bas:bas + n]
                        parcalar.append((
                            " ".join(w["text"] for w in dilim),
                            sum(w["confidence"] for w in dilim) / n,
                            sum(w["height"] for w in dilim) / n,
                        ))
        for idx in range(len(kelime_satirlari) - 1):
            ust, alt = kelime_satirlari[idx], kelime_satirlari[idx + 1]
            ust_y = sum(w["center_y"] for w in ust) / len(ust)
            alt_y = sum(w["center_y"] for w in alt) / len(alt)
            ort_h = (sum(w["height"] for w in ust) / len(ust) + sum(w["height"] for w in alt) / len(alt)) / 2
            if 0 < (alt_y - ust_y) <= max(ort_h * 3.0, 0.08):
                ort_g = (sum(w["confidence"] for w in ust) + sum(w["confidence"] for w in alt)) / (len(ust) + len(alt))
                parcalar.append((f"{' '.join(w['text'] for w in ust)} {' '.join(w['text'] for w in alt)}", ort_g, ort_h))

        aday_puanlari = {}

        def _kaydet(plaka, puan, ham=None, filo_mu=False):
            if plaka not in aday_puanlari or puan > aday_puanlari[plaka]["puan"]:
                aday_puanlari[plaka] = {"plaka": plaka, "puan": puan, "ham_plaka": ham, "filo_mu": filo_mu}

        for metin, guven, yukseklik in parcalar:
            oran = min(yukseklik / max(max_height, 0.02), 1.0)
            boyut_puani = (oran * 35.0) if oran >= 0.35 else -25.0

            for plaka, duzeltme, eksik_34 in plaka_adaylari_coz(metin, sadece_34=True, eksik_34_izin_ver=True):
                taban = guven + boyut_puani - (duzeltme * 22.0) + (25.0 if duzeltme == 0 and not eksik_34 else 0.0)
                filo_kayit, mesafe = filo_eslesmesi_bul(
                    plaka, bilinen_plakalar, dusuk_guven=(guven < 90.0 or duzeltme > 0 or eksik_34)
                )
                if filo_kayit and mesafe == 0.0:
                    bonus = (115.0 if eksik_34 else 160.0) + min(filo_kayit["frekans"] * 3.0, 30.0)
                    _kaydet(plaka, taban + bonus, filo_mu=True)
                elif filo_kayit and (not eksik_34 or mesafe <= 0.5):
                    fp = filo_kayit["plaka"]
                    bonus = (85.0 if eksik_34 else 125.0) - (mesafe * 35.0) + min(filo_kayit["frekans"] * 2.0, 20.0)
                    _kaydet(fp, taban + bonus, ham=(plaka if not eksik_34 and plaka != fp else None), filo_mu=True)
                    if not eksik_34 and duzeltme <= 1:
                        _kaydet(plaka, taban, filo_mu=False)
                elif not eksik_34:
                    seri_bonusu = 30.0 if plaka.split()[1] in harf_serileri else 0.0
                    _kaydet(plaka, taban + seri_bonusu, filo_mu=False)
                elif duzeltme == 0 and oran >= 0.4:
                    # All fleet vehicles start with 34: if '34' was obscured by glare/bolt, still accept clean HARF+SAYI.
                    seri_bonusu = 30.0 if plaka.split()[1] in harf_serileri else 0.0
                    _kaydet(plaka, taban - 20.0 + seri_bonusu, filo_mu=False)

            for filo_kayit, k_mesafe in kismi_filo_kurtar(metin, bilinen_plakalar):
                _kaydet(
                    filo_kayit["plaka"],
                    guven + boyut_puani + 90.0 - (k_mesafe * 30.0) + min(filo_kayit["frekans"] * 2.0, 20.0),
                    filo_mu=True,
                )

        if aday_puanlari:
            sirali = sorted(aday_puanlari.values(), key=lambda x: x["puan"], reverse=True)
            en_iyi = sirali[0]
            sonuc_detay["filo_duzeltmesi"] = en_iyi["ham_plaka"]
            sonuc_detay["yeni_arac"] = bool(bilinen_plakalar) and not en_iyi["filo_mu"]
            if en_iyi["ham_plaka"]:
                sonuc_detay["alternatif"] = en_iyi["ham_plaka"]
            elif len(sirali) > 1 and sirali[1]["puan"] >= en_iyi["puan"] - 45.0:
                sonuc_detay["alternatif"] = sirali[1]["plaka"]
            return en_iyi["plaka"], tum_satirlar, sonuc_detay

        return None, tum_satirlar, sonuc_detay
    except Exception as error:
        print(f"Rekognition error: {error}")
        return None, [], sonuc_detay


def _telegram_istek(metot, veri):
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN is missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metot}"
    payload = json.dumps(veri).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
    except Exception as error:
        print(f"Telegram API error ({metot}): {error}")


def telegram_mesaj_gonder(chat_id, metin, reply_markup=None):
    veri = {"chat_id": chat_id, "text": metin, "parse_mode": "Markdown"}
    if reply_markup:
        veri["reply_markup"] = reply_markup
    _telegram_istek("sendMessage", veri)


def telegram_mesaj_duzenle(chat_id, message_id, metin, reply_markup=None):
    veri = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": metin,
        "parse_mode": "Markdown",
        "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
    }
    _telegram_istek("editMessageText", veri)


def telegram_callback_yanitla(callback_query_id, metin=""):
    veri = {"callback_query_id": callback_query_id}
    if metin:
        veri["text"] = metin
    _telegram_istek("answerCallbackQuery", veri)


def webhook_callback_aktif_et():
    """Ensure Telegram webhook forwards callback_query events when inline buttons are clicked."""
    global _WEBHOOK_KONTROL_EDILDI
    if _WEBHOOK_KONTROL_EDILDI or not TELEGRAM_TOKEN:
        return
    _WEBHOOK_KONTROL_EDILDI = True
    try:
        info_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo"
        with urllib.request.urlopen(info_url, timeout=8) as response:
            bilgi = json.loads(response.read().decode("utf-8")).get("result") or {}
        mevcut_url = bilgi.get("url")
        allowed = bilgi.get("allowed_updates")
        if mevcut_url and isinstance(allowed, list) and "callback_query" not in allowed:
            yeni_allowed = list(dict.fromkeys(allowed + ["message", "callback_query"]))
            veri = {"url": mevcut_url, "allowed_updates": yeni_allowed}
            if TELEGRAM_WEBHOOK_SECRET:
                veri["secret_token"] = TELEGRAM_WEBHOOK_SECRET
            _telegram_istek("setWebhook", veri)
            print(f"Webhook updated with allowed_updates={yeni_allowed}")
    except Exception as error:
        print(f"Webhook check error: {error}")


def kayit_butonlari_olustur(plaka, alternatif=None):
    butonlar = []
    if alternatif and alternatif != plaka:
        degistir_data = f"degistir:{plaka}:{alternatif}"
        if len(degistir_data.encode("utf-8")) <= 64:
            butonlar.append([{"text": f"🔄 {alternatif} Yap", "callback_data": degistir_data}])
    sil_data = f"sil:{plaka}"
    if len(sil_data.encode("utf-8")) <= 64:
        butonlar.append([{"text": "🗑️ Yanlışsa Sil", "callback_data": sil_data}])
    return {"inline_keyboard": butonlar} if butonlar else None


def matris_to_xlsx(satirlar):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""")
        zip_file.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        zip_file.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""")
        zip_file.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Yikama Takip" sheetId="1" r:id="rId1"/></sheets>
</workbook>""")

        def col_harfi(index):
            result = ""
            index += 1
            while index > 0:
                index, remainder = divmod(index - 1, 26)
                result = chr(65 + remainder) + result
            return result

        rows_xml = []
        for row_index, row in enumerate(satirlar, 1):
            cells = []
            for column_index, value in enumerate(row):
                if value.strip():
                    escaped_value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    reference = f"{col_harfi(column_index)}{row_index}"
                    cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escaped_value}</t></is></c>')
            if cells:
                rows_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

        sheet_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>""" + "".join(rows_xml) + "</sheetData></worksheet>"
        zip_file.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def telegram_excel_gonder(chat_id):
    try:
        satirlar = s3_tabloyu_al()
    except S3TabloHatasi:
        telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
        return
    if not satirlar:
        telegram_mesaj_gonder(chat_id, "⚠️ Henuz kayitli veri bulunamadi.")
        return

    xlsx_bytes = matris_to_xlsx(satirlar)
    dosya_adi = f"yikama_takip_{bugunun_tarihi()}.xlsx"
    boundary = "----TelegramBoundaryXYZ123"
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode("utf-8"), b'Content-Disposition: form-data; name="chat_id"', b"", str(chat_id).encode("utf-8"),
        f"--{boundary}".encode("utf-8"), b'Content-Disposition: form-data; name="caption"', b"",
        f"📊 Guncel Yikama Tablosu ({datetime.now(TURKIYE_TZ).strftime('%d.%m.%Y %H:%M')})".encode("utf-8"),
        f"--{boundary}".encode("utf-8"),
        f'Content-Disposition: form-data; name="document"; filename="{dosya_adi}"'.encode("utf-8"),
        b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"", xlsx_bytes,
        f"--{boundary}--".encode("utf-8"), b"",
    ]
    body = crlf.join(parts)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as error:
        telegram_mesaj_gonder(chat_id, f"Dosya gonderilemedi: {error}")


def telegram_gorsel_indir(file_id):
    get_file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    with urllib.request.urlopen(get_file_url, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    file_path = result["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=15) as response:
        return response.read()


def s3_tabloyu_al():
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=S3_FILE_KEY)
        content = obj["Body"].read().decode("utf-8-sig", errors="ignore").replace("\ufeff", "").strip()
        if not content:
            return []
        return [[cell.replace("\ufeff", "").strip() for cell in line.split(";")] for line in content.splitlines() if line.strip()]
    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "NoSuchKey":
            return []
        print(f"S3 table read failed ({error_code}): {error}")
        raise S3TabloHatasi("S3 tablosu okunamadi") from error
    except BotoCoreError as error:
        print(f"S3 table read failed: {error}")
        raise S3TabloHatasi("S3 tablosu okunamadi") from error


def s3_tablo_kaydet(satirlar):
    yeni_csv = "\r\n".join(";".join(row) for row in satirlar) + "\r\n"
    try:
        s3_client.put_object(Bucket=S3_BUCKET, Key=S3_FILE_KEY, Body=yeni_csv.encode("utf-8-sig"))
    except (ClientError, BotoCoreError) as error:
        print(f"S3 table write failed: {error}")
        raise S3TabloHatasi("S3 tablosu kaydedilemedi") from error


def _sutun_yaz_ve_kaydet(satirlar, basliklar, sutun_index, yeni_liste):
    for index in range(max(len(satirlar) - 1, len(yeni_liste))):
        row_index = index + 1
        if row_index >= len(satirlar):
            satirlar.append([""] * len(basliklar))
        while len(satirlar[row_index]) <= sutun_index:
            satirlar[row_index].append("")
        satirlar[row_index][sutun_index] = yeni_liste[index] if index < len(yeni_liste) else ""
    while len(satirlar) > 1 and all(cell == "" for cell in satirlar[-1]):
        satirlar.pop()
    s3_tablo_kaydet(satirlar)


def s3_plaka_ekle(plaka, satirlar=None):
    bugun = bugunun_tarihi()
    if satirlar is None:
        satirlar = s3_tabloyu_al()
    if not satirlar or not satirlar[0] or satirlar[0] == [""]:
        s3_tablo_kaydet([[bugun], [plaka]])
        return "EKLENDI", 1

    basliklar = [header.replace("\ufeff", "").strip() for header in satirlar[0]]
    if bugun not in basliklar:
        basliklar.append(bugun)
        satirlar[0] = basliklar
        for index in range(1, len(satirlar)):
            satirlar[index].append("")
        satirlar.append([""] * (len(basliklar) - 1) + [plaka])
        s3_tablo_kaydet(satirlar)
        return "EKLENDI", 1

    sutun_index = basliklar.index(bugun)
    mevcut = [row[sutun_index] for row in satirlar[1:] if sutun_index < len(row) and row[sutun_index]]
    if plaka in mevcut:
        return "MUKERRER", len(mevcut)
    for row in satirlar[1:]:
        while len(row) <= sutun_index:
            row.append("")
        if not row[sutun_index]:
            row[sutun_index] = plaka
            s3_tablo_kaydet(satirlar)
            return "EKLENDI", len(mevcut) + 1
    satirlar.append([""] * sutun_index + [plaka])
    s3_tablo_kaydet(satirlar)
    return "EKLENDI", len(mevcut) + 1


def s3_plaka_sil(hedef_plaka=None):
    bugun = bugunun_tarihi()
    satirlar = s3_tabloyu_al()
    if not satirlar or not satirlar[0]:
        return "BOS", None, 0
    basliklar = [header.replace("\ufeff", "").strip() for header in satirlar[0]]
    if bugun not in basliklar:
        return "BOS", None, 0

    sutun_index = basliklar.index(bugun)
    mevcut = [row[sutun_index].strip() for row in satirlar[1:] if sutun_index < len(row) and row[sutun_index].strip()]
    if not mevcut:
        return "BOS", None, 0

    if hedef_plaka is None:
        silinen_plaka, yeni_liste = mevcut.pop(), mevcut
    else:
        temiz_hedef = hedef_plaka.upper().replace(" ", "")
        silinen_plaka, yeni_liste = None, []
        for plaka in mevcut:
            if silinen_plaka is None and plaka.upper().replace(" ", "") == temiz_hedef:
                silinen_plaka = plaka
            else:
                yeni_liste.append(plaka)
        if silinen_plaka is None:
            return "BULUNAMADI", hedef_plaka, len(mevcut)

    _sutun_yaz_ve_kaydet(satirlar, basliklar, sutun_index, yeni_liste)
    return "SILINDI", silinen_plaka, len(yeni_liste)


def s3_plaka_degistir(eski_plaka, yeni_plaka):
    bugun = bugunun_tarihi()
    satirlar = s3_tabloyu_al()
    if not satirlar or not satirlar[0]:
        return s3_plaka_ekle(yeni_plaka, satirlar=satirlar)
    basliklar = [header.replace("\ufeff", "").strip() for header in satirlar[0]]
    if bugun not in basliklar:
        return s3_plaka_ekle(yeni_plaka, satirlar=satirlar)

    sutun_index = basliklar.index(bugun)
    mevcut = [row[sutun_index].strip() for row in satirlar[1:] if sutun_index < len(row) and row[sutun_index].strip()]
    temiz_eski = eski_plaka.upper().replace(" ", "")
    temiz_yeni = yeni_plaka.upper().replace(" ", "")

    yeni_liste, degisti = [], False
    for plaka in mevcut:
        if not degisti and plaka.upper().replace(" ", "") == temiz_eski:
            if not any(p.upper().replace(" ", "") == temiz_yeni for p in mevcut):
                yeni_liste.append(yeni_plaka)
            degisti = True
        else:
            yeni_liste.append(plaka)
    if not degisti and not any(p.upper().replace(" ", "") == temiz_yeni for p in yeni_liste):
        yeni_liste.append(yeni_plaka)

    _sutun_yaz_ve_kaydet(satirlar, basliklar, sutun_index, yeni_liste)
    return "DEGISTIRILDI", len(yeni_liste)


def _callback_isle(callback_query):
    cb_id = callback_query.get("id")
    msg = callback_query.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data = (callback_query.get("data") or "").strip()

    if not chat_id or not chat_yetkili_mi(chat_id):
        if cb_id:
            telegram_callback_yanitla(cb_id, "Yetkisiz islem")
        return {"statusCode": 200, "body": "ok"}

    try:
        if data.startswith("sil:"):
            hedef = data.split(":", 1)[1].strip()
            durum, silinen, kalan = s3_plaka_sil(hedef)
            if durum == "SILINDI":
                telegram_callback_yanitla(cb_id, f"{silinen} silindi")
                telegram_mesaj_duzenle(chat_id, message_id, f"🗑️ `{silinen}` bugunun listesinden geri alindi.\n📊 Kalan toplam: *{kalan} arac*")
            else:
                telegram_callback_yanitla(cb_id, "Kayit zaten silinmis")
                telegram_mesaj_duzenle(chat_id, message_id, f"ℹ️ `{hedef}` bugunun listesinde bulunamadi.")
        elif data.startswith("degistir:"):
            parcalar = data.split(":", 2)
            if len(parcalar) == 3:
                eski_plaka, yeni_plaka = parcalar[1].strip(), parcalar[2].strip()
                _, toplam = s3_plaka_degistir(eski_plaka, yeni_plaka)
                telegram_callback_yanitla(cb_id, f"{yeni_plaka} olarak guncellendi")
                telegram_mesaj_duzenle(
                    chat_id,
                    message_id,
                    f"✅ *Plaka Guncellendi:* `{yeni_plaka}`\n(Eski kayit `{eski_plaka}` degistirildi)\n📊 Bugunku toplam: *{toplam} arac*",
                    reply_markup=kayit_butonlari_olustur(yeni_plaka),
                )
    except S3TabloHatasi:
        if cb_id:
            telegram_callback_yanitla(cb_id, "Tabloya su an erisilemiyor")
    except Exception as error:
        print(f"Callback processing error: {error}")
        if cb_id:
            telegram_callback_yanitla(cb_id, "Islem sirasinda hata olustu")
    return {"statusCode": 200, "body": "ok"}


def lambda_handler(event, context):
    if not telegram_webhook_gecerli_mi(event):
        return {"statusCode": 401, "body": "unauthorized"}

    try:
        raw_body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        body = json.loads(raw_body)
    except Exception:
        return {"statusCode": 200, "body": "ok"}

    if "callback_query" in body:
        return _callback_isle(body["callback_query"])

    msg = body.get("message")
    if not msg or "chat" not in msg:
        return {"statusCode": 200, "body": "ok"}

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    command, argument = komutu_ayir(text)

    if command == "/id":
        telegram_mesaj_gonder(chat_id, f"Bu sohbetin Chat ID'si: `{chat_id}`")
        return {"statusCode": 200, "body": "ok"}

    if not chat_yetkili_mi(chat_id):
        print(f"Unauthorized chat rejected: {chat_id}")
        return {"statusCode": 200, "body": "ok"}

    webhook_callback_aktif_et()

    if "photo" in msg:
        try:
            satirlar = s3_tabloyu_al()
        except S3TabloHatasi:
            telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
            return {"statusCode": 200, "body": "ok"}

        try:
            file_id = msg["photo"][-1]["file_id"]
            plaka, okunanlar, detay_bilgi = plaka_tespit_et(telegram_gorsel_indir(file_id), satirlar=satirlar)
            if plaka:
                durum, toplam = s3_plaka_ekle(plaka, satirlar=satirlar)
                alternatif = detay_bilgi.get("alternatif")
                filo_duzeltmesi = detay_bilgi.get("filo_duzeltmesi")
                butonlar = kayit_butonlari_olustur(plaka, alternatif=alternatif)

                if durum == "MUKERRER":
                    ek_not = f"\n🔧 (Okunan `{filo_duzeltmesi}` filo kaydina gore eslestirildi)" if filo_duzeltmesi else ""
                    telegram_mesaj_gonder(chat_id, f"ℹ️ *Plaka:* `{plaka}`{ek_not}\n(Bu arac bugun zaten kaydedilmis. Toplam: {toplam})", reply_markup=butonlar)
                else:
                    ek_not = f"\n🔧 (Okunan `{filo_duzeltmesi}` filo kaydina gore duzeltildi)" if filo_duzeltmesi else (" (🆕 Yeni Arac)" if detay_bilgi.get("yeni_arac") else "")
                    telegram_mesaj_gonder(chat_id, f"✅ *Plaka Kaydedildi:* `{plaka}`{ek_not}\n📊 Bugunku toplam: *{toplam} arac*", reply_markup=butonlar)
            else:
                detay = ", ".join(okunanlar[:12]) if okunanlar else "Yazi bulunamadi"
                telegram_mesaj_gonder(chat_id, f"⚠️ Plaka okunamadi.\n🔍 Algilananlar: {detay}\nManuel eklemek icin: `/ekle 34ABC123`")
        except S3TabloHatasi:
            telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
        except Exception as error:
            print(f"Photo processing error: {error}")
            telegram_mesaj_gonder(chat_id, "⚠️ Fotograf islenirken sistem hatasi olustu.")
        return {"statusCode": 200, "body": "ok"}

    if command == "/excel":
        telegram_excel_gonder(chat_id)
    elif command == "/bugun":
        bugun = bugunun_tarihi()
        try:
            satirlar = s3_tabloyu_al()
        except S3TabloHatasi:
            telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
            return {"statusCode": 200, "body": "ok"}
        if satirlar and satirlar[0] and bugun in satirlar[0]:
            sutun_index = satirlar[0].index(bugun)
            plakalar = [row[sutun_index] for row in satirlar[1:] if sutun_index < len(row) and row[sutun_index]]
            if plakalar:
                lines = [f"📅 Bugun Yikanan Araclar ({bugun}) - Toplam: {len(plakalar)}", ""]
                lines.extend(f"{index}. `{plaka}`" for index, plaka in enumerate(plakalar, 1))
                telegram_mesaj_gonder(chat_id, "\n".join(lines))
            else:
                telegram_mesaj_gonder(chat_id, f"Bugun ({bugun}) henuz arac kaydi yok.")
        else:
            telegram_mesaj_gonder(chat_id, f"Bugun ({bugun}) henuz arac kaydi yok.")
    elif command == "/sil":
        try:
            durum, silinen, kalan = s3_plaka_sil(argument or None)
        except S3TabloHatasi:
            telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
            return {"statusCode": 200, "body": "ok"}
        if durum == "SILINDI":
            if argument:
                telegram_mesaj_gonder(chat_id, f"🗑️ `{silinen}` bugunun listesinden silindi!\n📊 Kalan toplam: *{kalan} arac*")
            else:
                telegram_mesaj_gonder(chat_id, f"🗑️ En son eklenen `{silinen}` silindi!\n📊 Kalan toplam: *{kalan} arac*")
        elif argument:
            telegram_mesaj_gonder(chat_id, f"ℹ️ `{argument.upper()}` bugunun listesinde bulunamadi.")
        else:
            telegram_mesaj_gonder(chat_id, "ℹ️ Bugun silinecek herhangi bir arac kaydi bulunmuyor.")
    elif command == "/ekle":
        plaka = plaka_coz(argument) if argument else None
        if not plaka:
            telegram_mesaj_gonder(chat_id, "Gecerli bir TR plaka giriniz (orn: `/ekle 34ABC123`).")
        else:
            try:
                durum, toplam = s3_plaka_ekle(plaka)
            except S3TabloHatasi:
                telegram_mesaj_gonder(chat_id, "⚠️ Tabloya su an erisilemiyor. Hicbir kayit degistirilmedi.")
                return {"statusCode": 200, "body": "ok"}
            if durum == "MUKERRER":
                telegram_mesaj_gonder(chat_id, f"ℹ️ `{plaka}` bugun zaten kayitliydi.")
            else:
                telegram_mesaj_gonder(chat_id, f"✅ `{plaka}` eklendi! (Toplam: {toplam})")
    elif command in {"/start", "/yardim"}:
        telegram_mesaj_gonder(chat_id, "🚗 *Oto Yikama Plaka Botu:*\n\n📸 Fotograf gonderin (otomatik kaydeder)\n📥 `/excel` - Excel tablosunu indirir\n📅 `/bugun` - Bugunku araclari gosterir\n✍️ `/ekle 34ABC123` - Manuel plaka ekler\n🗑️ `/sil` - En son eklenen araci siler\n🗑️ `/sil 34ABC123` - Belirli bir plakayi siler")

    return {"statusCode": 200, "body": "ok"}
