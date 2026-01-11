# Nöbetçi Eczane Scraper 🏥

Türkiye genelinde nöbetçi eczaneleri çeken ve Supabase veritabanına kaydeden scraper.

## İyileştirmeler (v2)

- **Error Handling**: Retry mekanizması (3 deneme, exponential backoff)
- **Logging**: Dosya + konsol loglama (`scraper.log`)
- **Duplicate Check**: Aynı gün için tekrar kayıt engelleme
- **Context Managers**: Driver ve DB session düzgün yönetimi
- **Eski Veri Temizleme**: 7 günden eski kayıtları otomatik silme
- **CLI Argümanları**: Belirli şehirler için çalıştırma

## Kurulum

```bash
pip install -r requirements.txt
```

## Kullanım

```bash
# Tüm şehirler
python eczane_scraper_v2.py

# Belirli şehirler
python eczane_scraper_v2.py --cities istanbul ankara izmir

# Eski verileri silmeden
python eczane_scraper_v2.py --no-clear
```

## GitHub Actions ile Günlük Çalıştırma

### 1. Repository'yi GitHub'a Push Et

```bash
cd /Users/sametirkoren/Desktop/scrapping
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/KULLANICI_ADIN/eczane-scraper.git
git push -u origin main
```

### 2. GitHub Secrets Ekle

Repository Settings → Secrets and variables → Actions → New repository secret:

| Secret Name | Değer |
|-------------|-------|
| `DATABASE_URL` | Supabase PostgreSQL bağlantı URL'i |
| `UPSTASH_REDIS_REST_URL` | Redis URL (opsiyonel) |
| `UPSTASH_REDIS_REST_TOKEN` | Redis token (opsiyonel) |

**Supabase URL formatı:**
```
postgresql://postgres.[PROJECT_REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres
```

### 3. Workflow Aktif

Workflow her gün **Türkiye saati 06:00**'da otomatik çalışır.

Manuel çalıştırmak için: Actions → Daily Pharmacy Scraper → Run workflow

## Alternatif: Supabase Edge Function + pg_cron

Supabase'de doğrudan cron job çalıştırmak istersen:

```sql
-- pg_cron extension aktif et (Supabase Dashboard → Database → Extensions)
select cron.schedule(
  'daily-pharmacy-scraper',
  '0 3 * * *', -- Her gün UTC 03:00
  $$SELECT net.http_post(
    url:='https://your-edge-function-url.supabase.co/functions/v1/scrape-pharmacies',
    headers:='{"Authorization": "Bearer YOUR_ANON_KEY"}'::jsonb
  )$$
);
```

## Çevre Değişkenleri

`.env` dosyası:

```env
DATABASE_URL=postgresql://...
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
```

## Lisans

MIT
