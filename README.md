# Nöbetçi Eczane Scraper 🏥

Türkiye genelindeki **82 şehir** ve tüm ilçelerden nöbetçi eczane verilerini çeken, PostgreSQL ve Redis'e kaydeden scraper.

## Özellikler

- 🧅 **Tor Proxy** - Cloudflare bypass için zorunlu
- 🚀 **Paralel İşlem** - 5 worker ile hızlı scraping
- 📍 **Koordinat Çekme** - Detay sayfalarından lat/long
- 🏘️ **İlçe Bazlı** - Doğru ilçe bilgisi (URL'den)
- 💾 **PostgreSQL** - Kalıcı veri depolama
- ⚡ **Redis Cache** - Şehir bazlı hızlı erişim
- 📅 **Günlük Çalışma** - GitHub Actions cron job

## Veri Yapısı

```json
{
  "city": "İstanbul",
  "district": "Kadıköy",
  "pharmacy": "Merkez Eczanesi",
  "address": "Caferağa Mah. Moda Cad. No:12",
  "phone": "0 (216) 123-45-67",
  "date": "2026-01-11",
  "latitude": "40.987654",
  "longitude": "29.012345"
}
```

## Kurulum

### Gereksinimler

```bash
pip install -r requirements.txt
```

### Tor Proxy (Zorunlu)

**macOS:**
```bash
brew install tor
brew services start tor
```

**Ubuntu/GitHub Actions:**
```bash
sudo apt-get install tor
sudo service tor start
```

### Çevre Değişkenleri

`.env` dosyası oluştur:

```env
DATABASE_URL=postgresql://user:pass@host:port/db
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx
```

## Kullanım

```bash
python eczane_scraper_requests.py
```

## GitHub Actions

Workflow her gün **UTC 03:00** (Türkiye 06:00) otomatik çalışır.

### Secrets

Repository Settings → Secrets → Actions:

| Secret | Açıklama |
|--------|----------|
| `DATABASE_URL` | PostgreSQL bağlantı URL'i |
| `UPSTASH_REDIS_REST_URL` | Redis URL |
| `UPSTASH_REDIS_REST_TOKEN` | Redis token |

### Manuel Çalıştırma

Actions → Daily Pharmacy Scraper → Run workflow

## Redis Key Format

```
{Şehir}:{Tarih}
İstanbul:2026-01-11
Ankara:2026-01-11
```

## Lisans

MIT
