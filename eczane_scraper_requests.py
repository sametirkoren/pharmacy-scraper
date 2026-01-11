import os
import uuid
import json
import logging
import requests
import re
from datetime import date
from typing import Optional, List, Dict
from bs4 import BeautifulSoup
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Tor proxy ayarları (GitHub Actions'ta çalışır)
TOR_PROXY = {
    'http': 'socks5h://127.0.0.1:9050',
    'https': 'socks5h://127.0.0.1:9050'
}

# Session oluştur
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'tr-TR,tr;q=0.9',
})

# Tor proxy'yi test et, yoksa direkt bağlan
def get_proxies():
    try:
        test = requests.get('http://127.0.0.1:9050', timeout=2)
    except:
        pass
    # Tor çalışıyorsa proxy kullan
    try:
        requests.get('https://check.torproject.org', proxies=TOR_PROXY, timeout=10)
        return TOR_PROXY
    except:
        return None

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
        response = session.get(detail_url, proxies=proxies, timeout=10)
        if response.status_code != 200:
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
    except:
        pass
    return None, None

def scrape_city(city_slug: str, db_session: Session, proxies=None, max_retries: int = 3) -> List[Dict]:
    url = f"https://www.eczaneler.gen.tr/nobetci-{city_slug}"
    city_name = get_city_name(city_slug)
    today = date.today()
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                logger.info(f"🔄 {city_name} - Deneme {attempt}/{max_retries}")
                time.sleep(2)
            
            response = session.get(url, proxies=proxies, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Bugünkü tab'ı bul
            nav_bugun = soup.find('div', id='nav-bugun')
            if not nav_bugun:
                logger.warning(f"⚠️ {city_name}: nav-bugun bulunamadı")
                continue
            
            # Eczaneleri bul
            rows = nav_bugun.select("td[colspan='3'] .row")
            logger.info(f"🔍 {city_name}: {len(rows)} eczane bulundu")
            
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
                        ilce_elem = col_lg_6.select_one('.bg-info')
                        ilce = ilce_elem.get_text(strip=True) if ilce_elem else ""
                    else:
                        adres_text = ""
                        ilce = ""
                    
                    col_lg_3_list = row.select('.col-lg-3')
                    telefon = col_lg_3_list[-1].get_text(strip=True) if col_lg_3_list else ""
                    
                    # Detay sayfası linkini bul ve koordinat çek
                    lat, lon = None, None
                    detail_link = None
                    
                    # 1. isim elementinin parent'ında link ara
                    detail_link = isim_elem.find_parent('a')
                    
                    # 2. Row içinde /eczane/ linki ara
                    if not detail_link:
                        detail_link = row.select_one('a[href*="/eczane/"]')
                    
                    # 3. Row'un parent td'sinde ara
                    if not detail_link:
                        parent_td = row.find_parent('td')
                        if parent_td:
                            detail_link = parent_td.select_one('a[href*="/eczane/"]')
                    
                    # 4. isim elementinin içinde link ara
                    if not detail_link:
                        detail_link = isim_elem.select_one('a[href*="/eczane/"]')
                    
                    # 5. isim elementinin kardeşlerinde ara
                    if not detail_link and isim_elem.parent:
                        detail_link = isim_elem.parent.select_one('a[href*="/eczane/"]')
                    
                    if detail_link and detail_link.get('href'):
                        href = detail_link.get('href')
                        detail_url = href if href.startswith('http') else "https://www.eczaneler.gen.tr" + href
                        lat, lon = get_coords_from_detail_page(detail_url, proxies)
                        time.sleep(0.2)
                    
                    pharmacy_data = {
                        "city": city_name,
                        "district": ilce,
                        "pharmacy": ad,
                        "address": adres_text,
                        "phone": telefon,
                        "date": today.isoformat(),
                        "latitude": lat,
                        "longitude": lon
                    }
                    all_pharmacies.append(pharmacy_data)
                    
                    # DB'ye kaydet
                    if db_session and not check_duplicate(db_session, city_name, ad, today):
                        pharmacy_entry = Pharmacy(
                            city=city_name,
                            district=ilce,
                            pharmacy=ad,
                            address=adres_text,
                            phone=telefon,
                            date=today,
                            latitude=lat,
                            longitude=lon
                        )
                        db_session.add(pharmacy_entry)
                        results.append(pharmacy_data)
                        
                except Exception as e:
                    continue
            
            # Commit
            if db_session:
                try:
                    db_session.commit()
                except Exception as e:
                    db_session.rollback()
                    logger.error(f"DB commit hatası {city_name}: {e}")
            
            # Redis'e yaz
            redis = get_redis_client()
            if redis and all_pharmacies:
                try:
                    redis_key = f"{city_name}:{today.isoformat()}"
                    redis.set(redis_key, json.dumps(all_pharmacies, ensure_ascii=False))
                    logger.info(f"📦 Redis: {city_name} - {len(all_pharmacies)} eczane")
                except:
                    pass
            
            logger.info(f"✅ {city_name}: {len(results)} yeni eczane kaydedildi")
            return results
            
        except Exception as e:
            logger.warning(f"⚠️ {city_name} hata (deneme {attempt}/{max_retries}): {e}")
    
    logger.error(f"❌ {city_name} - {max_retries} deneme sonrası başarısız")
    return []

def main():
    init_database()
    
    redis = get_redis_client()
    if redis:
        try:
            redis.ping()
            logger.info("✅ Redis bağlantısı kuruldu")
        except:
            logger.warning("⚠️ Redis bağlantısı kurulamadı")
    
    # Tor proxy kontrolü - ZORUNLU
    proxies = get_proxies()
    if proxies:
        logger.info("🧅 Tor proxy aktif")
    else:
        logger.error("❌ Tor proxy bulunamadı! Tor kurulu ve çalışıyor olmalı.")
        logger.error("   Local: brew install tor && brew services start tor")
        logger.error("   GitHub Actions: Otomatik kurulur")
        exit(1)
    
    total_results = []
    failed_cities = []
    
    MAX_WORKERS = 5
    logger.info(f"🚀 {len(ILLER_SLUG)} şehir {MAX_WORKERS} paralel worker ile scrape edilecek")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scrape_city, city, SessionLocal(), proxies): city for city in ILLER_SLUG}
        
        for future in as_completed(futures):
            city = futures[future]
            try:
                results = future.result()
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
    
    # JSON'a kaydet
    if total_results:
        output_file = f"eczaneler_{date.today().isoformat()}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(total_results, f, ensure_ascii=False, indent=2)
        logger.info(f"📁 Sonuçlar kaydedildi: {output_file}")

if __name__ == "__main__":
    main()
