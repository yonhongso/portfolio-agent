from dotenv import load_dotenv
import os, requests

load_dotenv()
token = os.getenv('TELEGRAM_BOT_TOKEN')
chat_id = os.getenv('TELEGRAM_CHAT_ID')

r = requests.post(
    f'https://api.telegram.org/bot{token}/sendMessage',
    json={'chat_id': chat_id, 'text': '✅ Portfolio Intelligence 텔레그램 연결 성공!'}
)
print(r.json())