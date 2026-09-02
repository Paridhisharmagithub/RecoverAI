import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception: pass
APP_NAME='RecoverAI'
DATABASE_PATH=os.getenv('DATABASE_PATH',str(Path(__file__).resolve().parent.parent/'data'/'recoverai.db'))
RAZORPAY_KEY_ID=os.getenv('RAZORPAY_KEY_ID','')
RAZORPAY_KEY_SECRET=os.getenv('RAZORPAY_KEY_SECRET','')
RAZORPAY_WEBHOOK_SECRET=os.getenv('RAZORPAY_WEBHOOK_SECRET','')
USE_RAZORPAY=os.getenv('USE_RAZORPAY','false').lower()=='true' and bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
