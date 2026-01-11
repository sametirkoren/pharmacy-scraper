#!/bin/bash
# Günlük eczane scraper - cron için

cd /Users/sametirkoren/Desktop/scrapping

# Python environment
source ~/.pyenv/versions/3.10.11/bin/activate 2>/dev/null || true

# Run scraper
python eczane_scraper_requests.py

echo "Scraper tamamlandı: $(date)"
