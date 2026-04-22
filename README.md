# Nöbetçi Eczane Scraper

Türkiye genelindeki 82 şehir için `eczaneler.gen.tr`'den nöbetçi eczane verisini çeker, PostgreSQL'e yazar ve Upstash Redis'e şehir bazlı önbellekler.

## Mimari

- **Fetch:** `curl_cffi` — Chrome/Safari/Edge TLS fingerprint'lerini impersonate ederek Cloudflare bypass, exponential backoff ile yeniden deneme.
- **Runtime:** Claude Code **routine** — günde bir kez Anthropic cloud sandbox'ında çalışır, bu repo'yu clone eder ve `python eczane_scraper_requests.py` komutunu koşturur.
- **Storage:** PostgreSQL (SQLAlchemy upsert, `pharmacies` tablosu) + Upstash Redis REST (günlük şehir bazlı snapshot).

## Veri Yapısı

```json
{
  "city": "İstanbul",
  "district": "Kadıköy",
  "pharmacy": "Merkez Eczanesi",
  "address": "Caferağa Mah. Moda Cad. No:12",
  "phone": "0 (216) 123-45-67",
  "date": "2026-04-22",
  "latitude": null,
  "longitude": null
}
```

## Lokal çalıştırma

```bash
pip install -r requirements.txt
```

`.env`:

```env
DATABASE_URL=postgresql://user:pass@host:port/db
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx
```

```bash
python eczane_scraper_requests.py
```

## Günlük schedule: Claude routines

Scraper, Anthropic cloud sandbox'ında günde bir kez çalışan bir **Claude routine** olarak kuruludur. Routine repo'yu clone eder, bağımlılıkları kurar, secret'ları export edip script'i koşturur.

- **Zamanlama:** Her gün 09:30 Europe/Istanbul (06:30 UTC), cron `30 6 * * *`
- **Repo:** Public (routine auth olmadan clone eder)
- **Secrets:** `DATABASE_URL`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` — routine prompt'unda inline; Anthropic routines config'inde saklanır.

Routine'u yönetmek için: <https://claude.ai/code/routines>

## Redis key formatı

```
{Şehir}:{YYYY-MM-DD}
İstanbul:2026-04-22
Ankara:2026-04-22
```

## Sorun giderme

- **403 Forbidden log'da:** Sandbox çıkış IP'si Cloudflare'a takılmış olabilir. Routine prompt'una bir residential/free proxy katmanı eklenebilir veya alternatif runtime (Fly.io, Render.com cron, Oracle Cloud Free VM) değerlendirilebilir.
- **`curl_cffi` kurulumu başarısız:** Sandbox Python sürümünü kontrol et — wheel Python 3.8+'a gereksinir.
- **Secrets değişti:** Routine config'ini `claude.ai/code/routines` üzerinden düzenle; prompt'taki env export satırlarını güncelle.

## Lisans

MIT
