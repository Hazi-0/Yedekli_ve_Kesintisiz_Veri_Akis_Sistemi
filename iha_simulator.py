
import time
import random
import json
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Optional


# Redis
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_CONNECT_TIMEOUT_S = 2
REDIS_SOCKET_TIMEOUT_S = 2

KANAL_TELEMETRI = "ihasinyal"


# Yayın parametreleri
PUBLISH_PERIOD_S = 1.0
ANOMALI_PERIYODU = 30  # her 30 veriden 1'i gibi


# “Normal” uçuş zarfı (arayüzde de benzeri kontrol ediliyor)
HIZ_MIN, HIZ_MAX = 120.0, 280.0          # km/s
IRTIFA_MIN, IRTIFA_MAX = 500.0, 5200.0   # m
MAX_IVME = 15.0                          # (km/s)/s (demo için kaba limit)
MAX_TIRMANIS = 25.0                      # m/s


@dataclass
class UcusDurumu:
    t0: float
    hiz: float
    irtifa: float
    batarya: float
    x: float
    y: float
    heading_deg: float


def redis_baglan():

    try:
        import redis as redis_mod

        r = redis_mod.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_S,
            socket_timeout=REDIS_SOCKET_TIMEOUT_S,
        )
        r.ping()
        return r
    except Exception as e:
        print(f"[HATA] Redis bağlantısı kurulamadı: {e}")
        return None


def redis_baglan_bekle() -> Any:
    """Redis erişilebilir olana kadar bloklar (simülatör çökmesin diye)."""
    print("Redis bağlantısı bekleniyor...")
    r = redis_baglan()
    while r is None:
        time.sleep(2)
        r = redis_baglan()
    print("Redis bağlantısı kuruldu.")
    return r


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def telemetri_uret(st: UcusDurumu, dt: float, seq: int) -> dict[str, Any]:
    """Bir adım telemetri üretir; periyodik anomali enjekte eder."""
    t = time.time()

    # Hız: referansa 1. dereceden yaklaş + ivme limiti
    hedef_hiz = 200.0 + 25.0 * math.sin((t - st.t0) / 40.0) + random.uniform(-5.0, 5.0)
    hiz_delta = clamp((hedef_hiz - st.hiz) * 0.35, -MAX_IVME * dt, MAX_IVME * dt)
    st.hiz = clamp(st.hiz + hiz_delta, HIZ_MIN, HIZ_MAX)

    # İrtifa: referansa yaklaş + tırmanış limiti
    hedef_irtifa = 3000.0 + 700.0 * math.sin((t - st.t0) / 55.0) + random.uniform(-30.0, 30.0)
    irtifa_hizi = clamp((hedef_irtifa - st.irtifa) * 0.12, -MAX_TIRMANIS, MAX_TIRMANIS)
    st.irtifa = clamp(st.irtifa + irtifa_hizi * dt, IRTIFA_MIN, IRTIFA_MAX)

    # Heading + konum (demo kinematik)
    st.heading_deg = (st.heading_deg + random.uniform(-3.0, 3.0)) % 360.0
    step = (st.hiz * dt) / 111_000.0
    st.x += step * math.cos(math.radians(st.heading_deg))
    st.y += step * math.sin(math.radians(st.heading_deg))

    # Batarya (heuristic)
    tuketim = 0.04 + (st.hiz / HIZ_MAX) * 0.06 + abs(hiz_delta) * 0.002
    st.batarya = max(0.0, st.batarya - tuketim)

    anomali = False
    anomali_nedeni = ""

    if seq % ANOMALI_PERIYODU == 0:
        anomali = True
        secim = random.choice(["asiri_ivme", "asiri_tirmanis", "sensor_sicrama", "batarya_sicrama"])
        if secim == "asiri_ivme":
            st.hiz = min(HIZ_MAX + 120.0, st.hiz + random.uniform(60.0, 120.0))
            anomali_nedeni = "Aşırı ivme/hız sıçraması"
        elif secim == "asiri_tirmanis":
            st.irtifa = min(IRTIFA_MAX + 2000.0, st.irtifa + random.uniform(800.0, 2000.0))
            anomali_nedeni = "Aşırı tırmanış/irtifa sıçraması"
        elif secim == "sensor_sicrama":
            st.x += random.uniform(0.2, 0.6)
            st.y += random.uniform(0.2, 0.6)
            anomali_nedeni = "Konum sensörü sıçraması"
        else:
            st.batarya = max(0.0, st.batarya - random.uniform(10.0, 35.0))
            anomali_nedeni = "Batarya değeri beklenmedik düşüş"

    return {
        "ts": t,
        "seq": seq,
        "hiz": round(st.hiz, 2),
        "irtifa": round(st.irtifa, 1),
        "batarya": round(st.batarya, 1),
        "x": round(st.x, 6),
        "y": round(st.y, 6),
        "anomali": anomali,
        "anomali_nedeni": anomali_nedeni,
    }


def main() -> None:
    print(f"İHA Simülatörü: Redis `{KANAL_TELEMETRI}` kanalına yayın yapacak ({REDIS_HOST}:{REDIS_PORT}).")
    r = redis_baglan_bekle()

    st = UcusDurumu(
        t0=time.time(),
        hiz=random.uniform(160.0, 210.0),
        irtifa=random.uniform(2200.0, 3200.0),
        batarya=random.uniform(70.0, 100.0),
        x=random.uniform(39.2, 39.8),
        y=random.uniform(32.2, 32.8),
        heading_deg=random.uniform(0.0, 360.0),
    )

    seq = 0
    while True:
        seq += 1
        telemetri = telemetri_uret(st, PUBLISH_PERIOD_S, seq)

        try:
            r.publish(KANAL_TELEMETRI, json.dumps(telemetri))
            flag = " [ANOMALİ]" if telemetri["anomali"] else ""
            neden = f" | Neden:{telemetri['anomali_nedeni']}" if telemetri["anomali"] else ""
            print(
                f"Seq:{telemetri['seq']} -> Hız:{telemetri['hiz']} | İrtifa:{telemetri['irtifa']} | "
                f"Batarya:%{telemetri['batarya']} | X:{telemetri['x']} Y:{telemetri['y']}{flag}{neden}"
            )
        except Exception as e:
            print(f"[HATA] Publish başarısız: {e}. Yeniden bağlanılıyor...")
            r = redis_baglan_bekle()

        time.sleep(PUBLISH_PERIOD_S)


if __name__ == "__main__":
    main()
