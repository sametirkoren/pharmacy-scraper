import os
import uuid
import json
import logging
from datetime import date
from typing import Optional, List, Dict
from contextlib import contextmanager
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Text, Date, and_
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from upstash_redis import Redis
from tenacity import retry, stop_after_attempt, wait_exponential

# Logging ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ENV değişkenlerini yükle
load_dotenv()

# Config
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

# Global flag for database mode
USE_DATABASE = True
FETCH_COORDINATES = False  # Koordinat çekme - yavaşlatıyor
MAX_WORKERS = 2  # GitHub Actions için azaltıldı (4'ten 2'ye)

# Thread-local storage for database sessions
thread_local = threading.local()

# SQLAlchemy setup
Base = declarative_base()
engine = None
SessionLocal = None

def init_database():
    """Initialize database connection and create tables if needed"""
    global engine, SessionLocal
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is required")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    logger.info("Veritabanı tabloları kontrol edildi/oluşturuldu")

# Redis - her thread için ayrı client oluştur
USE_REDIS = bool(REDIS_URL and REDIS_TOKEN)

def get_redis_client() -> Optional[Redis]:
    """Thread-safe Redis client"""
    if not USE_REDIS:
        return None
    try:
        return Redis(url=REDIS_URL, token=REDIS_TOKEN)
    except Exception:
        return None


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


def get_thread_session():
    """Thread-safe database session"""
    if not USE_DATABASE or SessionLocal is None:
        return None
    if not hasattr(thread_local, "session"):
        thread_local.session = SessionLocal()
    return thread_local.session


def close_thread_session():
    """Close thread-local session"""
    if hasattr(thread_local, "session"):
        thread_local.session.close()
        del thread_local.session


def create_driver():
    """Create a new Selenium driver"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--remote-debugging-port=9222")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    options.page_load_strategy = 'normal'  # Tam yükleme bekle
    
    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    driver.set_page_load_timeout(45)  # Daha uzun timeout
    return driver


def check_duplicate(session: Session, city: str, pharmacy_name: str, target_date: date) -> bool:
    """Aynı gün için aynı eczane kaydı var mı kontrol et"""
    if session is None:
        return False
    existing = session.query(Pharmacy).filter(
        and_(
            Pharmacy.city == city,
            Pharmacy.pharmacy == pharmacy_name,
            Pharmacy.date == target_date
        )
    ).first()
    return existing is not None


def get_coordinates(driver, detail_url: str) -> tuple[Optional[str], Optional[str]]:
    """Detay sayfasından koordinatları çek"""
    if not FETCH_COORDINATES:
        return None, None
    
    latitude, longitude = None, None
    try:
        driver.execute_script("window.open('');")
        driver.switch_to.window(driver.window_handles[1])
        driver.get(detail_url)
        
        try:
            latitude = driver.find_element(
                By.CSS_SELECTOR, 'meta[itemprop="latitude"]'
            ).get_attribute("content")
            longitude = driver.find_element(
                By.CSS_SELECTOR, 'meta[itemprop="longitude"]'
            ).get_attribute("content")
        except Exception:
            pass
        
        driver.close()
        driver.switch_to.window(driver.window_handles[0])
    except Exception as e:
        if len(driver.window_handles) > 1:
            driver.close()
            driver.switch_to.window(driver.window_handles[0])
    
    return latitude, longitude


def scrape_city(city_slug: str, max_retries: int = 5) -> List[Dict]:
    """Tek bir şehir için nöbetçi eczaneleri çek - hata durumunda retry"""
    import time
    url = f"https://www.eczaneler.gen.tr/nobetci-{city_slug}"
    city_name = get_city_name(city_slug)
    today = date.today()
    session = get_thread_session()
    
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            logger.info(f"🔄 {city_name} - Deneme {attempt}/{max_retries}")
            time.sleep(2)  # Retry öncesi bekle
        else:
            logger.info(f"🧭 {city_name} için scraping başlıyor")
        
        results = []
        driver = None
        
        try:
            driver = create_driver()
            driver.get(url)
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.ID, "nav-bugun"))
            )
            
            active_tab = driver.find_element(By.ID, "nav-bugun")
            eczaneler = active_tab.find_elements(By.CSS_SELECTOR, "td[colspan='3'] .row")
            logger.info(f"🔍 {city_name}: {len(eczaneler)} eczane bulundu")
            
            # Önce TÜM eczane verilerini topla
            all_pharmacies = []
            for row in eczaneler:
                try:
                    ad = row.find_element(By.CLASS_NAME, "isim").text.strip()
                    adres_div = row.find_element(By.CLASS_NAME, "col-lg-6")
                    adres = adres_div.text.strip().split('\n')[0]
                    ilce = adres_div.find_element(By.CLASS_NAME, "bg-info").text.strip()
                    col3 = row.find_elements(By.CLASS_NAME, "col-lg-3")[-1]
                    telefon = col3.text.strip()
                    
                    all_pharmacies.append({
                        "city": city_name,
                        "district": ilce,
                        "pharmacy": ad,
                        "address": adres,
                        "phone": telefon,
                        "date": today.isoformat()
                    })
                except:
                    continue
            
            # Veritabanına kaydet (duplicate kontrolü ile)
            for pharmacy_data in all_pharmacies:
                if check_duplicate(session, city_name, pharmacy_data["pharmacy"], today):
                    continue
                
                if session is not None:
                    pharmacy_entry = Pharmacy(
                        city=city_name,
                        district=pharmacy_data["district"],
                        pharmacy=pharmacy_data["pharmacy"],
                        address=pharmacy_data["address"],
                        phone=pharmacy_data["phone"],
                        date=today,
                        latitude=None,
                        longitude=None
                    )
                    session.add(pharmacy_entry)
                
                results.append(pharmacy_data)
            
            # Commit
            if session is not None:
                try:
                    session.commit()
                except Exception as e:
                    session.rollback()
                    logger.error(f"DB commit hatası {city_name}: {e}")
            
            # Redis'e TÜM eczaneleri yaz
            if USE_REDIS and all_pharmacies:
                try:
                    redis = get_redis_client()
                    if redis:
                        redis_key = f"{city_name}:{today.isoformat()}"
                        redis.set(redis_key, json.dumps(all_pharmacies, ensure_ascii=False))
                        logger.info(f"📦 Redis: {city_name} - {len(all_pharmacies)} eczane")
                except Exception as e:
                    logger.warning(f"⚠️ Redis hatası {city_name}: {e}")
            
            logger.info(f"✅ {city_name}: {len(results)} eczane kaydedildi")
            return results  # Başarılı - döngüden çık
            
        except TimeoutException:
            logger.warning(f"⏱️ {city_name} timeout (deneme {attempt}/{max_retries})")
        except WebDriverException as e:
            logger.warning(f"🌐 {city_name} WebDriver hatası (deneme {attempt}/{max_retries})")
        except Exception as e:
            logger.warning(f"⚠️ {city_name} hata: {e} (deneme {attempt}/{max_retries})")
        finally:
            if driver:
                driver.quit()
    
    # Tüm denemeler başarısız
    logger.error(f"❌ {city_name} - {max_retries} deneme sonrası başarısız")
    return []


def run_scraper_parallel(cities: List[str], max_workers: int = MAX_WORKERS):
    """Paralel scraper"""
    total_results = []
    failed_cities = []
    
    logger.info(f"🚀 {len(cities)} şehir {max_workers} paralel worker ile scrape edilecek")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_city = {executor.submit(scrape_city, city): city for city in cities}
        
        for future in as_completed(future_to_city):
            city = future_to_city[future]
            try:
                results = future.result()
                total_results.extend(results)
            except Exception as e:
                logger.error(f"❌ {city} başarısız: {e}")
                failed_cities.append(city)
    
    logger.info(f"\n{'='*50}")
    logger.info(f"📊 ÖZET: {len(total_results)} eczane kaydedildi")
    logger.info(f"✅ Başarılı: {len(cities) - len(failed_cities)} şehir")
    if failed_cities:
        logger.warning(f"❌ Başarısız: {failed_cities}")
    
    return total_results, failed_cities


# İl listeleri
ILLER_SLUG = [
    "adana", "adiyaman", "afyonkarahisar", "agri", "amasya", "ankara", "antalya", "artvin", "aydin",
    "balikesir", "bilecik", "bingol", "bitlis", "bolu", "burdur", "bursa", "canakkale", "cankiri", "corum",
    "denizli", "diyarbakir", "edirne", "elazig", "erzincan", "erzurum", "eskisehir", "gaziantep", "giresun",
    "gumushane", "hakkari", "hatay", "isparta", "mersin", "istanbul", "izmir", "kars", "kastamonu", "kayseri",
    "kirklareli", "kirsehir", "kocaeli", "konya", "kutahya", "malatya", "manisa", "kahramanmaras", "mardin",
    "mugla", "mus", "nevsehir", "nigde", "ordu", "osmaniye", "rize", "sakarya", "samsun", "siirt", "sinop",
    "sivas", "sanliurfa", "sirnak", "tekirdag", "tokat", "trabzon", "tunceli", "usak", "van", "yozgat",
    "zonguldak", "aksaray", "bayburt", "karaman", "batman", "bartin", "ardahan", "igdir", "yalova",
    "karabuk", "kilis", "duzce", "kibris", "kirikkale"
]

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

ILLER = ILLER_SLUG

def get_city_name(slug: str) -> str:
    """URL slug'dan Türkçe şehir adını döndür"""
    return ILLER_MAPPING.get(slug, slug.title())


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Nöbetçi Eczane Scraper v3 - Hızlı")
    parser.add_argument("--cities", nargs="+", help="Sadece belirli şehirler için çalıştır")
    parser.add_argument("--workers", type=int, default=4, help="Paralel worker sayısı (default: 4)")
    parser.add_argument("--coords", action="store_true", help="Koordinatları da çek (yavaşlatır)")
    parser.add_argument("--no-db", action="store_true", help="Veritabanı olmadan çalıştır")
    
    args = parser.parse_args()
    
    # Config
    if args.coords:
        FETCH_COORDINATES = True
        logger.info("📍 Koordinat çekme aktif")
    
    if args.no_db:
        USE_DATABASE = False
        logger.info("🔕 Veritabanı devre dışı")
    else:
        try:
            init_database()
            logger.info("✅ Veritabanı bağlantısı kuruldu")
        except Exception as e:
            logger.error(f"❌ Veritabanı bağlantısı kurulamadı: {e}")
            exit(1)
    
    # Redis durumu
    if USE_REDIS:
        try:
            test_redis = get_redis_client()
            if test_redis:
                test_redis.ping()
                logger.info("✅ Redis bağlantısı kuruldu")
        except Exception as e:
            logger.warning(f"⚠️ Redis bağlantısı test edilemedi: {e}")
    
    cities = args.cities if args.cities else ILLER_SLUG
    results, failed = run_scraper_parallel(cities, max_workers=args.workers)
    
    # JSON'a kaydet
    if results:
        output_file = f"eczaneler_{date.today().isoformat()}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"📁 Sonuçlar kaydedildi: {output_file}")
