
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

# Başlangıç süresini kısaltmak için OS tema tespitini kapattık
os.environ["CUSTOMTKINTER_DISABLE_DARKDETECT"] = "1"

import customtkinter as ctk


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_CONNECT_TIMEOUT_S = 2
REDIS_SOCKET_TIMEOUT_S = 2

KANAL_TELEMETRI = "ihasinyal"
KANAL_ACIL = "acil_durum"

UI_POLL_MS = 200
UYARI_TUT_S = 8.0


NORMAL_PROFIL = {
    "hiz_min": 120.0,
    "hiz_max": 280.0,
    "irtifa_min": 500.0,
    "irtifa_max": 5200.0,
    "max_hiz_degisim": 25.0,      # km/s per second
    "max_irtifa_degisim": 40.0,   # m per second
    "min_batarya": 0.0,
    "max_batarya": 100.0,
}


_redis_lock = threading.Lock()
_redis_client = None


def redis_baglan():
    """Redis client döndürür; başarısızsa None."""
    global _redis_client
    with _redis_lock:
        try:
            import redis

            _redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT_S,
                socket_timeout=REDIS_SOCKET_TIMEOUT_S,
            )
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None
            return None


def redis_dinle_thread(veri_kuyrugu: "queue.Queue[dict[str, Any]]", durum_dict: dict) -> None:
    while True:
        r = redis_baglan()
        if r is None:
            durum_dict["bagli"] = False
            durum_dict["mesaj"] = "Redis bağlantısı yok. Yeniden deneniyor..."
            threading.Event().wait(2)
            continue

        durum_dict["bagli"] = True
        durum_dict["mesaj"] = "Bağlı"

        try:
            pubsub = r.pubsub()
            pubsub.subscribe(KANAL_TELEMETRI)
            for mesaj in pubsub.listen():
                if mesaj.get("type") != "message":
                    continue
                try:
                    veri = json.loads(mesaj["data"])
                    if isinstance(veri, dict):
                        veri_kuyrugu.put(veri)
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as e:
            durum_dict["bagli"] = False
            durum_dict["mesaj"] = f"Hata: {str(e)[:40]}"
            try:
                r.close()
            except Exception:
                pass
            threading.Event().wait(2)


def panel_olustur(parent, baslik: str, **kwargs):
    frame = ctk.CTkFrame(parent, fg_color=("gray20", "gray15"), corner_radius=8, **kwargs)

    lbl_baslik = ctk.CTkLabel(frame, text=baslik, font=ctk.CTkFont(size=18, weight="bold"))
    lbl_baslik.pack(pady=(12, 8))

    lbl_uyari = ctk.CTkLabel(
        frame,
        text="",
        font=ctk.CTkFont(size=12, weight="bold"),
        text_color="#FF3B30",
    )
    lbl_uyari.pack(pady=(0, 6))

    satirlar = []
    alanlar = [
        ("hiz", "Hız (km/s)"),
        ("irtifa", "İrtifa (m)"),
        ("batarya", "Batarya (%)"),
        ("x", "X Koordinat"),
        ("y", "Y Koordinat"),
    ]
    for key, etiket in alanlar:
        satir = ctk.CTkFrame(frame, fg_color="transparent")
        satir.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(satir, text=f"{etiket}:", anchor="w", width=140).pack(side="left")
        val = ctk.CTkLabel(satir, text="--", anchor="w", text_color=("gray70", "gray75"))
        val.pack(side="left", fill="x", expand=True)
        satirlar.append((key, val))

    durum = ctk.CTkLabel(frame, text="Son veri: bekleniyor...", font=ctk.CTkFont(size=11), text_color=("gray60", "gray65"))
    durum.pack(pady=(8, 12))
    return frame, dict(satirlar), durum, lbl_uyari


def anomali_kontrol(veri: dict, onceki: Optional[dict]) -> tuple[bool, str]:
    try:
        hiz = float(veri.get("hiz"))
        irtifa = float(veri.get("irtifa"))
        batarya = float(veri.get("batarya"))
    except Exception:
        return True, "Telemetri formatı hatalı"

    if not (NORMAL_PROFIL["hiz_min"] <= hiz <= NORMAL_PROFIL["hiz_max"]):
        return True, f"Hız sınır dışı: {hiz}"
    if not (NORMAL_PROFIL["irtifa_min"] <= irtifa <= NORMAL_PROFIL["irtifa_max"]):
        return True, f"İrtifa sınır dışı: {irtifa}"
    if not (NORMAL_PROFIL["min_batarya"] <= batarya <= NORMAL_PROFIL["max_batarya"]):
        return True, f"Batarya sınır dışı: {batarya}"

    if onceki:
        try:
            t1 = float(veri.get("ts", time.time()))
            t0 = float(onceki.get("ts", t1 - 1.0))
            dt = max(0.2, min(5.0, t1 - t0))
            hiz0 = float(onceki.get("hiz"))
            irtifa0 = float(onceki.get("irtifa"))
            if abs(hiz - hiz0) / dt > NORMAL_PROFIL["max_hiz_degisim"]:
                return True, f"Hız sıçraması: Δ={hiz - hiz0:.1f}/dt={dt:.1f}s"
            if abs(irtifa - irtifa0) / dt > NORMAL_PROFIL["max_irtifa_degisim"]:
                return True, f"İrtifa sıçraması: Δ={irtifa - irtifa0:.1f}/dt={dt:.1f}s"
        except Exception:
            pass

    return False, ""


@dataclass
class PanelState:
    g: dict
    durum: Any
    uyari: Any
    onceki: Optional[dict] = None
    uyari_until: float = 0.0
    uyari_text: str = ""


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    ana = ctk.CTk()
    ana.title("İHA Komuta Kontrol ve Haberleşme Sistemi")
    ana.geometry("900x580")
    ana.minsize(800, 500)

    ust = ctk.CTkFrame(ana, fg_color="transparent")
    ust.pack(fill="x", padx=10, pady=(10, 0))
    ctk.CTkLabel(ust, text="Yedekli Kesintisiz Veri Akışı — Redis Pub/Sub", font=ctk.CTkFont(size=14)).pack()

    ic = ctk.CTkFrame(ana, fg_color="transparent")
    ic.pack(fill="both", expand=True)
    ic.columnconfigure(0, weight=1)
    ic.columnconfigure(1, weight=1)
    ic.rowconfigure(0, weight=1)

    p1, gosterge1, durum1, uyari1 = panel_olustur(ic, "SUNUCU-1 (AKTİF)")
    p1.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
    p2, gosterge2, durum2, uyari2 = panel_olustur(ic, "SUNUCU-2 (YEDEK)")
    p2.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

    paneller = [
        PanelState(g=gosterge1, durum=durum1, uyari=uyari1),
        PanelState(g=gosterge2, durum=durum2, uyari=uyari2),
    ]

    veri_kuyrugu: "queue.Queue[dict[str, Any]]" = queue.Queue()
    baglanti_durumu = {"bagli": True, "mesaj": "Bağlantı kuruluyor..."}

    def kuyruk_guncelle():
        now = time.time()
        try:
            while True:
                veri = veri_kuyrugu.get_nowait()
                for p in paneller:
                    for key, lbl in p.g.items():
                        if key in veri:
                            lbl.configure(text=str(veri[key]))
                    p.durum.configure(text="Son veri: anlık")

                    if veri.get("anomali") is True:
                        neden = veri.get("anomali_nedeni") or "Beklenmeyen değer"
                        p.uyari_text = f"UYARI: {neden}"
                        p.uyari_until = now + UYARI_TUT_S

                    anomali, neden2 = anomali_kontrol(veri, p.onceki)
                    if anomali:
                        p.uyari_text = f"UYARI: {neden2}"
                        p.uyari_until = now + UYARI_TUT_S

                    p.onceki = veri
        except queue.Empty:
            pass

        for p in paneller:
            if p.uyari_until > now:
                p.uyari.configure(text=p.uyari_text)
            else:
                p.uyari.configure(text="")

        if not baglanti_durumu.get("bagli", True):
            for p in paneller:
                p.durum.configure(text=baglanti_durumu.get("mesaj", "Bağlantı yok"))
        ana.after(UI_POLL_MS, kuyruk_guncelle)

    # Bağlantı durumu çubuğu (en alttaki kırmızı butonun hemen üstünde)
    alt_bar = ctk.CTkFrame(ana, fg_color=("gray25", "gray20"), height=28)
    lbl_durum = ctk.CTkLabel(alt_bar, text="Redis: Bağlantı kuruluyor...", font=ctk.CTkFont(size=11))
    lbl_durum.pack(side="left", padx=10, pady=4)

    def durum_bar_guncelle():
        lbl_durum.configure(text=f"Redis: {baglanti_durumu.get('mesaj', '?')}")
        ana.after(1000, durum_bar_guncelle)

    # Acil durum butonu — en alta, büyük kırmızı buton
    def acil_durum_gonder():
        r = redis_baglan()
        if r:
            try:
                r.publish(KANAL_ACIL, "STOP")
                lbl_durum.configure(text="Redis: ACİL DURUM (STOP) gönderildi.")
            except Exception as e:
                lbl_durum.configure(text=f"Gönderilemedi: {e}")
        else:
            lbl_durum.configure(text="Redis bağlı değil; STOP gönderilemedi.")

    acil_cerceve = ctk.CTkFrame(ana, fg_color="transparent")
    btn_acil = ctk.CTkButton(
        acil_cerceve,
        text="ACİL DURUM — SİSTEMİ KAPAT",
        font=ctk.CTkFont(size=16, weight="bold"),
        fg_color="#8B0000",
        hover_color="#A52A2A",
        height=50,
        command=acil_durum_gonder,
    )
    btn_acil.pack(fill="x", pady=4)
    acil_cerceve.pack(fill="x", padx=10, pady=10, side="bottom")
    alt_bar.pack(fill="x", side="bottom")
    ana.after(1000, durum_bar_guncelle)

    t = threading.Thread(target=redis_dinle_thread, args=(veri_kuyrugu, baglanti_durumu), daemon=True)
    t.start()

    ana.after(UI_POLL_MS, kuyruk_guncelle)

    ana.mainloop()


if __name__ == "__main__":
    main()
