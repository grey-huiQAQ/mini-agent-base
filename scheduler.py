import schedule
import time
import threading
from datetime import datetime
from agents.daily_agent import generate_daily_plan, generate_daily_review

def run_scheduler():
    schedule.every().day.at("08:00").do(generate_daily_plan)
    schedule.every().day.at("20:00").do(generate_daily_review)
    while True:
        schedule.run_pending()
        time.sleep(60)

def start_scheduler():
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()