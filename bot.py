#!/usr/bin/env python3
"""
Telegram Tài/Xỉu Bot (Demo, tiền ảo)

Features:
- Auto roll 3 dice every 60s in group
- Animation of three dice (edited message)
- Bets via /T<amount> and /X<amount> in group, only during open betting window
- Pot (hũ): 30% of winners' payout deducted to pot; losers' stakes go to pot
- If triple 1 or triple 6 -> split pot among winners (proportional)
- Admins can set next result or bias
- Deposit (request) via /naptien -> admin approves and bot credits user
- Withdraw via /ruttien -> admin approves or deny
- History, leaderboard (top streaks), details
- Save data to data.json
- Crash detection: if roll not posted in time, notify admins
- Demo-only: virtual currency only
"""

import os
import json
import time
import threading
import random
import traceback
from datetime import datetime
from functools import wraps

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'PUT_YOUR_TOKEN_HERE')
ADMINS = []
ADMIN_IDS_ENV = os.environ.get('ADMIN_IDS', '')  # comma separated telegram user ids
if ADMIN_IDS_ENV:
    try:
        ADMINS = [int(x.strip()) for x in ADMIN_IDS_ENV.split(',') if x.strip()]
    except:
        ADMINS = []
GROUP_ID = os.environ.get('GROUP_ID')  # optional: limit to one group id (string)
ROLL_INTERVAL = int(os.environ.get('ROLL_INTERVAL', '60'))  # seconds between auto-rolls
DATA_FILE = 'data.json'
BET_WINDOW = int(os.environ.get('BET_WINDOW', '50'))  # seconds from bet open until roll (we roll each interval)
INITIAL_BONUS = 10000  # 10k on first join (but only 1k usable per rules)
MAX_BONUS_BET = 1000

# ---------------- STORAGE ----------------
data_lock = threading.Lock()

default_data = {
    'users': {},  # user_id -> {balance:int, first_bonus_given:bool, streak:int, best_streak:int, history:[...]}
    'bets': {},   # current bets keyed by round_id -> {user_id: {'side':'T'/'X', 'amount':int}}
    'round': { 'id': 0, 'status': 'idle', 'scheduled_roll_ts': None, 'forced_next': None, 'bias': None },
    'history': [], # list of rounds: {id, timestamp, dice:[a,b,c], total, side, bets, payouts, pot_before, pot_after, distributed_from_pot}
    'pot': 0,      # hu amount
    'withdraw_requests': [], # each: {id, user_id, amount, time, status, admin_id}
    'deposit_requests': []  # each: {id, user_id, amount, time, status, admin_id}
}

def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE,'w') as f:
            json.dump(default_data,f,indent=2)
    with open(DATA_FILE,'r') as f:
        return json.load(f)

def save_data(d):
    with data_lock:
        with open(DATA_FILE,'w') as f:
            json.dump(d, f, indent=2, ensure_ascii=False)

data = load_data()

# ---------------- UTIL ----------------
def is_admin(user_id):
    return int(user_id) in ADMINS

def admin_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        uid = update.effective_user.id
        if not is_admin(uid):
            update.message.reply_text("Chỉ admin mới được dùng lệnh này.")
            return
        return func(update, context)
    return wrapper

def user_display_mask(user_id):
    s = str(user_id)
    if len(s) >= 5:
        return s[:2] + "..." + s[-3:]
    return s

def ensure_user(u):
    uid = str(u)
    if uid not in data['users']:
        data['users'][uid] = {
            'balance': 0,
            'first_bonus_given': False,
            'streak': 0,
            'best_streak': 0,
            'history': []  # list of round ids and results
        }

def give_first_bonus_if_needed(user_id):
    uid = str(user_id)
    ensure_user(user_id)
    if not data['users'][uid]['first_bonus_given']:
        data['users'][uid]['balance'] += INITIAL_BONUS
        data['users'][uid]['first_bonus_given'] = True
        # Note: restrict betting from bonus separately when checking bet acceptance
        save_data(data)
        return True
    return False

def add_balance(user_id, amount):
    ensure_user(user_id)
    data['users'][str(user_id)]['balance'] += int(amount)
    save_data(data)

def sub_balance(user_id, amount):
    ensure_user(user_id)
    data['users'][str(user_id)]['balance'] -= int(amount)
    save_data(data)

def get_balance(user_id):
    ensure_user(user_id)
    return int(data['users'][str(user_id)]['balance'])

def record_user_history(user_id, rec):
    ensure_user(user_id)
    data['users'][str(user_id)]['history'].append(rec)
    save_data(data)

# ---------------- BET / ROUND LOGIC ----------------
def open_new_round():
    # increment round id
    data['round']['id'] = data['round'].get('id', 0) + 1
    rid = data['round']['id']
    data['round']['status'] = 'open'
    # schedule next roll ts
    data['round']['scheduled_roll_ts'] = int(time.time()) + ROLL_INTERVAL
    data['bets'][str(rid)] = {}
    save_data(data)
    return rid

def close_betting_and_roll(bot: Bot, chat_id):
    # close current betting, perform roll (taking into account forced_next / bias)
    rid = data['round']['id']
    if data['round']['status'] != 'open':
        return None
    data['round']['status'] = 'closed'
    save_data(data)
    # compute result
    # admin forced?
    forced = data['round'].get('forced_next')
    dice = None
    if forced in ('T','X'):
        # generate dice that fit forced side
        dice = generate_dice_for_side(forced)
    else:
        # bias handling: if bias set as dict {'T':p, 'X':q}
        bias = data['round'].get('bias')
        if bias and isinstance(bias, dict):
            pT = bias.get('T', 0.5)
            # sample side with pT
            side = 'T' if random.random() < pT else 'X'
            dice = generate_dice_for_side(side)
        else:
            # random normal roll
            dice = [random.randint(1,6) for _ in range(3)]
    total = sum(dice)
    side = 'T' if 11 <= total <= 17 else 'X'
    # settle bets
    pot_before = data['pot']
    bets_for_round = data['bets'].get(str(rid), {})
    # compute totals
    total_bets_T = sum(bets_for_round.get(uid, {}).get('amount',0) for uid in bets_for_round if bets_for_round[uid]['side']=='T')
    total_bets_X = sum(bets_for_round.get(uid, {}).get('amount',0) for uid in bets_for_round if bets_for_round[uid]['side']=='X')
    winners = [int(uid) for uid in bets_for_round if bets_for_round[uid]['side']==side]
    losers = [int(uid) for uid in bets_for_round if bets_for_round[uid]['side']!=side]
    payouts = {}
    # payout multiplier: winners get x1.97 of stake (meaning profit 0.97 * stake)
    PAY_MULTI = 1.97
    # collect pot contributions: winners' payout contribute 30% of winners' payout profit, losers' stakes go to pot
    winners_total_stake = sum(bets_for_round[uid]['amount'] for uid in bets_for_round if bets_for_round[uid]['side']==side)
    losers_total_stake = sum(bets_for_round[uid]['amount'] for uid in bets_for_round if bets_for_round[uid]['side']!=side)
    # basic payouts: pay each winner stake * PAY_MULTI (rounded)
    for uid_str, bet in bets_for_round.items():
        uid = int(uid_str)
        amt = int(bet['amount'])
        if bet['side']==side:
            pay = int(round(amt * PAY_MULTI))
            payouts[uid] = pay
        else:
            payouts[uid] = 0
    # pot adjustments:
    # - add all losers' stakes to pot
    data['pot'] += losers_total_stake
    # - take 30% of winners' payout profits (profit = payout - stake) into pot
    winners_profit = sum(int(round(bets_for_round[uid]['amount']* (PAY_MULTI-1))) for uid in bets_for_round if bets_for_round[uid]['side']==side)
    winners_profit_cut = int(round(winners_profit * 0.30))
    data['pot'] += winners_profit_cut
    # now actually credit payouts to winners and debit stakes already reserved
    # NOTE: We didn't reserve stakes; we will deduct stakes at bet time. So here we only credit payouts (net)
    # For fair accounting: at bet time we already subtracted stake; here add payout to winners.
    for uid, pay in payouts.items():
        if pay>0:
            add_balance(uid, pay)
            # update streak
            ensure_user(uid)
            data['users'][str(uid)]['streak'] = data['users'][str(uid)].get('streak',0) + 1
            data['users'][str(uid)]['best_streak'] = max(data['users'][str(uid)].get('best_streak',0), data['users'][str(uid)]['streak'])
        else:
            # loser -> reset streak
            ensure_user(uid)
            data['users'][str(uid)]['streak'] = 0
    # check triple 1 or triple 6 for special pot distribution
    distributed_from_pot = 0
    if dice.count(1) == 3 or dice.count(6) == 3:
        # distribute full pot among winners (if any) proportionally to their stake
        if winners:
            pot_amount = data['pot']
            distributed_from_pot = pot_amount
            # compute winner stakes to split proportionally
            total_winner_stakes = sum(bets_for_round[str(uid)]['amount'] for uid in winners)
            for uid in winners:
                stake = bets_for_round[str(uid)]['amount']
                share = int(round(pot_amount * (stake / total_winner_stakes))) if total_winner_stakes>0 else int(pot_amount/len(winners))
                add_balance(uid, share)
            data['pot'] = 0
    # record history entry
    hist_rec = {
        'id': rid,
        'timestamp': int(time.time()),
        'dice': dice,
        'total': total,
        'side': side,
        'bets': { uid: data['bets'][str(rid)][uid] for uid in data['bets'][str(rid)] },
        'payouts': payouts,
        'pot_before': pot_before,
        'pot_after': data['pot'],
        'distributed_from_pot': distributed_from_pot
    }
    data['history'].append(hist_rec)
    # cleanup
    data['bets'].pop(str(rid), None)
    data['round']['status'] = 'idle'
    data['round']['forced_next'] = None
    save_data(data)
    # compose message
    text = f"🎲 Phiên #{rid} — Kết quả:\n"
    # animation will be handled outside (we return dice and text)
    return hist_rec

def generate_dice_for_side(side):
    # generate dice triple so that total falls into desired side (heuristic trying random until match)
    for _ in range(200):
        d = [random.randint(1,6) for _ in range(3)]
        tot = sum(d)
        s = 'T' if 11 <= tot <= 17 else 'X'
        if s == side:
            return d
    # fallback: random
    return [random.randint(1,6) for _ in range(3)]

# ---------------- BOT HANDLERS ----------------
updater = Updater(BOT_TOKEN, use_context=True)
bot = updater.bot

def run_in_group_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        chat = update.effective_chat
        if chat is None:
            return
        if GROUP_ID:
            # require group id match
            if str(chat.id) != str(GROUP_ID):
                update.message.reply_text("Bot chỉ hoạt động trong group được cấu hình.")
                return
        else:
            # allow only group/supergroup messages
            if chat.type not in ('group','supergroup'):
                update.message.reply_text("Lệnh này chỉ dùng trong group.")
                return
        return func(update, context)
    return wrapper

def start_cmd(update: Update, context: CallbackContext):
    user = update.effective_user
    ensure_user(user.id)
    given = give_first_bonus_if_needed(user.id)
    txt = f"Xin chào {user.first_name}! Đây là bot Tài/Xỉu demo (tiền ảo).\n"
    if given:
        txt += f"Bạn đã được tặng khuyến mãi {INITIAL_BONUS} VNĐ (chỉ dùng tối đa {MAX_BONUS_BET} cho cược ban đầu)."
    txt += "\nDùng /balance để xem số dư."
    update.message.reply_text(txt)

def balance_cmd(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    ensure_user(uid)
    bal = get_balance(uid)
    update.message.reply_text(f"Số dư của bạn: {bal} VNĐ")

# betting command parser: e.g. /T1000 or /X500
@run_in_group_only
def handle_bet_command(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    user = update.effective_user
    # parse patterns: /T1000 , /t1000 , /X500
    import re
    m = re.match(r'^\/([TtXx])\s*([0-9]+)$', text)
    if not m:
        # also accept without slash if people type just T1000? we require slash
        return
    side = m.group(1).upper()
    amt = int(m.group(2))
    uid = user.id
    # only accept if current round open
    rid = data['round'].get('id', 0)
    if data['round'].get('status') != 'open':
        update.message.reply_text("Hiện không mở cửa cược. Xin chờ phiên mới.")
        return
    # check balance
    ensure_user(uid)
    bal = get_balance(uid)
    # enforce bonus staking limit: if user has just bonus and balance equals INITIAL_BONUS and first time, limit bet to MAX_BONUS_BET
    if data['users'][str(uid)]['first_bonus_given'] and bal == INITIAL_BONUS:
        if amt > MAX_BONUS_BET:
            update.message.reply_text(f"Bạn chỉ được cược tối đa {MAX_BONUS_BET} VNĐ sử dụng tiền thưởng ban đầu.")
            return
    if amt <= 0:
        update.message.reply_text("Số tiền không hợp lệ.")
        return
    if amt > bal:
        update.message.reply_text("Số tiền không đủ.")
        return
    # deduct stake immediately
    sub_balance(uid, amt)
    # record bet
    bids = data['bets'].setdefault(str(rid), {})
    bids[str(uid)] = {'side': 'T' if side=='T' else 'X', 'amount': amt}
    save_data(data)
    update.message.reply_text(f"Đã nhận cược {amt} VNĐ cho {'Tài' if side=='T' else 'Xỉu'} (Phiên #{rid}). Số dư mới: {get_balance(uid)} VNĐ")

@run_in_group_only
def open_bet_cmd(update: Update, context: CallbackContext):
    # allow admin to force open a new round, else normal auto opening used
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Chỉ admin mới mở cược thủ công.")
        return
    rid = open_new_round()
    # announce
    update.message.reply_text(f"🔔 Mở cược Phiên #{rid}. Bạn có {ROLL_INTERVAL} giây để đặt cược.")
    # scheduled roll will happen by background scheduler

@run_in_group_only
def show_history_cmd(update: Update, context: CallbackContext):
    # show last N rounds summary
    N = 10
    h = data.get('history', [])[-N:]
    if not h:
        update.message.reply_text("Chưa có lịch sử.")
        return
    txts = []
    for rec in reversed(h):
        dice = rec['dice']
        side = rec['side']
        txts.append(f"#{rec['id']} {dice[0]}+{dice[1]}+{dice[2]}={rec['total']} → {'Tài' if side=='T' else 'Xỉu'} (pot {rec['pot_before']}→{rec['pot_after']})")
    update.message.reply_text("\n".join(txts))

# deposit request
@run_in_group_only
def deposit_cmd(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    import re
    m = re.match(r'^\/naptien\s+([0-9]+)$', text, re.IGNORECASE)
    if not m:
        update.message.reply_text("Dùng: /naptien <số tiền>. (Admin sẽ xác thực và credit thủ công trong demo).")
        return
    amt = int(m.group(1))
    uid = update.effective_user.id
    rid = int(time.time()*1000)
    req = {'id': rid, 'user_id': uid, 'amount': amt, 'time': int(time.time()), 'status': 'pending', 'admin_id': None}
    data['deposit_requests'].append(req)
    save_data(data)
    # notify admins with inline buttons
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve_deposit:{rid}"), InlineKeyboardButton("Deny", callback_data=f"deny_deposit:{rid}")]])
    for a in ADMINS:
        try:
            bot.send_message(chat_id=a, text=f"📥 Deposit request #{rid} từ {user_display_mask(uid)}: {amt} VNĐ", reply_markup=kb)
        except Exception:
            pass
    update.message.reply_text("Đã gửi yêu cầu nạp tiền tới admin. Chờ phê duyệt.")

# withdraw request
@run_in_group_only
def withdraw_cmd(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    import re
    m = re.match(r'^\/ruttien\s+([0-9]+)$', text, re.IGNORECASE)
    if not m:
        update.message.reply_text("Dùng: /ruttien <số tiền>. Lưu ý rút tối thiểu 100000 VNĐ và bạn phải đã cược ít nhất 1 vòng tương ứng số tiền nạp (rule demo).")
        return
    amt = int(m.group(1))
    uid = update.effective_user.id
    if amt < 100000:
        update.message.reply_text("Rút tối thiểu 100000 VNĐ.")
        return
    # additional check: user must have bet at least once after deposit of that amount: for demo we skip deep verification
    if get_balance(uid) < amt:
        update.message.reply_text("Số dư không đủ để rút.")
        return
    rid = int(time.time()*1000)
    req = {'id': rid, 'user_id': uid, 'amount': amt, 'time': int(time.time()), 'status': 'pending', 'admin_id': None}
    data['withdraw_requests'].append(req)
    save_data(data)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve_withdraw:{rid}"), InlineKeyboardButton("Deny", callback_data=f"deny_withdraw:{rid}")]])
    for a in ADMINS:
        try:
            bot.send_message(chat_id=a, text=f"📤 Withdraw request #{rid} từ {user_display_mask(uid)}: {amt} VNĐ", reply_markup=kb)
        except Exception:
            pass
    update.message.reply_text("Yêu cầu rút tiền đã gửi. Chờ admin xử lý.")

# admin approve handlers
def callback_query_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data_str = query.data
    user = query.from_user
    if not is_admin(user.id):
        query.answer("Chỉ admin được phê duyệt.")
        return
    if data_str.startswith('approve_deposit:') or data_str.startswith('deny_deposit:'):
        rid = int(data_str.split(':',1)[1])
        for r in data['deposit_requests']:
            if r['id']==rid:
                if data_str.startswith('approve_deposit:'):
                    r['status']='approved'
                    r['admin_id']=user.id
                    add_balance(r['user_id'], r['amount'])
                    save_data(data)
                    query.answer("Đã approve deposit")
                    # notify group briefly masked
                    masked = user_display_mask(r['user_id'])
                    try:
                        bot.send_message(chat_id=GROUP_ID or update.effective_chat.id, text=f"📥 {masked} đã nạp {r['amount']} VNĐ (đã xác thực bởi admin).")
                    except:
                        pass
                else:
                    r['status']='denied'; r['admin_id']=user.id; save_data(data)
                    query.answer("Đã từ chối deposit")
                return
    if data_str.startswith('approve_withdraw:') or data_str.startswith('deny_withdraw:'):
        rid = int(data_str.split(':',1)[1])
        for r in data['withdraw_requests']:
            if r['id']==rid:
                if data_str.startswith('approve_withdraw:'):
                    r['status']='approved'; r['admin_id']=user.id
                    # debit user's balance and notify
                    sub_balance(r['user_id'], r['amount'])
                    save_data(data)
                    query.answer("Đã approve withdraw")
                    masked = user_display_mask(r['user_id'])
                    try:
                        bot.send_message(chat_id=GROUP_ID or update.effective_chat.id, text=f"📤 {masked} rút {r['amount']} VNĐ (đã duyệt).")
                    except:
                        pass
                else:
                    r['status']='denied'; r['admin_id']=user.id; save_data(data)
                    query.answer("Đã từ chối rút tiền")
                return
    # admin set next forced
    if data_str.startswith('force_next:'):
        val = data_str.split(':',1)[1]
        data['round']['forced_next'] = val  # 'T' or 'X'
        save_data(data)
        query.answer(f"Đã set forced next -> {val}")
        return
    query.answer()

# leaderboard
def leaderboard_cmd(update: Update, context: CallbackContext):
    # top 10 by best_streak or balance — let's show best_streak
    users = []
    for uid, info in data['users'].items():
        users.append((int(uid), info.get('best_streak',0), info.get('balance',0)))
    users.sort(key=lambda x: x[1], reverse=True)
    top = users[:10]
    lines = []
    for uid, streak, bal in top:
        lines.append(f"{user_display_mask(uid)} — best streak: {streak}, bal: {bal}")
    update.message.reply_text("🏆 Top streaks:\n" + ("\n".join(lines) if lines else "Chưa có dữ liệu"))

# admin set next or bias
@admin_only
def admin_setnext(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    parts = text.split()
    if len(parts)<2 or parts[1].upper() not in ('T','X','NONE'):
        update.message.reply_text("Dùng: /setnext T|X|NONE")
        return
    val = parts[1].upper()
    if val == 'NONE':
        data['round']['forced_next'] = None
    else:
        data['round']['forced_next'] = val
    save_data(data)
    update.message.reply_text(f"Đã set forced next = {data['round']['forced_next']}")

@admin_only
def admin_setbias(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    # format: /setbias T:0.6 (means P(T)=0.6)
    import re
    m = re.match(r'^\/setbias\s+T:([0-9]*\.?[0-9]+)$', text, re.IGNORECASE)
    if not m:
        update.message.reply_text("Dùng: /setbias T:0.6  (value between 0 and 1)")
        return
    v = float(m.group(1))
    if v<0 or v>1:
        update.message.reply_text("Value invalid")
        return
    data['round']['bias'] = {'T': v, 'X': 1-v}
    save_data(data)
    update.message.reply_text(f"Đã set bias T={v}, X={1-v}")

# command to show pot
def pot_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(f"Hũ hiện tại: {data.get('pot',0)} VNĐ")

# quick admin adjust user balance
@admin_only
def admin_credit(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    parts = text.split()
    if len(parts)<3:
        update.message.reply_text("Dùng: /credit <user_id> <amount>")
        return
    uid = int(parts[1])
    amt = int(parts[2])
    add_balance(uid, amt)
    update.message.reply_text(f"Đã cộng {amt} cho {uid}")

# ---------------- SCHEDULER / AUTO RUN ----------------
# We'll run a background thread that:
# - opens round if idle
# - waits until scheduled roll time
# - closes betting, rolls, posts result
# - repeats

stop_event = threading.Event()

def post_roll_with_animation(chat_id, hist_rec):
    rid = hist_rec['id']
    dice = hist_rec['dice']
    total = hist_rec['total']
    side = hist_rec['side']
    # create animation: send initial message then edit showing dice one by one
    try:
        m = bot.send_message(chat_id=chat_id, text=f"🎲 Phiên #{rid} đang mở kết quả...")
        # show dice one by one
        emojis = {1:"⚀",2:"⚁",3:"⚂",4:"⚃",5:"⚄",6:"⚅"}
        text = f"🎲 Phiên #{rid}\n"
        text += "Kết quả: "
        # show placeholders then fill
        text += " _ _ _ \n"
        bot.edit_message_text(chat_id=chat_id, message_id=m.message_id, text=text)
        time.sleep(0.9)
        # fill first
        text = f"🎲 Phiên #{rid}\nKết quả: {emojis[dice[0]]} _ _ \n"
        bot.edit_message_text(chat_id=chat_id, message_id=m.message_id, text=text)
        time.sleep(0.9)
        text = f"🎲 Phiên #{rid}\nKết quả: {emojis[dice[0]]} {emojis[dice[1]]} _ \n"
        bot.edit_message_text(chat_id=chat_id, message_id=m.message_id, text=text)
        time.sleep(0.9)
        text = f"🎲 Phiên #{rid}\nKết quả: {emojis[dice[0]]} {emojis[dice[1]]} {emojis[dice[2]]} = {total}\n→ {'Tài' if side=='T' else 'Xỉu'}"
        # include pot info
        text += f"\nHũ: {hist_rec['pot_before']} → {hist_rec['pot_after']}"
        bot.edit_message_text(chat_id=chat_id, message_id=m.message_id, text=text)
    except Exception as e:
        print("Error posting roll animation:", e)
        # fallback simple post
        try:
            bot.send_message(chat_id=chat_id, text=f"🎲 Phiên #{rid}: {dice[0]}+{dice[1]}+{dice[2]} = {total} → {'Tài' if side=='T' else 'Xỉu'}")
        except:
            pass

def scheduler_loop(chat_id):
    # run until stopped
    try:
        while not stop_event.is_set():
            try:
                if data['round']['status'] == 'idle':
                    # open a round automatically
                    rid = open_new_round()
                    # announce open
                    try:
                        bot.send_message(chat_id=chat_id, text=f"🔔 Mở cược Phiên #{rid}. Có {ROLL_INTERVAL} giây để đặt cược.")
                    except:
                        pass
                # wait until scheduled roll ts or until stop
                now = int(time.time())
                sched = data['round'].get('scheduled_roll_ts') or (now + ROLL_INTERVAL)
                # sleep until scheduled time (but wake up occasionally to detect stop)
                while int(time.time()) < sched and not stop_event.is_set():
                    time.sleep(1)
                # time to close and roll
                hist = close_betting_and_roll(bot, chat_id)
                if hist:
                    # post result with animation
                    post_roll_with_animation(chat_id, hist)
                # small pause before opening next
                time.sleep(1)
            except Exception as e:
                print("Scheduler loop error:", e, traceback.format_exc())
                # notify admins about crash
                for a in ADMINS:
                    try:
                        bot.send_message(chat_id=a, text=f"⚠️ Bot scheduler error: {str(e)}")
                    except:
                        pass
                # wait a bit then continue
                time.sleep(10)
    except Exception as e:
        print("Scheduler outer error", e)
        for a in ADMINS:
            try:
                bot.send_message(chat_id=a, text=f"🚨 Bot stopped unexpectedly: {e}")
            except:
                pass

# crash detection: monitor if last history round time is too old vs now
def crash_monitor_loop(chat_id):
    try:
        while not stop_event.is_set():
            try:
                last = data['history'][-1] if data['history'] else None
                if last:
                    last_ts = last['timestamp']
                    if int(time.time()) - last_ts > (ROLL_INTERVAL*3):
                        for a in ADMINS:
                            try:
                                bot.send_message(chat_id=a, text=f"⚠️ Warning: Last roll was at {datetime.fromtimestamp(last_ts)}, system may be down.")
                            except:
                                pass
                time.sleep(ROLL_INTERVAL*2)
            except Exception as e:
                time.sleep(5)
    except Exception:
        pass

# ---------------- Setup handlers ----------------
dp = updater.dispatcher
dp.add_handler(CommandHandler('start', start_cmd))
dp.add_handler(CommandHandler('balance', balance_cmd))
dp.add_handler(CommandHandler('history', show_history_cmd))
dp.add_handler(CommandHandler('leaderboard', leaderboard_cmd))
dp.add_handler(CommandHandler('pot', pot_cmd))
dp.add_handler(CommandHandler('openbet', open_bet_cmd))
dp.add_handler(CommandHandler('naptien', deposit_cmd))
dp.add_handler(CommandHandler('ruttien', withdraw_cmd))
dp.add_handler(CommandHandler('setnext', admin_setnext))
dp.add_handler(CommandHandler('setbias', admin_setbias))
dp.add_handler(CommandHandler('credit', admin_credit))
dp.add_handler(CallbackQueryHandler(callback_query_handler))
# bets: handle text matching /T1000 or /X500
dp.add_handler(MessageHandler(Filters.regex(r'^\/[TtXx]\s*[0-9]+$') & Filters.group, handle_bet_command))

# start scheduler threads after bot starts polling
def start_background(chat_id):
    sched_thread = threading.Thread(target=scheduler_loop, args=(chat_id,), daemon=True)
    sched_thread.start()
    crash_thread = threading.Thread(target=crash_monitor_loop, args=(chat_id,), daemon=True)
    crash_thread.start()

# ---------- START BOT ----------
def main():
    print("Admins:", ADMINS)
    updater.start_polling()
    # determine a chat id to post: prefer GROUP_ID, else wait for first group message
    if GROUP_ID:
        chat_id = int(GROUP_ID)
        start_background(chat_id)
    else:
        # no GROUP_ID configured: will wait until bot gets added to group; we will find a group id from recent updates
        print("GROUP_ID not configured: bot will start background only after joined a group.")
        # Try to get updates to find a group id - or admin should call /openbet with bot in group
    updater.idle()

import threading
import time

def start_bot_background():
    # Chạy hàm main() trong thread riêng
    thread = threading.Thread(target=main)
    thread.daemon = True
    thread.start()

if __name__ == "__main__":
    print("Starting bot...")

    # Nếu có biến môi trường ADMIN_IDS_ENV thì parse
    if ADMIN_IDS_ENV and not ADMINS:
        try:
            ADMINS = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip()]
        except Exception:
            ADMINS = []

    # Khởi động bot trong background
    start_bot_background()

    # Giữ cho process chính không bị thoát
    while True:
        time.sleep(60)
