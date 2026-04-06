import logging
import requests
import time
import json
import asyncio
import threading
import smtplib
import ssl
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

notifications_list = []     # Записи (рядки) для Excel
price_state = {}            # Стан по кожному токену (ключ = asset_id)
subscribed_tokens = set()   # Поточний перелік токенів, на які ми підписані

metadata_map = {}

SEND_EMAIL_EVERY_DAY = True

def write_excel():
    """
    Записуємо поточний notifications_list у Excel (alertsa.xlsx).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Alerts"
    headers = ["ASSET_ID","timestamp_buy","timestamp_sell","buy","sell","outcome","question","groupItemTitle","label","url"]
    ws.append(headers)

    for notif in notifications_list:
        row = [
            notif.get("ASSET_ID",""),
            notif.get("timestamp_buy",""),
            notif.get("timestamp_sell",""),
            notif.get("buy",""),
            notif.get("sell",""),
            notif.get("outcome",""),
            notif.get("question",""),
            notif.get("groupItemTitle",""),
            notif.get("label",""),
            notif.get("url",""),
        ]
        ws.append(row)

    wb.save("alertsb.xlsx")
    logging.info("Excel alertsb.xlsx updated.")

def tokens_we_still_need():
    """
    Які токени не можна відписувати, якщо для них уже є buy, але немає sell.
    """
    needed = set()
    for notif in notifications_list:
        asset_id = notif.get("ASSET_ID")
        if notif.get("buy") and not notif.get("sell"):
            needed.add(asset_id)
    return needed

def fetch_current_tokens():
    """
    Тягнемо із gamma-api список подій та наповнюємо metadata_map[outcome/question/slug/...].
    Фільтруємо:
      - у slug присутнє "up-or-down";
      - у кінці slug є число дня місяця (щоденні slug);
      - лейбли містять одночасно "Crypto Prices" і "Recurring";
      - ринки, де m.get("active") та m.get("acceptingOrders") == True.
    Додатково: у консоль виводимо slug та asset_id-и.
    """
    offset = 0
    found_tokens = set()

    while True:
        try:
            resp = requests.get(f"https://gamma-api.polymarket.com/events?closed=false&offset={offset}")
            if resp.status_code == 429:
                logging.warning("fetch_current_tokens: Rate limited (429). Чекаємо 5s...")
                time.sleep(5)
                continue
            if resp.status_code != 200:
                logging.error(f"fetch_current_tokens: HTTP {resp.status_code}, припиняємо.")
                break

            events = resp.json()
            if not events:
                break

            for event in events:
                event_slug = event.get("slug", "")
                # Має містити "up-or-down"
                if "up-or-down" not in event_slug:
                    continue
                # Має бути щоденний slug: закінчуватися на "-<число>"
                if not re.search(r'-\d{1,2}$', event_slug):
                    continue

                # Збираємо лейбли
                tags = event.get("tags", [])
                labels_list = []
                for tg in tags:
                    lb = tg.get("label") if isinstance(tg, dict) else tg
                    if lb:
                        labels_list.append(lb)
                label_str = ", ".join(labels_list)
                if not ("Crypto Prices" in label_str and "Recurring" in label_str):
                    continue

                markets = event.get("markets", [])
                for m in markets:
                    if not (m.get("active") and m.get("acceptingOrders")):
                        continue

                    # Розбираємо outcomes та token IDs
                    try:
                        outcomes_list = json.loads(m.get("outcomes", "[]"))
                    except:
                        outcomes_list = []
                    try:
                        clob_list = json.loads(m.get("clobTokenIds", "[]"))
                    except:
                        clob_list = []

                    question_text = m.get("question", "")

                    for i, raw_token_id in enumerate(clob_list):
                        asset_id = str(raw_token_id)
                        if asset_id not in found_tokens:
                            print(f"[fetch_current_tokens] slug={event_slug}, asset_id={asset_id}")
                        found_tokens.add(asset_id)

                        outcome_val = outcomes_list[i] if i < len(outcomes_list) else ""
                        metadata_map[asset_id] = {
                            "outcome": outcome_val,
                            "question": question_text,
                            "slug": event_slug,
                            "groupItemTitle": event.get("title", ""),
                            "label": label_str
                        }

            offset += len(events)
            time.sleep(0.2)

        except Exception as e:
            logging.error(f"fetch_current_tokens exception: {e}")
            time.sleep(5)
            break

    return found_tokens

# ====================== ЛОГІКА ОРДЕРБУКА ======================
orderbooks = {}

def init_orderbook_for_asset(asset_id):
    if asset_id not in orderbooks:
        orderbooks[asset_id] = {"bids": [], "asks": []}

def sort_orderbook(asset_id):
    ob = orderbooks[asset_id]
    ob["bids"].sort(key=lambda x: x["price"], reverse=True)
    ob["asks"].sort(key=lambda x: x["price"])

def get_best_bid_ask(asset_id):
    ob = orderbooks[asset_id]
    best_bid = ob["bids"][0]["price"] if ob["bids"] else None
    best_ask = ob["asks"][0]["price"] if ob["asks"] else None
    return best_bid, best_ask

def apply_book_snapshot(asset_id, bids, asks):
    init_orderbook_for_asset(asset_id)
    ob = orderbooks[asset_id]
    ob["bids"] = [{"price": float(b["price"]), "size": float(b["size"])} for b in bids]
    ob["asks"] = [{"price": float(a["price"]), "size": float(a["size"])} for a in asks]
    sort_orderbook(asset_id)

def apply_price_changes(asset_id, changes):
    init_orderbook_for_asset(asset_id)
    ob = orderbooks[asset_id]
    for ch in changes:
        price = float(ch["price"])
        size = float(ch["size"])
        side = ch["side"]
        if side == "BUY":
            found = False
            for lvl in ob["bids"]:
                if abs(lvl["price"] - price) < 1e-8:
                    found = True
                    if size == 0:
                        ob["bids"].remove(lvl)
                    else:
                        lvl["size"] = size
                    break
            if not found and size > 0:
                ob["bids"].append({"price": price, "size": size})
        else:  # side == "SELL"
            found = False
            for lvl in ob["asks"]:
                if abs(lvl["price"] - price) < 1e-8:
                    found = True
                    if size == 0:
                        ob["asks"].remove(lvl)
                    else:
                        lvl["size"] = size
                    break
            if not found and size > 0:
                ob["asks"].append({"price": price, "size": size})
    sort_orderbook(asset_id)

# ====================== Основний WS-цикл з авто-реконектом ======================
async def listen_tokens_once(token_list):
    """
    Підключаємось до WS з усіма token_list, і при розривах з'єднання автоматично перепідключаємось.
    Слухаємо "book", "price_change" тощо, визначаємо best_bid/best_ask, застосовуємо BUY/SELL-правила,
    записуємо в notifications_list.
    """
    import aiohttp

    ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    subscription_data = {
        "assets_ids": list(token_list),
        "type": "market"
    }

    while True:
        try:
            logging.info(f"Connecting WS for {len(token_list)} tokens...")
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    await ws.send_json(subscription_data)
                    logging.info("WebSocket subscription sent.")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.CLOSED:
                            logging.warning("WS CLOSED by remote.")
                            break
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            logging.error(f"WS error: {msg.data}")
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue

                        try:
                            data = json.loads(msg.data)
                        except:
                            continue

                        events = data if isinstance(data, list) else [data]
                        for event in events:
                            asset_id = event.get("asset_id")
                            if not asset_id:
                                continue

                            etype = event.get("event_type")
                            if etype == "book":
                                apply_book_snapshot(
                                    asset_id,
                                    event.get("bids", []),
                                    event.get("asks", [])
                                )
                            elif etype == "price_change":
                                apply_price_changes(asset_id, event.get("changes", []))
                            else:
                                pass

                            best_bid, best_ask = get_best_bid_ask(asset_id)
                            if best_bid is None or best_ask is None:
                                continue

                            current_time = datetime.now(ZoneInfo("Europe/Kiev"))
                            current_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')

                            if asset_id not in price_state:
                                price_state[asset_id] = {
                                    "first_record_created": False,
                                    "dropped_below_08": False, # замість 0.94
                                    "last_bid_price": None
                                }
                            st = price_state[asset_id]
                            st["last_bid_price"] = best_bid

                            # Забороняємо дублювати запис (якщо він уже існує, не створюємо новий)
                            record_exists = any(r.get("ASSET_ID") == asset_id for r in notifications_list)

                            # ================== BUY-логіка ==================
                            # Нові пороги: (0.8 <= best_ask <= 0.95)
                            if not record_exists:
                                # Якщо вже була перша покупка, і best_ask < 0.8 => фіксуємо падіння нижче 0.8
                                if st["first_record_created"] and (best_ask < 0.9):
                                    st["dropped_below_08"] = True

                                # Якщо опустилися нижче 0.8, а тепер зросли у [0.8..0.95], створюємо новий BUY
                                if st["dropped_below_08"] and (0.9 <= best_ask <= 0.95):
                                    st["dropped_below_08"] = False
                                    st["first_record_created"] = True

                                    new_rec = {
                                        "ASSET_ID": asset_id,
                                        "timestamp_buy": current_time_str,
                                        "timestamp_sell": "",
                                        "buy": best_ask,
                                        "sell": "",
                                        "outcome": "",
                                        "question": "",
                                        "groupItemTitle": "",
                                        "label": "",
                                        "url": ""
                                    }
                                    info = metadata_map.get(asset_id, {})
                                    new_rec["outcome"] = info.get("outcome", "")
                                    new_rec["question"] = info.get("question", "")
                                    new_rec["groupItemTitle"] = info.get("groupItemTitle", "")
                                    new_rec["label"] = info.get("label", "")
                                    new_rec["url"] = f"https://polymarket.com/event/{info.get('slug', '')}"
                                    notifications_list.append(new_rec)
                                    logging.info(f"[New cycle BUY] asset_id={asset_id}, best_ask={best_ask}")
                                    write_excel()

                                # Якщо ще не було жодної покупки і best_ask у [0.8..0.95], робимо перший BUY
                                if (not st["first_record_created"]) and (0.9 <= best_ask <= 0.95):
                                    st["first_record_created"] = True

                                    new_notif = {
                                        "ASSET_ID": asset_id,
                                        "timestamp_buy": current_time_str,
                                        "timestamp_sell": "",
                                        "buy": best_ask,
                                        "sell": "",
                                        "outcome": "",
                                        "question": "",
                                        "groupItemTitle": "",
                                        "label": "",
                                        "url": ""
                                    }
                                    info = metadata_map.get(asset_id, {})
                                    new_notif["outcome"] = info.get("outcome", "")
                                    new_notif["question"] = info.get("question", "")
                                    new_notif["groupItemTitle"] = info.get("groupItemTitle", "")
                                    new_notif["label"] = info.get("label", "")
                                    new_notif["url"] = f"https://polymarket.com/event/{info.get('slug', '')}"
                                    notifications_list.append(new_notif)
                                    logging.info(f"[First BUY] asset_id={asset_id}, best_ask={best_ask}")
                                    write_excel()

                            # ================== SELL-логіка ==================
                            # 1) if best_bid > 0.99 => sell=best_bid
                            # 2) if best_bid <= 0.6 => sell=best_bid
                            buy_recs = [
                                r for r in notifications_list
                                if r.get("ASSET_ID") == asset_id and r.get("buy") and not r.get("sell")
                            ]
                            for rec in buy_recs:
                                if rec.get("sell"):
                                    continue

                                if best_bid > 0.99:
                                    rec["sell"] = best_bid
                                    rec["timestamp_sell"] = current_time_str
                                    logging.info(f"[Set SELL (bid={best_bid})] asset_id={asset_id}")
                                    write_excel()

            logging.warning("WS connection closed or ended. Will attempt reconnect in 5s...")
            await asyncio.sleep(5)

        except asyncio.CancelledError:
            logging.info("listen_tokens_once cancelled.")
            raise
        except Exception as e:
            logging.error(f"listen_tokens_once error or lost connection: {e}")
            logging.info("Will attempt reconnect in 5s...")
            await asyncio.sleep(5)

async def subscription_manager():
    """
    Тепер перевіряємо підписку кожні 6 годин (instead of 3).
    Якщо список токенів змінився, скасовуємо поточний WS-таск і створюємо новий.
    """
    global subscribed_tokens
    ws_task = None

    while True:
        fresh = fetch_current_tokens()
        must_keep = tokens_we_still_need()
        updated_set = fresh.union(must_keep)

        if updated_set != subscribed_tokens:
            subscribed_tokens = updated_set
            logging.info(f"[Subscription Update] Tracking {len(subscribed_tokens)} tokens.")

            if ws_task and not ws_task.done():
                ws_task.cancel()
                try:
                    await ws_task
                except:
                    pass
                ws_task = None

            if subscribed_tokens:
                ws_task = asyncio.create_task(listen_tokens_once(subscribed_tokens))
            else:
                logging.info("No tokens to subscribe.")

        # чекаємо 6 годин перед наступною перевіркою
        await asyncio.sleep(6*3600)

def send_email():
    SMTP_SERVER = "hosting5.tenet.ua"
    SMTP_PORT = 465
    EMAIL_SENDER = "admin@govor.ua"
    EMAIL_PASSWORD = "knNzNTA92mS8e"
    EMAIL_RECEIVER = "admin@govor.ua"
    ATTACHMENTS = ["alertsb.xlsx"]

    msg = MIMEMultipart()
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = "PolyB alertsb.xlsx"
    msg.attach(MIMEText("Цей лист надсилається автоматично", "plain"))

    for file in ATTACHMENTS:
        try:
            with open(file, "rb") as f:
                attach_part = f.read()
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attach_part)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={file}")
            msg.attach(part)
        except Exception as e:
            logging.error(f"Failed to attach file {file}: {e}")

    context = ssl.create_default_context()
    context.set_ciphers("DEFAULT:@SECLEVEL=1")

    try:
        logging.info("Sending email...")
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        logging.info("Email sent.")
    except Exception as e:
        logging.error(f"Email error: {e}")

def email_loop():
    while SEND_EMAIL_EVERY_DAY:
        time.sleep(24 * 3600)
        send_email()

async def main_async():
    await subscription_manager()

def main():
    if SEND_EMAIL_EVERY_DAY:
        th = threading.Thread(target=email_loop, daemon=True)
        th.start()

    asyncio.run(main_async())

if __name__ == "__main__":
    main()
