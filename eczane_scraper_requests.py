import os
import uuid
import json
import logging
import re
import random
from datetime import date
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
import time

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Text, Date, and_
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from upstash_redis import Redis

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

Base = declarative_base()
engine = None
SessionLocal = None

def init_database():
    global engine, SessionLocal
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL required")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    logger.info("✅ Veritabanı bağlantısı kuruldu")

class Pharmacy(Base):
    __tablename__ = "pharmacies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    city = Column(Text, nullable=False)
    district = Column(Text, nullable=False)
    pharmacy = Column(Text, nullable=False)
    address = Column(Text, nullable=False)
    phone = Column(Text)
    date = Column(Date, nullable=False)
    latitude = Column(Text)
    longitude = Column(Text)

# Playwright browser yönetimi
_playwright = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None


class FetchResponse:
    """requests-uyumlu response wrapper"""
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def init_browser():
    """Playwright browser başlat"""
    global _playwright, _browser, _context
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)
    _context = _browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
    )
    # Webdriver detection'ı gizle
    _context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['tr-TR', 'tr', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)
    logger.info("Playwright browser baslatildi")


def close_browser():
    """Playwright browser kapat"""
    global _playwright, _browser, _context
    if _context:
        _context.close()
    if _browser:
        _browser.close()
    if _playwright:
        _playwright.stop()
    _context = None
    _browser = None
    _playwright = None


def fetch_url(url: str, timeout: int = 30) -> FetchResponse:
    """Playwright ile sayfa cek (Cloudflare bypass)"""
    page = _context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        status_code = response.status if response else 0
        # Cloudflare challenge varsa bekle
        if status_code == 403 or "challenge" in page.title().lower():
            page.wait_for_timeout(5000)
            content = page.content()
            # Challenge gecildiyse 200 kabul et
            if "eczane" in content.lower() or "nobetci" in content.lower():
                status_code = 200
        else:
            content = page.content()
        return FetchResponse(status_code=status_code, text=content)
    except Exception as e:
        logger.debug(f"Playwright fetch error: {url} - {e}")
        return FetchResponse(status_code=0, text="")
    finally:
        page.close()

def get_redis_client():
    if REDIS_URL and REDIS_TOKEN:
        try:
            return Redis(url=REDIS_URL, token=REDIS_TOKEN)
        except:
            return None
    return None

def check_duplicate(db_session: Session, city: str, pharmacy_name: str, target_date: date) -> bool:
    if db_session is None:
        return False
    existing = db_session.query(Pharmacy).filter(
        and_(
            Pharmacy.city == city,
            Pharmacy.pharmacy == pharmacy_name,
            Pharmacy.date == target_date
        )
    ).first()
    return existing is not None

ILLER_MAPPING = {
    "adana": "Adana", "adiyaman": "Adıyaman", "afyonkarahisar": "Afyonkarahisar", "agri": "Ağrı",
    "amasya": "Amasya", "ankara": "Ankara", "antalya": "Antalya", "artvin": "Artvin", "aydin": "Aydın",
    "balikesir": "Balıkesir", "bilecik": "Bilecik", "bingol": "Bingöl", "bitlis": "Bitlis", "bolu": "Bolu",
    "burdur": "Burdur", "bursa": "Bursa", "canakkale": "Çanakkale", "cankiri": "Çankırı", "corum": "Çorum",
    "denizli": "Denizli", "diyarbakir": "Diyarbakır", "edirne": "Edirne", "elazig": "Elazığ",
    "erzincan": "Erzincan", "erzurum": "Erzurum", "eskisehir": "Eskişehir", "gaziantep": "Gaziantep",
    "giresun": "Giresun", "gumushane": "Gümüşhane", "hakkari": "Hakkari", "hatay": "Hatay",
    "isparta": "Isparta", "mersin": "Mersin", "istanbul": "İstanbul", "izmir": "İzmir", "kars": "Kars",
    "kastamonu": "Kastamonu", "kayseri": "Kayseri", "kirklareli": "Kırklareli", "kirsehir": "Kırşehir",
    "kocaeli": "Kocaeli", "konya": "Konya", "kutahya": "Kütahya", "malatya": "Malatya", "manisa": "Manisa",
    "kahramanmaras": "Kahramanmaraş", "mardin": "Mardin", "mugla": "Muğla", "mus": "Muş",
    "nevsehir": "Nevşehir", "nigde": "Niğde", "ordu": "Ordu", "osmaniye": "Osmaniye", "rize": "Rize",
    "sakarya": "Sakarya", "samsun": "Samsun", "siirt": "Siirt", "sinop": "Sinop", "sivas": "Sivas",
    "sanliurfa": "Şanlıurfa", "sirnak": "Şırnak", "tekirdag": "Tekirdağ", "tokat": "Tokat",
    "trabzon": "Trabzon", "tunceli": "Tunceli", "usak": "Uşak", "van": "Van", "yozgat": "Yozgat",
    "zonguldak": "Zonguldak", "aksaray": "Aksaray", "bayburt": "Bayburt", "karaman": "Karaman",
    "batman": "Batman", "bartin": "Bartın", "ardahan": "Ardahan", "igdir": "Iğdır", "yalova": "Yalova",
    "karabuk": "Karabük", "kilis": "Kilis", "duzce": "Düzce", "kibris": "Kıbrıs", "kirikkale": "Kırıkkale"
}

ILLER_SLUG = list(ILLER_MAPPING.keys())

def get_city_name(slug: str) -> str:
    return ILLER_MAPPING.get(slug, slug.title())

def extract_coords_from_maps_link(maps_url: str) -> tuple:
    """Google Maps linkinden lat/long çıkar"""
    try:
        # daddr=37.551095,35.392546 formatı
        match = re.search(r'daddr=([-\d.]+),([-\d.]+)', maps_url)
        if match:
            return match.group(1), match.group(2)
        # q=37.551095,35.392546 formatı (iframe'de)
        match = re.search(r'[?&]q=([-\d.]+),([-\d.]+)', maps_url)
        if match:
            return match.group(1), match.group(2)
    except:
        pass
    return None, None

def get_coords_from_detail_page(detail_url: str, proxies=None) -> tuple:
    """Eczane detay sayfasından koordinat çek"""
    try:
        response = fetch_url(detail_url, timeout=10)
        if response.status_code != 200:
            logger.debug(f"Detail page error {response.status_code}: {detail_url}")
            return None, None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Meta tag'lardan çek (en güvenilir)
        lat_meta = soup.select_one('meta[itemprop="latitude"]')
        lon_meta = soup.select_one('meta[itemprop="longitude"]')
        if lat_meta and lon_meta:
            lat = lat_meta.get('content')
            lon = lon_meta.get('content')
            if lat and lon:
                return lat, lon
        
        # 2. Google Maps iframe'inden çek
        iframe = soup.select_one('iframe[src*="google.com/maps"]')
        if iframe:
            src = iframe.get('src', '')
            lat, lon = extract_coords_from_maps_link(src)
            if lat and lon:
                return lat, lon
        
        # 3. Google Maps linkinden çek
        maps_link = soup.select_one('a[href*="google.com/maps?daddr="]')
        if maps_link:
            return extract_coords_from_maps_link(maps_link.get('href', ''))
    except Exception as e:
        logger.debug(f"Detail page exception: {detail_url} - {e}")
    return None, None

def get_district_urls(city_slug: str, proxies=None) -> List[str]:
    """Şehrin ilçe sayfası URL'lerini çek"""
    url = f"https://www.eczaneler.gen.tr/nobetci-{city_slug}"
    try:
        response = fetch_url(url, timeout=30)
        if response.status_code != 200:
            return []
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # İlçe dropdown veya sidebar'dan linkleri çek
        district_links = soup.select(f'a[href*="nobetci-{city_slug}-"]')
        urls = list(set([link.get('href') for link in district_links if link.get('href')]))
        
        # Tam URL'ye çevir
        full_urls = []
        for u in urls:
            if u.startswith('/'):
                full_urls.append(f"https://www.eczaneler.gen.tr{u}")
            elif u.startswith('http'):
                full_urls.append(u)
        
        return full_urls
    except:
        return []

def extract_district_from_url(url: str, city_slug: str) -> str:
    """URL'den ilçe adını çıkar: nobetci-istanbul-bahcelievler -> Bahçelievler"""
    # URL'den path kısmını al
    path = url.split('/')[-1]  # nobetci-istanbul-bahcelievler
    prefix = f"nobetci-{city_slug}-"
    if prefix in path:
        district_slug = path.replace(prefix, '')
        # Slug'ı düzgün isme çevir
        district_map = {
            'bahcelievler': 'Bahçelievler', 'bakirkoy': 'Bakırköy', 'besiktas': 'Beşiktaş',
            'beyoglu': 'Beyoğlu', 'fatih': 'Fatih', 'kadikoy': 'Kadıköy', 'sisli': 'Şişli',
            'uskudar': 'Üsküdar', 'umraniye': 'Ümraniye', 'maltepe': 'Maltepe', 'kartal': 'Kartal',
            'pendik': 'Pendik', 'tuzla': 'Tuzla', 'atasehir': 'Ataşehir', 'sancaktepe': 'Sancaktepe',
            'sultanbeyli': 'Sultanbeyli', 'cekmekoy': 'Çekmeköy', 'beykoz': 'Beykoz',
            'sariyer': 'Sarıyer', 'eyup': 'Eyüp', 'kagithane': 'Kağıthane', 'bayrampasa': 'Bayrampaşa',
            'gaziosmanpasa': 'Gaziosmanpaşa', 'sultangazi': 'Sultangazi', 'esenler': 'Esenler',
            'bagcilar': 'Bağcılar', 'gungoren': 'Güngören', 'zeytinburnu': 'Zeytinburnu',
            'avcilar': 'Avcılar', 'kucukcekmece': 'Küçükçekmece', 'basaksehir': 'Başakşehir',
            'esenyurt': 'Esenyurt', 'beylikduzu': 'Beylikdüzü', 'buyukcekmece': 'Büyükçekmece',
            'arnavutkoy': 'Arnavutköy', 'catalca': 'Çatalca', 'silivri': 'Silivri', 'sile': 'Şile',
            'adalar': 'Adalar'
        }
        return district_map.get(district_slug, district_slug.replace('-', ' ').title())
    return ""

def parse_date_from_alert(alert_text: str) -> date:
    """Alert mesajından tarihi parse et: '5 Şubat Perşembe akşamından...' -> date(2026, 2, 5)"""
    months = {
        'ocak': 1, 'şubat': 2, 'mart': 3, 'nisan': 4, 'mayıs': 5, 'haziran': 6,
        'temmuz': 7, 'ağustos': 8, 'eylül': 9, 'ekim': 10, 'kasım': 11, 'aralık': 12
    }
    
    # "5 Şubat" formatını bul
    match = re.search(r'(\d{1,2})\s+(ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık)', 
                      alert_text.lower())
    if match:
        day = int(match.group(1))
        month = months.get(match.group(2))
        if month:
            year = date.today().year
            # Aralık'ta Ocak tarihini görürsek sonraki yıl
            if date.today().month == 12 and month == 1:
                year += 1
            return date(year, month, day)
    
    return date.today()

def scrape_page(url: str, city_name: str, city_slug: str, db_session: Session, proxies=None, retry_count: int = 0) -> List[Dict]:
    """Tek bir sayfayı scrape et (şehir veya ilçe)"""
    
    # URL'den ilçe adını çıkar (fallback için)
    district_from_url = extract_district_from_url(url, city_slug)
    
    # Rate limit'ten kaçınmak için delay
    time.sleep(random.uniform(1.5, 3.0))
    
    try:
        response = fetch_url(url, timeout=30)
        
        if response.status_code == 403:
            if retry_count < 3:
                wait = (retry_count + 1) * 5
                logger.warning(f"⚠️ 403 hatası, {wait}s bekleyip tekrar denenecek ({retry_count+1}/3): {url}")
                time.sleep(wait)
                return scrape_page(url, city_name, city_slug, db_session, proxies, retry_count + 1)
            logger.error(f"Page error {url}: 403 Forbidden (3 deneme sonra)")
            return []
        
        if response.status_code != 200:
            logger.error(f"Page error {url}: HTTP {response.status_code}")
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Bugünkü tab'ı bul
        nav_bugun = soup.find('div', id='nav-bugun')
        if not nav_bugun:
            return []
        
        # Alert mesajından gerçek tarihi al
        alert_elem = nav_bugun.select_one('.alert-warning')
        if alert_elem:
            pharmacy_date = parse_date_from_alert(alert_elem.get_text())
            logger.info(f"📅 Tarih parse edildi: {pharmacy_date.isoformat()} - {alert_elem.get_text()[:50]}...")
        else:
            pharmacy_date = date.today()
            logger.warning(f"⚠️ Alert bulunamadı, date.today() kullanılıyor: {pharmacy_date}")
        
        # Eczaneleri bul
        rows = nav_bugun.select("td[colspan='3'] .row")
        if not rows:
            return []
        
        all_pharmacies = []
        results = []
        
        for row in rows:
            try:
                isim_elem = row.select_one('.isim')
                if not isim_elem:
                    continue
                ad = isim_elem.get_text(strip=True)
                
                col_lg_6 = row.select_one('.col-lg-6')
                if col_lg_6:
                    adres_text = col_lg_6.get_text(separator='\n', strip=True).split('\n')[0]
                else:
                    adres_text = ""
                
                # İlçe her zaman URL'den gelsin (sayfa badge'i semt/mahalle bilgisi)
                ilce = district_from_url
                
                col_lg_3_list = row.select('.col-lg-3')
                telefon = col_lg_3_list[-1].get_text(strip=True) if col_lg_3_list else ""
                
                # Detay sayfası linkini bul ve koordinat çek
                lat, lon = None, None
                detail_link = None
                
                # 1. isim elementinin parent'ında link ara
                detail_link = isim_elem.find_parent('a')
                
                # 2. col-lg-3 içinde link ara
                if not detail_link:
                    col_parent = isim_elem.find_parent('div', class_='col-lg-3')
                    if col_parent:
                        detail_link = col_parent.select_one('a[href*="/eczane/"]')
                
                # 3. Row içinde herhangi bir /eczane/ linki ara
                if not detail_link:
                    detail_link = row.select_one('a[href*="/eczane/"]')
                
                detail_url = None
                if detail_link and detail_link.get('href'):
                    href = detail_link.get('href')
                    detail_url = href if href.startswith('http') else "https://www.eczaneler.gen.tr" + href
                
                if detail_url:
                    lat, lon = get_coords_from_detail_page(detail_url, proxies)
                    time.sleep(0.2)
                
                pharmacy_data = {
                    "city": city_name,
                    "district": ilce,
                    "pharmacy": ad,
                    "address": adres_text,
                    "phone": telefon,
                    "date": pharmacy_date.isoformat(),
                    "latitude": lat,
                    "longitude": lon
                }
                
                all_pharmacies.append(pharmacy_data)
                
                # Veritabanına kaydet
                try:
                    existing = db_session.query(Pharmacy).filter_by(
                        city=city_name,
                        pharmacy=ad,
                        date=pharmacy_date
                    ).first()
                    
                    if existing:
                        existing.district = ilce
                        existing.address = adres_text
                        existing.phone = telefon
                        existing.latitude = lat
                        existing.longitude = lon
                    else:
                        new_pharmacy = Pharmacy(
                            city=city_name,
                            district=ilce,
                            pharmacy=ad,
                            address=adres_text,
                            phone=telefon,
                            date=pharmacy_date,
                            latitude=lat,
                            longitude=lon
                        )
                        db_session.add(new_pharmacy)
                    
                    db_session.commit()
                    results.append(pharmacy_data)
                except Exception as e:
                    db_session.rollback()
                    logger.error(f"DB error: {e}")
            except Exception as e:
                logger.error(f"Row error: {e}")
                continue
        
        return results
    except Exception as e:
        logger.error(f"Page error {url}: {e}")
        return []

def scrape_city(city_slug: str, db_session: Session, proxies=None, max_retries: int = 3) -> List[Dict]:
    url = f"https://www.eczaneler.gen.tr/nobetci-{city_slug}"
    city_name = get_city_name(city_slug)
    
    # Önce ilçe sayfalarını dene (linkler orada var)
    district_urls = get_district_urls(city_slug, proxies)
    
    if district_urls:
        logger.info(f"🏘️ {city_name}: {len(district_urls)} ilçe bulundu, ilçe sayfaları scrape edilecek")
        all_results = []
        for district_url in district_urls:
            results = scrape_page(district_url, city_name, city_slug, db_session, proxies)
            if results:
                all_results.extend(results)
            time.sleep(0.3)
        
        if all_results:
            logger.info(f"✅ {city_name}: {len(all_results)} eczane (ilçe sayfalarından)")
            return all_results
    
    # İlçe yoksa ana sayfayı scrape et (küçük şehirler için)
    logger.info(f"� {city_name}: Ana sayfa scrape edilecek")
    results = scrape_page(url, city_name, city_slug, db_session, proxies)
    if results:
        logger.info(f"✅ {city_name}: {len(results)} eczane (ana sayfadan)")
    return results

def main():
    init_database()
    init_browser()

    redis = get_redis_client()
    if redis:
        try:
            redis.ping()
            logger.info("Redis baglantisi kuruldu")
        except:
            logger.warning("Redis baglantisi kurulamadi")

    logger.info("Playwright ile Cloudflare bypass aktif")
    
    total_results = []
    failed_cities = []
    
    # Test için sadece belirli şehirler (None = hepsi)
    TEST_CITIES = None  # Production: tüm şehirler
    cities_to_scrape = TEST_CITIES if TEST_CITIES else ILLER_SLUG
    
    logger.info(f"🚀 {len(cities_to_scrape)} şehir sırayla scrape edilecek")
    
    for city in cities_to_scrape:
        try:
            results = scrape_city(city, SessionLocal(), None)
            if results:
                total_results.extend(results)
            else:
                failed_cities.append(city)
        except Exception as e:
            logger.error(f"❌ {city} hata: {e}")
            failed_cities.append(city)
    
    logger.info(f"\n{'='*50}")
    logger.info(f"📊 ÖZET: {len(total_results)} eczane kaydedildi")
    logger.info(f"✅ Başarılı: {len(ILLER_SLUG) - len(failed_cities)} şehir")
    if failed_cities:
        logger.warning(f"❌ Başarısız: {failed_cities}")
    
    # Redis'e şehir bazlı kaydet
    if redis and total_results:
        try:
            # Şehir bazlı grupla
            city_data = {}
            for pharmacy in total_results:
                city = pharmacy.get('city', 'Unknown')
                if city not in city_data:
                    city_data[city] = []
                city_data[city].append(pharmacy)
            
            # Her şehri Redis'e kaydet
            today_str = date.today().isoformat()
            for city, pharmacies in city_data.items():
                redis_key = f"{city}:{today_str}"
                redis.set(redis_key, json.dumps(pharmacies, ensure_ascii=False))
            
            logger.info(f"📦 Redis: {len(city_data)} şehir kaydedildi")
        except Exception as e:
            logger.warning(f"⚠️ Redis kayıt hatası: {e}")
    
    # JSON'a kaydet
    if total_results:
        output_file = f"eczaneler_{date.today().isoformat()}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(total_results, f, ensure_ascii=False, indent=2)
        logger.info(f"Sonuclar kaydedildi: {output_file}")

    close_browser()

if __name__ == "__main__":
    main()
