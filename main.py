import os, json, base64, requests, re, threading, time, uuid
from copy import deepcopy
from datetime import datetime, timedelta
from flask import Flask, request

app = Flask(__name__)
TOKEN = os.getenv('BOT_TOKEN','').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
PROMPTINO_CHAT = os.getenv('PROMPTINO_CHAT','@PromptinoChannel').strip()
PROMPTINO_CHANNEL = os.getenv('PROMPTINO_CHANNEL','https://t.me/PromptinoChannel').strip()
OWNER_USERNAME = os.getenv('OWNER_USERNAME','').strip().lstrip('@')
BOT_USERNAME = os.getenv('BOT_USERNAME','').strip().lstrip('@')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN','').strip()
GITHUB_REPO = os.getenv('GITHUB_REPO','Amirsk13/promptino-bot').strip()
GITHUB_BRANCH = os.getenv('GITHUB_BRANCH','main').strip()
DATA_FILE = 'promptino_data.json'
CHATGPT_URL = os.getenv('CHATGPT_URL','https://chatgpt.com/').strip()
GEMINI_URL = os.getenv('GEMINI_URL','https://gemini.google.com/').strip()
FLOW_URL = os.getenv('FLOW_URL','https://labs.google/fx/tools/flow').strip()
TRAINING_POST_URL = os.getenv('TRAINING_POST_URL',PROMPTINO_CHANNEL).strip()
ORDERS_CHAT = os.getenv('ORDERS_CHAT','').strip()
VIP_ORDERS_CHAT = os.getenv('VIP_ORDERS_CHAT','').strip()
ARCHIVE_CHAT = os.getenv('ARCHIVE_CHAT','').strip()
TESTIMONIALS_CHAT = os.getenv('TESTIMONIALS_CHAT','').strip()
ORDER_RETENTION_DAYS = 14
API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
GH = 'https://api.github.com'

# All conversational state is kept here. Every text/photo message checks active state first.
STATES = {}
POST_STATES = {}
CURRENT_CB = None
RECENT_ACTIONS = {}

# Conversation state must survive a worker restart/sleep.  Without this, Render
# can restart the process between two perfectly normal customer messages and the
# next contact/receipt/photo is then routed as a new message, which looks exactly
# like the bot received /start and returned to the menu.  Order states are mirrored
# into DATA['active_order_states'] and restored on startup.
ORDER_STATE_TYPES = {'normal','customer','logo','vip_order'}
STATE_PERSIST_LOCK = threading.RLock()
STATE_PERSIST_PENDING = None
STATE_PERSIST_RUNNING = False
CONV_PERSIST_PENDING = None
CONV_PERSIST_RUNNING = False
DATA_SAVE_LOCK = threading.RLock()

# Telegram may retry a webhook update when the first request is slow, and a web
# server can also have overlapping webhook requests.  Processing the same update
# twice can advance STATES twice (contact -> card -> payer, receipt -> reference,
# etc.) and make a valid message look like it restarted the flow.  Keep update
# processing serialized and make Telegram update_id handling idempotent.
UPDATE_LOCK = threading.RLock()
PROCESSED_UPDATES = {}
PROCESSED_UPDATE_TTL = 300
PROCESSED_UPDATE_LIMIT = 2000
PROCESSED_CALLBACKS = {}
PROCESSED_CALLBACK_LIMIT = 1000
# Protect against the same inline-button action arriving as two distinct
# callback_query IDs (for example after a slow/duplicated webhook delivery).
PROCESSED_CALLBACK_ACTIONS = {}
PROCESSED_CALLBACK_ACTION_TTL = 8

PERMISSIONS = ['prompts','prices','vip','orders','vip_orders','logo_orders','reports','ads','training','admins','stats','notifications','settings','customer_data']

# Admin/editor flows are intentionally kept separate from customer order flows.
# A stale editor state must never be allowed to consume the next message of a
# different admin operation (post/VIP/price/testimonial).
ADMIN_STATE_TYPES = {
    'price','card_manage','vip_add','vip_edit','prompt_manage','testimonial',
    'report_result','training_add','training_edit','ad_add','ad_edit','admin_add',
    'msg_user'
}
# Only the admin flows reported as broken are made restart-safe here. Other admin
# flows keep their existing in-memory behavior and are intentionally untouched.
PERSISTED_ADMIN_STATE_TYPES = {'price','vip_add','testimonial'}

def reset_admin_flow(uid, *, clear_post=True):
    """Reset only back-office conversational state; never erase an order flow."""
    if clear_post:
        POST_STATES.pop(uid,None)
    st=STATES.get(uid)
    if isinstance(st,dict) and st.get('type') in ADMIN_STATE_TYPES:
        STATES.pop(uid,None)

DEFAULT_DATA = {
    'prompts': {},
    'vip': {},
    'orders': [],
    'completed_orders': [],
    'reports': [],
    'cards': [],
    'trainings': [],
    'ads': [],
    'admins': {},
    'users': {},
    'prices': {'channel_prompt': 0, 'customer_prompt': 0, 'logo': 0},
    'vip_customer_refs': {},
    'active_order_states': {},
    'active_admin_states': {},
    'active_post_states': {},
    'settings': {'notifications': {'new_order': True, 'payment': True, 'report': True, 'vip_order': True}},
    'counters': {'order': 0, 'customer': 0, 'logo': 0, 'vip': 0, 'report': 0},
    'messages': {
        'order_created': '✅ سفارش ثبت شد و برای ادمین ارسال شد.\n🆔 {order_id}',
        'payment_ok': '✅ پرداخت شما تأیید شد.\n🆔 {order_id}\n\n🟡 سفارش شما تأیید شد و در حال انجام است.' ,
        'payment_bad': '❌ پرداخت سفارش {order_id} تأیید نشد.\n\nاگر فکر می‌کنی اشتباهی رخ داده، از بخش «⚠️ گزارش مشکل» مشکل را گزارش بده.',
        'report_created': '✅ گزارش شما برای ادمین ارسال شد.',
        'report_result': '💬 نتیجه بررسی گزارش شما:\n\n{result}'
    }
}

def api(method, data=None):
    try:
        if not API: return {}
        return requests.post(f'{API}/{method}', json=data or {}, timeout=25).json()
    except Exception as e:
        print('API', method, e); return {}

def send(chat_id, text, keyboard=None, parse_mode=None):
    d={'chat_id':chat_id,'text':text}
    if keyboard: d['reply_markup']=keyboard
    if parse_mode: d['parse_mode']=parse_mode
    return api('sendMessage',d)

def send_photo(chat_id, photo, caption='', keyboard=None):
    d={'chat_id':chat_id,'photo':photo,'caption':caption}
    if keyboard: d['reply_markup']=keyboard
    return api('sendPhoto',d)

def answer(callback_id):
    if callback_id: api('answerCallbackQuery', {'callback_query_id': callback_id})

def kb(rows): return {'inline_keyboard': rows}
def btn(text,data): return {'text':text,'callback_data':data}
def urlbtn(text,url): return {'text':text,'url':url}
def money(x):
    try: return f'{int(x):,}'
    except: return str(x)

def github_headers():
    return {'Authorization':f'Bearer {GITHUB_TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

def gh_get(path):
    if not GITHUB_TOKEN: return None,None
    r=requests.get(f'{GH}/repos/{GITHUB_REPO}/contents/{path}', headers=github_headers(), params={'ref':GITHUB_BRANCH}, timeout=25)
    if r.status_code==404: return None,None
    r.raise_for_status(); j=r.json()
    return json.loads(base64.b64decode(j['content']).decode()), j.get('sha')

def gh_save(path,obj,msg):
    if not GITHUB_TOKEN: return False
    try:
        _,sha=gh_get(path)
        d={'message':msg,'content':base64.b64encode(json.dumps(obj,ensure_ascii=False,indent=2).encode()).decode(),'branch':GITHUB_BRANCH}
        if sha: d['sha']=sha
        r=requests.put(f'{GH}/repos/{GITHUB_REPO}/contents/{path}',headers=github_headers(),json=d,timeout=25)
        r.raise_for_status(); return True
    except Exception as e:
        print('GitHub save',e); return False

def deep_merge(default, actual):
    out=deepcopy(default)
    if not isinstance(actual,dict): return out
    for k,v in actual.items():
        if isinstance(v,dict) and isinstance(out.get(k),dict): out[k]=deep_merge(out[k],v)
        else: out[k]=v
    return out

def load_data():
    try:
        obj,_=gh_get(DATA_FILE)
        return deep_merge(DEFAULT_DATA,obj or {})
    except Exception as e:
        print('GitHub load',e); return deepcopy(DEFAULT_DATA)

def save_data(msg='Promptino update'):
    with DATA_SAVE_LOCK:
        return gh_save(DATA_FILE, DATA, msg)
DATA = load_data()

# Restore conversational flows that must survive a worker restart.
# Customer orders, admin/editor operations, and post creation are restored separately
# so one flow can never be mistaken for another.
def restore_order_states():
    saved=DATA.get('active_order_states',{})
    if not isinstance(saved,dict): return
    for uid,state in saved.items():
        try: key=int(uid)
        except Exception: continue
        if isinstance(state,dict) and state.get('type') in ORDER_STATE_TYPES:
            restored=deepcopy(state)
            ensure_order_flow_state(restored)
            STATES[key]=restored

restore_order_states()

def _restore_admin_states():
    saved=DATA.get('active_admin_states',{})
    if not isinstance(saved,dict): return
    for uid,state in saved.items():
        try: key=int(uid)
        except Exception: continue
        if isinstance(state,dict) and state.get('type') in PERSISTED_ADMIN_STATE_TYPES:
            STATES[key]=deepcopy(state)

def _restore_post_states():
    saved=DATA.get('active_post_states',{})
    if not isinstance(saved,dict): return
    for uid,state in saved.items():
        try: key=int(uid)
        except Exception: continue
        if isinstance(state,dict) and state.get('step') in {'photos','name','number','suitable','for','buttons','button_label','button_prompt'}:
            POST_STATES[key]=deepcopy(state)

_restore_admin_states()
_restore_post_states()

def new_order_flow_id():
    """Create an opaque token that uniquely identifies one customer order flow."""
    return uuid.uuid4().hex[:12]

def ensure_order_flow_state(state):
    """Normalize a restored/new order state without changing its business data."""
    if not isinstance(state, dict) or state.get('type') not in ORDER_STATE_TYPES:
        return state
    state.setdefault('flow_id', new_order_flow_id())
    return state

def order_state_matches(uid, flow_id=None, expected_types=None, expected_steps=None):
    s=STATES.get(uid)
    if not isinstance(s,dict) or s.get('type') not in ORDER_STATE_TYPES:
        return False
    ensure_order_flow_state(s)
    if flow_id is not None and s.get('flow_id') != flow_id:
        return False
    if expected_types is not None and s.get('type') not in expected_types:
        return False
    if expected_steps is not None and s.get('step') not in expected_steps:
        return False
    return True

def clear_order_state(uid):
    """Clear only a customer-facing order flow and immediately queue its removal."""
    s=STATES.get(uid)
    if isinstance(s,dict) and s.get('type') in ORDER_STATE_TYPES:
        STATES.pop(uid,None)
        persist_order_states_async()
        return True
    return False

def _snapshot_order_states():
    return {str(uid):deepcopy(state) for uid,state in STATES.items()
            if isinstance(state,dict) and state.get('type') in ORDER_STATE_TYPES}

def _persist_order_states_worker(snapshot):
    global STATE_PERSIST_PENDING, STATE_PERSIST_RUNNING
    try:
        # Serialize GitHub writes so state snapshots cannot overwrite one another.
        with STATE_PERSIST_LOCK:
            DATA['active_order_states']=snapshot
            save_data('Persist active order state')
    except Exception as e:
        print('Persist active order states',e)
    finally:
        with STATE_PERSIST_LOCK:
            STATE_PERSIST_RUNNING=False
            pending=STATE_PERSIST_PENDING
            STATE_PERSIST_PENDING=None
            if pending is not None:
                STATE_PERSIST_RUNNING=True
                threading.Thread(target=_persist_order_states_worker,args=(pending,),daemon=True).start()

def persist_order_states_async():
    global STATE_PERSIST_PENDING, STATE_PERSIST_RUNNING
    snapshot=_snapshot_order_states()
    with STATE_PERSIST_LOCK:
        STATE_PERSIST_PENDING=snapshot
        if STATE_PERSIST_RUNNING: return
        STATE_PERSIST_RUNNING=True
    threading.Thread(target=_persist_order_states_worker,args=(snapshot,),daemon=True).start()

def _snapshot_conversation_states():
    admin={str(uid):deepcopy(state) for uid,state in STATES.items()
           if isinstance(state,dict) and state.get('type') in PERSISTED_ADMIN_STATE_TYPES}
    posts={str(uid):deepcopy(state) for uid,state in POST_STATES.items()
           if isinstance(state,dict)}
    for state in posts.values():
        state.setdefault('flow_id',new_post_flow_id())
    return admin,posts

def _persist_conversation_states_worker(snapshot):
    global CONV_PERSIST_PENDING, CONV_PERSIST_RUNNING
    try:
        with STATE_PERSIST_LOCK:
            admin,posts=snapshot
            DATA['active_admin_states']=admin
            DATA['active_post_states']=posts
            save_data('Persist active admin/post state')
    except Exception as e:
        print('Persist admin/post states',e)
    finally:
        with STATE_PERSIST_LOCK:
            CONV_PERSIST_RUNNING=False
            pending=CONV_PERSIST_PENDING
            CONV_PERSIST_PENDING=None
            if pending is not None:
                CONV_PERSIST_RUNNING=True
                threading.Thread(target=_persist_conversation_states_worker,args=(pending,),daemon=True).start()

def persist_conversation_states_async():
    global CONV_PERSIST_PENDING, CONV_PERSIST_RUNNING
    snapshot=_snapshot_conversation_states()
    with STATE_PERSIST_LOCK:
        CONV_PERSIST_PENDING=snapshot
        if CONV_PERSIST_RUNNING: return
        CONV_PERSIST_RUNNING=True
    threading.Thread(target=_persist_conversation_states_worker,args=(snapshot,),daemon=True).start()

def is_owner(uid): return uid in ADMIN_IDS
def admin_record(uid): return DATA.get('admins',{}).get(str(uid),{})
def admin_role(uid):
    if is_owner(uid): return 'plus'
    return admin_record(uid).get('role','normal')
def is_plus(uid): return is_owner(uid) or admin_role(uid)=='plus'
def is_admin(uid): return is_owner(uid) or str(uid) in DATA.get('admins',{})
def has_perm(uid,perm):
    if is_owner(uid): return True
    p=admin_record(uid).get('permissions',[])
    if isinstance(p,dict): return bool(p.get(perm))
    return perm in p
def can_private_data(uid):
    return is_owner(uid) or (is_plus(uid) and has_perm(uid,'customer_data'))

def remember_user(uid, m):
    if not uid: return
    u=m.get('from',{}) if isinstance(m,dict) else {}
    username=(u.get('username') or '').strip().lstrip('@')
    if username:
        DATA.setdefault('users',{})[username.lower()]={'id':uid,'username':username}

def resolve_user_id(value):
    value=value.strip()
    if value.isdigit(): return value
    if value.startswith('@'):
        rec=DATA.get('users',{}).get(value.lstrip('@').lower())
        if rec: return str(rec.get('id'))
    return None
def guard(uid,perm): return is_admin(uid) and has_perm(uid,perm)

def main_menu(uid):
    testimonial_url=os.getenv('TESTIMONIALS_CHANNEL_URL','').strip()
    if not testimonial_url and TESTIMONIALS_CHAT.startswith('@'): testimonial_url=f'https://t.me/{TESTIMONIALS_CHAT.lstrip("@")}'
    report_btn = btn('⚠️ گزارش مشکلات','adm_reports') if guard(uid,'reports') else btn('⚠️ گزارش مشکل','report_start')
    rows=[[btn('📚 آموزش‌های اولیه','user_training'),btn('👑 VIP','user_vip')],
          [btn('🖼 سفارش ساخت عکس با پرامپت کانال','order_menu')],
          [btn('✍️ سفارش ساخت عکس با پرامپت مشتری','customer_order')],
          [btn('🎨 سفارش ساخت لوگو','logo_order')],
          [report_btn],
          [urlbtn('📣 کانال رضایت مشتری',testimonial_url)] if testimonial_url else [btn('📣 کانال رضایت مشتری','testimonial_channel_missing')],
          [urlbtn('📢 کانال پرامپتینو',PROMPTINO_CHANNEL)]]
    if is_admin(uid): rows.append([btn('⚙️ مدیریت','admin_menu')])
    return kb(rows)

WELCOME='🤖 سلام! به پرامپتینو خوش اومدی 👋\n\nاز منوی زیر انتخاب کن:'

def admin_menu(uid):
    rows=[]
    candidates=[
        ('prompts','📝 پرامپت‌ها','adm_prompts'),('prices','💰 قیمت‌ها','adm_prices'),
        ('vip','👑 VIP','adm_vip'),('orders','📦 سفارشات','adm_orders'),
        ('vip_orders','📦 سفارشات VIP','adm_vip_orders'),('logo_orders','🎨 سفارشات لوگو','adm_logo_orders'),('reports','⚠️ گزارش مشکلات','adm_reports'),
        ('ads','📣 تبلیغات','adm_ads'),('training','📚 آموزش','adm_training'),
        ('admins','👥 ادمین‌ها','adm_admins'),('stats','📊 آمار','adm_stats'),
        ('notifications','🔔 تنظیمات اعلان‌ها','adm_notifications'),('settings','⚙️ تنظیمات','adm_settings')]
    visible=[(label,cb) for perm,label,cb in candidates if guard(uid,perm)]
    for i in range(0,len(visible),2): rows.append([btn(*x) for x in visible[i:i+2]])
    if guard(uid,'prompts'): rows.append([btn('➕ افزودن پست','post_start')])
    if guard(uid,'orders') or guard(uid,'vip_orders'): rows.append([btn('⭐ ثبت رضایت','testimonial_start')])
    if guard(uid,'orders') or guard(uid,'vip_orders'): rows.append([btn('📣 کانال رضایت مشتری','testimonial_channel_admin')])
    rows.append([btn('🔙 بازگشت','back_main')])
    return kb(rows)

def next_id(kind):
    DATA.setdefault('counters',{})[kind]=int(DATA.get('counters',{}).get(kind,0))+1
    prefix={'order':'ORD','customer':'CUST','logo':'LOGO','vip':'VIP','report':'R'}.get(kind,kind.upper())
    return f'{prefix}-{DATA["counters"][kind]}'

def price_for(key):
    if key in DATA.get('vip',{}): return int(DATA['vip'][key].get('price',0) or 0)
    if key in DATA.get('prompts',{}):
        # All channel prompts use one global production price.
        return int(DATA.setdefault('prices',{}).get('channel_prompt',0) or 0)
    return int(DATA.setdefault('prices',{}).get(key,0) or 0)


def normalize_pid(x):
    x=x.strip().lower()
    return x if x.startswith('p') else 'p'+x

def vip_label(k,v):
    n=re.sub(r'\D','',k) or k.replace('VIP-','')
    return f'VIP{n} | {v.get("title","")}'

def find_order(oid): return next((o for o in DATA.get('orders',[]) if o.get('id')==oid),None)
def find_report(rid): return next((r for r in DATA.get('reports',[]) if r.get('id')==rid),None)

def order_type_label(o):
    return {'vip':'👑 VIP','logo':'🎨 سفارش ساخت لوگو','customer':'✍️ ساخت عکس با پرامپت مشتری','normal':'🖼 ساخت عکس با پرامپت کانال'}.get(o.get('type'),'📦 سفارش')

def order_private_prompt(o):
    typ=o.get('type')
    if typ=='vip':
        return DATA.get('vip',{}).get(o.get('item'),{}).get('prompt','')
    if typ=='customer': return o.get('prompt_text','')
    if typ=='logo': return o.get('prompt_text','')
    sv=o.get('selected_variant') or []
    if len(sv)==2:
        return DATA.get('prompts',{}).get(sv[0],{}).get('variants',{}).get(sv[1],{}).get('prompt','')
    return ''

def private_info_text(o):
    lines=[f'📦 {order_type_label(o)}',f'🆔 {o.get("id","")}',f'👤 آیدی/شماره مشتری: {o.get("contact","")}']
    if o.get('model'): lines.append(f'🤖 مدل: {o.get("model")}')
    typ=o.get('type')
    if typ=='vip':
        v=DATA.get('vip',{}).get(o.get('item'),{})
        variants=v.get('variants',{})
        if variants:
            lines += ['', '📝 پرامپت‌های VIP:']
            for x in variants.values():
                lines += [f'🔘 {x.get("label","")}', x.get('prompt','')]
        elif v.get('prompt'):
            lines += ['', '📝 پرامپت VIP:', v.get('prompt','')]
    elif typ=='customer':
        if o.get('prompt_text'): lines += ['', '📝 پرامپت مشتری:', o.get('prompt_text','')]
    elif typ=='logo':
        if o.get('prompt_text'): lines += ['', '🎨 توضیحات/ایده لوگو:', o.get('prompt_text','')]
    else:
        prompt=order_private_prompt(o)
        if prompt: lines += ['', '📝 پرامپت سفارش:', prompt]
    return '\n'.join(lines)

def private_button_for_order(o):
    # Never expose customer contact information in the Orders channel.
    # This button opens the bot and reveals only the registered contact to users
    # who are allowed to view private customer data.
    if not BOT_USERNAME: return None
    return urlbtn('💬 پیام به مشتری',f'https://t.me/{BOT_USERNAME}?start=ordercontact_{o.get("id")}')

def private_prompt_button_for_order(o):
    # Customer-entered prompt is private too. Keep it out of the channel and
    # make it available through the bot to authorized admins.
    if not BOT_USERNAME or o.get('type')!='customer': return None
    return urlbtn('📝 پرامپت مشتری',f'https://t.me/{BOT_USERNAME}?start=orderprompt_{o.get("id")}')

def private_logo_button_for_order(o):
    # Customer's logo brief is private and is retrieved through the bot.
    if not BOT_USERNAME or o.get('type')!='logo': return None
    return urlbtn('🎨 توضیحات ساخت لوگو',f'https://t.me/{BOT_USERNAME}?start=orderlogo_{o.get("id")}')

def channel_order_keyboard(oid):
    o=find_order(oid)
    if not o: return kb([])
    rows=[]
    b=private_button_for_order(o) if o.get('type') in {'vip','customer','logo'} else None
    if b: rows.append([b])
    pb=private_prompt_button_for_order(o)
    if pb: rows.append([pb])
    lb=private_logo_button_for_order(o)
    if lb: rows.append([lb])
    return kb(rows)

def move_terminal_orders_out_of_active():
    active=[]; changed=False
    terminal={'🟢 تمام شد','❌ رد شده','🟢 رضایت دریافت شد','رضایت دریافت شد','❌ رد شد','رد شده'}
    completed=DATA.setdefault('completed_orders',[])
    existing={x.get('id') for x in completed}
    for o in DATA.get('orders',[]):
        if o.get('status') in terminal:
            if o.get('status') in {'🟢 رضایت دریافت شد','رضایت دریافت شد'}: o['status']='🟢 تمام شد'
            if o.get('id') not in existing: completed.append(deepcopy(o))
            changed=True
        else:
            active.append(o)
    if changed:
        DATA['orders']=active
    return changed

def cleanup_expired_order_messages():
    now=datetime.now()
    changed=False
    for o in DATA.get('orders',[]) + DATA.get('completed_orders',[]):
        if o.get('channel_expires_at') and o.get('channel_message_ids'):
            try: expired=datetime.fromisoformat(o['channel_expires_at']) <= now
            except Exception: expired=False
            if expired and not o.get('channel_deleted'):
                for mid in o.get('channel_message_ids',[]):
                    channel=o.get('channel_chat') or (ORDERS_CHAT if o.get('type')=='normal' else VIP_ORDERS_CHAT)
                    if channel and mid:
                        api('deleteMessage',{'chat_id':channel,'message_id':mid})
                o['channel_deleted']=True; changed=True
    if changed: save_data('Cleanup expired order messages')

if move_terminal_orders_out_of_active():
    save_data('Initial cleanup terminal orders')

# One-time repair requested for the known broken test order. Its counter is not decremented,
# so future orders keep their normal unique numbering.
if any(o.get('id')=='ORD-3' for o in DATA.get('orders',[])) or any(o.get('id')=='ORD-3' for o in DATA.get('completed_orders',[])):
    DATA['orders']=[o for o in DATA.get('orders',[]) if o.get('id')!='ORD-3']
    DATA['completed_orders']=[o for o in DATA.get('completed_orders',[]) if o.get('id')!='ORD-3']
    save_data('Remove broken ORD-3')

def repair_existing_order_messages():
    # Repair existing messages only when the stored record identifies the channel
    # that owns those message IDs. Legacy customer/logo/VIP records without a
    # channel_chat marker are left untouched so startup can never duplicate them
    # into a new channel. New records always store channel_chat.
    changed=False
    for o in DATA.get('orders',[]):
        channel=o.get('channel_chat')
        if not channel:
            if o.get('type')=='normal': channel=ORDERS_CHAT
            else: continue
        mids=[m for m in (o.get('channel_message_ids') or []) if m]
        if not mids or not channel: continue
        target=o.get('channel_action_message_id')
        if target and target in mids:
            if edit_order_message(channel,target,o): changed=True
            continue
        r=send(channel,view_order_text(o),order_action_kb(o.get('id')))
        if r.get('ok'):
            am=r.get('result',{}).get('message_id'); o['channel_action_message_id']=am; o['channel_message_ids']=mids+[am]; changed=True
    if changed: save_data('Repair legacy order channel messages')

def start_cleanup_thread():
    def loop():
        import time
        while True:
            try:
                if move_terminal_orders_out_of_active(): save_data('Cleanup terminal orders')
                cleanup_expired_order_messages()
            except Exception as e: print('Cleanup loop',e)
            time.sleep(3600)
    threading.Thread(target=loop,daemon=True).start()


def is_member(uid,ad):
    username=ad.get('username','').strip()
    if not username: return True
    r=api('getChatMember',{'chat_id':username,'user_id':uid})
    if not r.get('ok'): return False
    status=r.get('result',{}).get('status')
    return status in {'creator','administrator','member'} or (status=='restricted' and r.get('result',{}).get('is_member',False))

def require_membership(chat_id,uid):
    missing=[a for a in DATA.get('ads',[]) if a.get('required',True) and not is_member(uid,a)]
    if not missing: return True
    rows=[]
    for a in missing:
        url=a.get('url') or (f'https://t.me/{a.get("username","").lstrip("@")}')
        rows.append([urlbtn(f'📢 عضویت در {a.get("title","کانال")}',url)])
    rows.append([btn('✅ بررسی عضویت','check_membership')])
    send(chat_id,'🔒 برای استفاده از ربات، ابتدا در کانال‌های تبلیغاتی زیر عضو شو و بعد «بررسی عضویت» را بزن.',kb(rows))
    return False

# ---------- training ----------
def training_user(cid):
    trainings=DATA.get('trainings',[])
    if trainings:
        rows=[]
        for i,t in enumerate(trainings):
            if t.get('url'): rows.append([urlbtn(t.get('title') or f'آموزش {i+1}',t['url'])])
            else: rows.append([btn(t.get('title') or f'آموزش {i+1}',f'training_view|{i}')])
        send(cid,'📚 آموزش‌های اولیه',kb(rows)); return
    send(cid,'📚 آموزش‌های اولیه',kb([[urlbtn('📖 مشاهده آموزش اولیه',TRAINING_POST_URL)]]))

def training_admin(cid):
    rows=[[btn('➕ افزودن آموزش','training_add')]]
    for i,t in enumerate(DATA.get('trainings',[])):
        rows.append([btn(f'✏️ {i+1}. {t.get("title","")}',f'training_edit|{i}'),btn('🗑 حذف',f'training_delete|{i}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'📚 آموزش‌ها',kb(rows))

# ---------- ads / required channels ----------
def ads_admin(cid):
    rows=[[btn('➕ افزودن کانال تبلیغاتی','ad_add')]]
    for i,a in enumerate(DATA.get('ads',[])):
        rows.append([btn(f'✏️ {i+1}. {a.get("title","")}',f'ad_edit|{i}'),btn('🗑 حذف',f'ad_delete|{i}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'📣 تبلیغات\n\nکانال‌های این بخش شرط عضویت اجباری کاربران هستند.',kb(rows))

# ---------- admins ----------
def admins_admin(cid):
    rows=[]
    if is_owner(cid): rows.append([btn('➕ افزودن ادمین','admin_add')])
    for aid,a in DATA.get('admins',{}).items():
        rows.append([btn(f'⚙️ {aid} | {a.get("name","")} | {"ادمین پلاس" if a.get("role","normal")=="plus" else "ادمین عادی"}',f'admin_perms|{aid}'),btn('🗑 حذف',f'admin_delete|{aid}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'👥 ادمین‌ها',kb(rows))

def admin_permissions_page(cid,aid):
    a=DATA.get('admins',{}).get(str(aid))
    if not a: return send(cid,'❌ ادمین پیدا نشد.')
    perms=a.setdefault('permissions',[])
    if isinstance(perms,dict): perms=[k for k,v in perms.items() if v]; a['permissions']=perms
    role=a.get('role','normal')
    rows=[[btn(('🟣 ادمین پلاس' if role=='plus' else '⚪ ادمین عادی'),f'admin_role_toggle|{aid}')]]
    perm_labels={'prompts':'پرامپت‌ها','prices':'قیمت‌ها','vip':'VIP','orders':'سفارشات','vip_orders':'سفارشات VIP','logo_orders':'سفارشات لوگو','reports':'گزارش مشکلات','ads':'تبلیغات','training':'آموزش','admins':'ادمین‌ها','stats':'آمار','notifications':'اعلان‌ها','settings':'تنظیمات','customer_data':'اطلاعات خصوصی مشتری'}
    rows += [[btn(('🟢 ' if p in perms else '🔴 ')+perm_labels.get(p,p),f'admin_perm_toggle|{aid}|{p}')] for p in PERMISSIONS]
    rows.append([btn('🔙 ادمین‌ها','adm_admins')]); send(cid,f'⚙️ دسترسی‌های ادمین {aid}',kb(rows))

# ---------- cards ----------
def cards_admin(cid):
    lines=['💳 کارت‌ها','']
    for i,c in enumerate(DATA.get('cards',[]),1): lines.append(f'{i}. {c.get("bank","")} — {c.get("number","")} — به نام {c.get("name","")}')
    if len(lines)==2: lines.append('کارت ثبت نشده.')
    rows=[[btn('➕ افزودن کارت','card_add')]]
    for i,_ in enumerate(DATA.get('cards',[])): rows.append([btn(f'✏️ ویرایش کارت {i+1}',f'card_edit|{i}'),btn('🗑 حذف',f'card_delete|{i}')])
    rows.append([btn('🔙 تنظیمات','adm_settings')]); send(cid,'\n'.join(lines),kb(rows))

def card_kb(flow_id=None):
    rows=[]
    for i,c in enumerate(DATA.get('cards',[])):
        cb=f'card|{i}|{flow_id}' if flow_id else f'card|{i}'
        rows.append([btn(f'💳 {c.get("bank","")} | {c.get("number","")}',cb)])
    cancel=f'ord_cancel|{flow_id}' if flow_id else 'ord_cancel'
    rows.append([btn('❌ لغو',cancel)]); return kb(rows)

# ---------- prices / vip ----------
def prices_admin(cid):
    prices=DATA.setdefault('prices',{})
    rows=[
        [btn(f'🖼 ساخت عکس با پرامپت کانال | {money(prices.get("channel_prompt",0))} تومان','price_channel')],
        [btn(f'✍️ ساخت عکس با پرامپت مشتری | {money(prices.get("customer_prompt",0))} تومان','price_customer')],
        [btn(f'🎨 سفارش ساخت لوگو | {money(prices.get("logo",0))} تومان','price_logo')],
        [btn('👑 قیمت‌های VIP','price_vip')]
    ]
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'💰 قیمت‌ها\n\nیک بخش را انتخاب کن:',kb(rows))

def vip_prices_admin(cid):
    rows=[]
    for k,v in DATA.get('vip',{}).items():
        rows.append([btn(f'{vip_label(k,v)} | {money(v.get("price",0))} تومان',f'setprice|{k}')])
    rows.append([btn('🔙 قیمت‌ها','adm_prices')]); send(cid,'👑 قیمت VIP\n\nبرای هر VIP جداگانه قیمت تعیین کن:',kb(rows))

def vip_admin(cid):
    rows=[[btn('➕ افزودن VIP','vip_add')]]
    for k,v in DATA.get('vip',{}).items(): rows.append([btn(f'✏️ {vip_label(k,v)}',f'vip_edit|{k}'),btn('🗑 حذف',f'vip_delete|{k}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'👑 VIP',kb(rows))

def vip_add_buttons():
    return kb([[btn('➕ اضافه کردن دکمه','vip_add_button')],[btn('✅ ثبت VIP','vip_add_done')],[btn('❌ لغو','ord_cancel')]])

def vip_user(cid):
    rows=[[btn(f'{vip_label(k,v)} — {money(v.get("price",0))} تومان',f'vip_buy|{k}')] for k,v in DATA.get('vip',{}).items()]
    send(cid,'👑 پرامپت‌های VIP\n\nیک مورد را انتخاب کن:',kb(rows) if rows else None)

# ---------- reports ----------
def start_report(uid):
    STATES[uid]={'type':'report','step':'description'}
    send(uid,'سلام 👋\nمشکل را کامل توضیح بده.\n\n/cancel برای لغو')

def save_report(uid,s):
    rid=next_id('report')
    r={'id':rid,'user_id':uid,'description':s['description'],'contact':s['contact'],'status':'🟡 در حال بررسی','created_at':datetime.now().strftime('%Y-%m-%d %H:%M'),'result':''}
    DATA['reports'].append(r); save_data(f'New report {rid}')
    if ORDERS_CHAT:
        report_text=(f'⚠️ گزارش مشکل جدید {rid}\n\n📝 مشکل:\n{s["description"]}\n\n📱 تماس: {s["contact"]}\n🕐 {r["created_at"]}\n📌 وضعیت: {r["status"]}')
        if s.get('evidence'):
            send_photo(ORDERS_CHAT,s['evidence'],report_text,kb([[btn('💬 پاسخ به کاربر',f'report_reply|{rid}')]]))
        else:
            send(ORDERS_CHAT,report_text,kb([[btn('💬 پاسخ به کاربر',f'report_reply|{rid}')]]))
    STATES.pop(uid,None); send(uid,DATA['messages']['report_created'],main_menu(uid))

def reports_admin(cid):
    rows=[[btn(f'{r.get("id")} | {r.get("status","در حال بررسی")}',f'report_view|{r.get("id")}')] for r in DATA.get('reports',[])[-30:]]
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'⚠️ گزارش مشکلات',kb(rows))

def report_detail(cid,rid):
    r=find_report(rid)
    if not r: return send(cid,'❌ گزارش پیدا نشد.')
    text=f'⚠️ گزارش {rid}\n\n📝 مشکل:\n{r.get("description","")}\n\n📱 تماس: {r.get("contact","")}\n🕐 تاریخ: {r.get("created_at","")}\n📌 وضعیت: {r.get("status","")}'
    if r.get('result'): text+=f'\n\n💬 آخرین پاسخ:\n{r["result"]}'
    send(cid,text,kb([[btn('💬 پاسخ به کاربر',f'report_reply|{rid}')],[btn('🟢 حل شد',f'report_resolve|{rid}')],[btn('🔙 گزارش‌ها','adm_reports')]]))

# ---------- customer testimonials ----------
def testimonial_admin(cid):
    rows=[[btn('👑 VIP','testimonial_type|vip'),btn('🎨 لوگو','testimonial_type|logo')],
          [btn('✍️ ساخت عکس با پرامپت مشتری','testimonial_type|customer')],
          [btn('🖼 ساخت عکس با پرامپت کانال','testimonial_type|normal')],
          [btn('🔙 مدیریت','admin_menu')]]
    send(cid,'⭐ ثبت رضایت\n\nبرای کدام نوع سفارش است؟',kb(rows))

def resolve_testimonial_order(kind,number):
    if not str(number).isdigit(): return None
    n=int(number)
    if kind=='vip': oid=f'VIP-{n}'
    elif kind=='normal': oid=f'ORD-{n}'
    elif kind=='customer': oid=f'CUST-{n}'
    else: oid=f'LOGO-{n}'
    return find_order(oid) or next((o for o in DATA.get('completed_orders',[]) if o.get('id')==oid),None)

def publish_testimonial_album(uid,kind,number,photos):
    if not TESTIMONIALS_CHAT:
        send(uid,'❌ کانال رضایت مشتری تنظیم نشده است. متغیر TESTIMONIALS_CHAT را تنظیم کن.')
        return
    labels={'vip':'👑 VIP','logo':'🎨 سفارش ساخت لوگو','customer':'✍️ ساخت عکس با پرامپت مشتری','normal':'🖼 ساخت عکس با پرامپت کانال'}
    prefix={'vip':'VIP','logo':'LOGO','customer':'CUST','normal':'ORD'}[kind]
    oid=f'{prefix}-{int(number)}'
    caption=f'⭐ رضایت مشتری\n\n📦 {labels[kind]}\n🆔 {oid}'
    media=[]
    for i,pid in enumerate(photos):
        x={'type':'photo','media':pid}
        if i==0: x['caption']=caption
        media.append(x)
    r=api('sendMediaGroup',{'chat_id':TESTIMONIALS_CHAT,'media':media})
    if not r.get('ok'):
        # Single photo fallback, still keeps caption.
        if len(photos)==1:
            r2=send_photo(TESTIMONIALS_CHAT,photos[0],caption)
            if not r2.get('ok'): send(uid,'❌ انتشار رضایت در کانال ناموفق بود.'); return
        else:
            send(uid,'❌ ارسال آلبوم رضایت ناموفق بود.'); return
    if kind=='vip':
        ref=resolve_testimonial_order(kind,number)
        contact=(ref or {}).get('contact','') if ref else ''
        if contact:
            DATA.setdefault('vip_customer_refs',{})[oid]={'vip_label':f'VIP{int(number)}','contact':contact,'user_id':(ref or {}).get('user_id')}
            save_data(f'VIP testimonial ref {oid}')
            # Telegram cannot make a channel button visible only to one person. Use a bot deep-link;
            # the bot itself checks that the opener is the owner before revealing the contact.
            if BOT_USERNAME:
                send(TESTIMONIALS_CHAT,'🔐 اطلاعات مشتری VIP',kb([[urlbtn('👤 آیدی مشتری',f'https://t.me/{BOT_USERNAME}?start=orderinfo_{oid}')]]))
    send(uid,f'✅ رضایت {oid} در کانال ثبت شد.')

def testimonial_start(uid):
    STATES[uid]={'type':'testimonial','step':'kind','photos':[],'last_message_id':None}
    testimonial_admin(uid)

# ---------- orders ----------
def start_order(uid,customer=False,logo=False):
    typ='logo' if logo else ('customer' if customer else 'normal')
    STATES[uid]={'type':typ,'step':'what','items':[],'contact':'','receipt':None,'ref_photo':None,'flow_id':new_order_flow_id()}
    if logo: send(uid,'🎨 توضیحات و ایده ساخت لوگو را کامل بفرست.\n\n/cancel برای لغو')
    elif customer: send(uid,'✍️ پرامپت خودت را کامل بفرست.\n\n/cancel برای لغو')
    else: send(uid,'🛒 شماره یک پرامپت را بفرست. مثال: 4\n\nهر سفارش فقط یک پرامپت دارد.\n/cancel برای لغو')

def order_text(uid,text):
    s=STATES[uid]
    if s['step']=='what':
        if s['type']=='logo':
            s['prompt_text']=text; s['price']=price_for('logo'); s['step']='confirm_price'
            send(uid,f'💰 قیمت سفارش لوگو: {money(s["price"])} تومان\n\nتأیید می‌کنی؟',kb([[btn('✅ تأیید',f'ord_price_ok|{s["flow_id"]}'),btn('❌ لغو',f'ord_cancel|{s["flow_id"]}')]])); return
        if s['type']=='customer':
            s['prompt_text']=text; s['price']=price_for('customer_prompt'); s['step']='confirm_price'
            send(uid,f'💰 قیمت سفارش: {money(s["price"])} تومان\n\nتأیید می‌کنی؟',kb([[btn('✅ تأیید',f'ord_price_ok|{s["flow_id"]}'),btn('❌ لغو',f'ord_cancel|{s["flow_id"]}')]])); return
        # Exactly one prompt per order. Multiple prompt numbers are intentionally disabled.
        if not re.fullmatch(r'\d+', text):
            return send(uid,'❌ در هر سفارش فقط یک شماره پرامپت وارد کن.\nمثال: 4')
        pid=normalize_pid(text)
        if pid not in DATA.get('prompts',{}): return send(uid,'❌ شماره پرامپت پیدا نشد.')
        s['prompt_ids']=[pid]; rows=[]
        for k,v in DATA['prompts'][pid].get('variants',{}).items():
            rows.append([btn(f'{pid[1:]} | {v.get("label",k)}',f'ord_variant|{pid}|{k}|{s["flow_id"]}')])
        rows.append([btn('❌ لغو',f'ord_cancel|{s["flow_id"]}')]); s['step']='variants'
        send(uid,'🤖 مدل هوش مصنوعی موردنظر را انتخاب کن:',kb(rows)); return
    if s['step']=='contact':
        s['contact']=text; s['step']='card'; send(uid,'💳 کارت مقصد را انتخاب کن:',card_kb(s.get('flow_id'))); return

def order_price_confirm(uid):
    s=STATES.get(uid)
    if not s or s.get('step')!='confirm_price': return
    s['step']='contact'; send(uid,'📱 آیدی تلگرام یا شماره خود را قرار دهید')

def finish_order(uid):
    s=STATES.get(uid)
    if not s: return
    # Finalize only from a terminal payment/reference step.  This makes the
    # function idempotent against delayed/duplicate updates.
    typ=s.get('type')
    if typ not in {'normal','customer','logo'}: return
    oid=next_id('customer' if typ=='customer' else 'logo' if typ=='logo' else 'order')
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    if typ=='customer':
        item='سفارش ساخت عکس با پرامپت مشتری'; model=''
    elif typ=='logo':
        item='سفارش ساخت لوگو'; model=''
    else:
        pid=s.get('prompt_ids',[''])[0]; vk=(s.get('selected_variant') or (pid,''))[1]
        variant=DATA.get('prompts',{}).get(pid,{}).get('variants',{}).get(vk,{})
        model=variant.get('label',vk); item=f'{pid[1:]}'
    o={'id':oid,'type':typ,'user_id':uid,'contact':s.get('contact',''),'item':item,'model':model,
       'prompt_text':s.get('prompt_text',''),'prompt_id':(s.get('prompt_ids') or [''])[0],
       'selected_variant':s.get('selected_variant'),'price':s.get('price',0),'card':s.get('card'),
       'payer_name':s.get('payer_name',''),'ref_photo':s.get('ref_photo'),'status':'🟠 در حال تأیید',
       'payment':'pending','created_at':now,'channel_chat':(ORDERS_CHAT if typ=='normal' else VIP_ORDERS_CHAT)}
    DATA['orders'].append(o); save_data(f'New order {oid}')

    # Only channel-prompt orders belong in the normal Orders channel.
    # Customer-prompt, logo and VIP orders are always routed to VIP_ORDERS_CHAT.
    target_chat=ORDERS_CHAT if typ=='normal' else VIP_ORDERS_CHAT
    if target_chat:
        details=f'🆔 {oid}\n🛒 سفارش: {item}\n'
        if model: details+=f'🤖 مدل: {model}\n'
        details+=f'💰 قیمت: {money(o["price"])} تومان\n'
        if typ=='normal': details+=f'📱 تماس: {o.get("contact", "ثبت نشده")}\n'
        if o.get('type') not in {'vip','customer','logo'} and s.get('payer_name'):
            details+=f'👤 واریزکننده: {s.get("payer_name","")}\n'
        details+=f'🕐 {now}\n📌 وضعیت: {o["status"]}'
        ids=[]
        if s.get('ref_photo') and s.get('receipt'):
            media=[{'type':'photo','media':s['receipt']},{'type':'photo','media':s['ref_photo']}]
            r=api('sendMediaGroup',{'chat_id':target_chat,'media':media})
            if r.get('ok'): ids=[x.get('message_id') for x in r.get('result',[]) if x.get('message_id')]
        elif s.get('receipt'):
            r=send_photo(target_chat,s['receipt'],'')
            if r.get('ok'): ids=[r.get('result',{}).get('message_id')]
        if ids:
            r2=send(target_chat,details,order_action_kb(oid))
            if r2.get('ok'):
                o['channel_action_message_id']=r2.get('result',{}).get('message_id'); ids.append(o['channel_action_message_id'])
            o['channel_message_ids']=ids
        else:
            r=send(target_chat,details,order_action_kb(oid))
            if r.get('ok'):
                mid=r.get('result',{}).get('message_id'); o['channel_action_message_id']=mid; o['channel_message_ids']=[mid]
        if typ in {'customer','logo'}:
            o['channel_expires_at']=(datetime.now()+timedelta(days=ORDER_RETENTION_DAYS)).isoformat()
        save_data(f'Channel order {oid}')
    clear_order_state(uid); send(uid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid))

def order_action_kb(oid):
    o=find_order(oid)
    if not o: return kb([])
    st=o.get('status',''); rows=[]
    if o.get('payment')=='pending' and st not in {'❌ رد شده','🟢 تمام شد'}:
        rows.append([btn('✅ تأیید پرداخت',f'pay_ok|{oid}'),btn('❌ رد پرداخت',f'pay_bad|{oid}')])
    elif o.get('payment')=='approved' and st=='🟡 در حال انجام':
        rows.append([btn('🔵 تحویل شد',f'status|{oid}|delivered')])
        if o.get('type')=='customer':
            rows.append([btn('📝 پرامپت مشتری',f'order_prompt|{oid}')])
        elif o.get('type')=='logo':
            rows.append([btn('🎨 خواسته مشتری',f'order_request|{oid}')])
    elif st=='🔵 تحویل داده شد':
        rows.append([btn('🟢 رضایت دریافت شد',f'status|{oid}|satisfied')])
    if o.get('type') in {'vip','customer','logo'} and st not in {'🟢 تمام شد','❌ رد شده'}:
        rows.append([btn('💬 پیام به مشتری',f'order_contact|{oid}')])
    return kb(rows)

def orders_admin(cid, vip_only=False, order_type=None):
    if order_type:
        items=[o for o in DATA.get('orders',[]) if o.get('type')==order_type]
    elif vip_only:
        items=[o for o in DATA.get('orders',[]) if o.get('type') in {'vip','customer','logo'}]
    else:
        items=[o for o in DATA.get('orders',[]) if o.get('type')=='normal']
    rows=[[btn(f'{o.get("id")} | {o.get("status")}',f'view_order|{o.get("id")}')] for o in items[-30:]]
    rows.append([btn('🔙 مدیریت','admin_menu')])
    title='🎨 سفارشات لوگو' if order_type=='logo' else ('📦 سفارشات VIP' if vip_only else '📦 سفارشات')
    send(cid,title,kb(rows))

# ---------- post builder ----------
def new_post_flow_id():
    return uuid.uuid4().hex[:12]

def new_post_stage_token():
    return uuid.uuid4().hex[:10]

def post_input_fingerprint(text, photos):
    photo_id = photos[-1].get('file_id','') if photos else ''
    return f"{text}|{photo_id}"

def ensure_post_state(uid):
    """Return the live post-builder state; recover only when memory lost it."""
    s=POST_STATES.get(uid)
    if isinstance(s,dict) and s.get('flow_id'):
        return s
    saved=DATA.get('active_post_states',{}).get(str(uid))
    if isinstance(saved,dict) and saved.get('step') in {'photos','name','number','suitable','for','buttons','button_label','button_prompt'}:
        restored=deepcopy(saved)
        restored.setdefault('flow_id',new_post_flow_id())
        POST_STATES[uid]=restored
        return restored
    return None

def post_start(uid):
    # A new post is the only operation allowed to replace an existing post flow.
    # From this point on, every post callback is bound to this exact flow_id.
    reset_admin_flow(uid)
    flow_id=new_post_flow_id()
    POST_STATES[uid]={
        'type':'post',
        'step':'photos',
        'photos':[],
        'buttons':[],
        'last_message_id':None,
        'last_user_message_id':0,
        'processed_message_ids':[],
        'flow_id':flow_id,
        'stage_token':new_post_stage_token(),
        'revision':0,
        'mode':'create',
    }
    persist_conversation_states_async()
    send(uid,'🖼 عکس پست را ارسال کن. می‌توانی چند عکس پشت سر هم بفرستی؛ وقتی تمام شد روی «✅ اتمام عکس‌ها» بزن.',kb([[btn('✅ اتمام عکس‌ها',f'post_photos_done|{flow_id}|photos|{POST_STATES[uid]["stage_token"]}')],[btn('❌ لغو',f'post_cancel|{flow_id}')]]))

def post_state_matches(uid, flow_id):
    """Return the active post flow only when its flow id matches.

    The callback's expected stage is deliberately not used as an authority.
    Telegram can deliver a delayed/duplicate click from an older keyboard; the
    live state is the source of truth and the handlers below make those clicks
    idempotent instead of falsely reporting a broken flow.
    """
    s=ensure_post_state(uid)
    if not s or not flow_id or s.get('flow_id')!=flow_id:
        return None
    return s

def post_step_prompt(s):
    prompts={
        'photos':'🖼 هنوز در مرحله عکس‌ها هستیم. یک عکس بفرست و بعد «✅ اتمام عکس‌ها» را بزن.',
        'name':'📝 نام پست را بفرست.',
        'number':'🔢 شماره پرامپت را بفرست.',
        'suitable':'🎯 مناسب برای چه کاری؟',
        'for':'📌 در بخش «برای» چه بنویسم؟',
        'buttons':'از دکمه‌های فعلی همین مرحله استفاده کن.',
        'button_label':'🔘 نام دکمه را بفرست. مثال: Gemini',
        'button_prompt':'✍️ متن کامل پرامپت این دکمه را بفرست.',
    }
    return prompts.get(s.get('step'),'')

def post_advance(s, step):
    s['step']=step
    s['stage_token']=new_post_stage_token()
    s['revision']=int(s.get('revision',0))+1
    s.pop('last_input_fingerprint',None)
    s.pop('last_input_at',None)
    persist_conversation_states_async()

def post_preview(s):
    lines=[f'🔥 پرامپت شماره {s["number"]} | {s["name"]}']
    if s.get('suitable'): lines+=['',f'🎯 مناسب: {s["suitable"]}']
    if s.get('for_what'): lines+=['',f'📌 برای: {s["for_what"]}']
    lines+=['','✨ برای نتیجه بهتر:','یک عکس واضح و باکیفیت از خودت به مدل بده.','','⚠️ توجه:','نتیجه نهایی ممکنه بسته به مدل تصویرساز و عکس مرجع کمی متفاوت باشه.']
    return '\n'.join(lines)

def post_buttons(flow_id=None, stage_token=None):
    suffix=f'|{flow_id}' if flow_id else ''
    tok=f'|{stage_token}' if stage_token else ''
    return kb([[btn('➕ اضافه کردن دکمه',f'post_add{suffix}|buttons{tok}')],[btn('✅ انتشار',f'post_publish{suffix}|buttons{tok}'),btn('❌ لغو',f'post_cancel{suffix}{tok}')]])

def publish_post(uid):
    s=POST_STATES.get(uid)
    if not s or not s.get('photos') or not s.get('buttons'): return send(uid,'⚠️ حداقل یک عکس و یک دکمه لازم است.')
    pid=f'p{s["number"]}'
    existing=DATA.get('prompts',{}).get(pid,{})
    DATA['prompts'][pid]={
        'title':s['name'],
        'suitable':s.get('suitable',''),
        'for':s.get('for_what',''),
        'price':existing.get('price',0),
        'variants':{b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']},
        'photos':list(s.get('photos',[])),
        'channel_message_ids':[],
        'archive_message_ids':[],
    }
    save_data(f'Publish {pid}')
    # Prefer a Telegram deep-link for channel buttons. Tapping it opens the bot and
    # the /start handler delivers the exact selected variant. If the bot username
    # cannot be resolved, keep the callback fallback so publishing still works.
    bot_name=BOT_USERNAME
    if not bot_name:
        me=api('getMe')
        bot_name=(me.get('result',{}).get('username') or '').strip().lstrip('@') if me.get('ok') else ''
    if bot_name:
        links=kb([[urlbtn(b['label'],f'https://t.me/{bot_name}?start={pid}_{b["key"]}')] for b in s['buttons']])
    else:
        links=kb([[btn(b['label'],f'getprompt|{pid}|{b["key"]}')] for b in s['buttons']])

    # Telegram does not allow inline keyboards on sendMediaGroup albums. For a single
    # photo we attach the buttons directly; for multiple photos we publish the album
    # first and then a dedicated text message containing the buttons.
    channel_ids=[]
    if len(s['photos'])==1:
        result=send_photo(PROMPTINO_CHAT,s['photos'][0],post_preview(s),links)
        if result.get('ok'): channel_ids=[result.get('result',{}).get('message_id')]
    else:
        media=[{'type':'photo','media':p} for p in s['photos']]
        result=api('sendMediaGroup',{'chat_id':PROMPTINO_CHAT,'media':media})
        if result.get('ok'):
            channel_ids=[x.get('message_id') for x in result.get('result',[]) if x.get('message_id')]
            result=send(PROMPTINO_CHAT,post_preview(s),links)
            if result.get('ok'): channel_ids.append(result.get('result',{}).get('message_id'))
    if not result.get('ok'):
        send(uid,'❌ انتشار در کانال ناموفق بود. ربات باید در کانال ادمین باشد و Chat ID کانال درست باشد.')
        return

    # Archive layout: publish the exact same album/post appearance first, then
    # keep every variant in a simple two-message sequence:
    # 1) button name
    # 2) the full prompt in the next message
    # This keeps the archive easy to read and avoids making the prompt itself
    # part of the same message as its title.
    if ARCHIVE_CHAT:
        media=[]
        for i,photo_id in enumerate(s['photos']):
            item={'type':'photo','media':photo_id}
            if i==0:
                item['caption']=post_preview(s)
            media.append(item)
        album_result=api('sendMediaGroup',{'chat_id':ARCHIVE_CHAT,'media':media})
        if not album_result.get('ok'):
            print('Archive album failed:',album_result)
        else:
            archive_ids=[x.get('message_id') for x in album_result.get('result',[]) if x.get('message_id')]
            for b in s['buttons']:
                r1=send(ARCHIVE_CHAT,f'🔘 {b["label"]}')
                if r1.get('ok'): archive_ids.append(r1.get('result',{}).get('message_id'))
                prompt=b.get('prompt','')
                if prompt and len(prompt)<=256:
                    r2=send(ARCHIVE_CHAT,prompt,kb([[{'text':'📋 کپی پرامپت','copy_text':{'text':prompt}}]]))
                else:
                    r2=send(ARCHIVE_CHAT,prompt)
                if r2.get('ok'): archive_ids.append(r2.get('result',{}).get('message_id'))
    else:
        archive_ids=[]
    DATA['prompts'][pid]['channel_message_ids']=[x for x in channel_ids if x]
    DATA['prompts'][pid]['archive_message_ids']=[x for x in archive_ids if x]
    save_data(f'Publish metadata {pid}')
    POST_STATES.pop(uid,None); persist_conversation_states_async(); send(uid,'🎉 پست با موفقیت منتشر شد.',admin_menu(uid))

# ---------- prompt editor ----------
def prompt_edit_menu(s):
    rows=[]
    buttons=s.get('buttons',[])
    if buttons:
        for b in buttons:
            label=b.get('label','') or b.get('key','')
            rows.append([
                btn(f'✏️ {label}',f'post_edit_variant|{s["flow_id"]}|{b["key"]}|{s.get("stage_token","")}'),
                btn('🗑 حذف',f'post_delete_variant|{s["flow_id"]}|{b["key"]}|{s.get("stage_token","")}')
            ])
    else:
        rows.append([btn('ℹ️ هنوز دکمه‌ای وجود ندارد','post_edit_noop')])
    rows.append([btn('➕ اضافه کردن دکمه',f'post_edit_add|{s["flow_id"]}|{s.get("stage_token","")}')])
    rows.append([btn('✅ انتشار و جایگزینی',f'post_edit_publish|{s["flow_id"]}|{s.get("stage_token","")}'),btn('❌ لغو',f'post_edit_cancel|{s["flow_id"]}|{s.get("stage_token","")}')])
    return kb(rows)

def start_prompt_editor(uid,pid):
    p=DATA.get('prompts',{}).get(pid)
    if not p: return False
    reset_admin_flow(uid)
    flow_id=new_post_flow_id()
    buttons=[{'key':k,'label':v.get('label',k),'prompt':v.get('prompt','')} for k,v in p.get('variants',{}).items()]
    POST_STATES[uid]={
        'type':'post','mode':'edit','step':'buttons','pid':pid,'flow_id':flow_id,
        'stage_token':new_post_stage_token(),'revision':0,'photos':list(p.get('photos',[])),
        'buttons':buttons,'name':p.get('title',pid),'suitable':p.get('suitable',''),'for_what':p.get('for',''),
        'number':int(re.sub(r'\D','',pid) or 0),'processed_message_ids':[],
    }
    persist_conversation_states_async()
    if buttons:
        intro='✏️ ویرایش پست\n\nدکمه‌های فعلی پست را پایین می‌بینی. برای هر دکمه می‌توانی «ویرایش» یا «حذف» کنی. با «اضافه کردن دکمه» هم دکمه جدید بساز.'
    else:
        intro='✏️ ویرایش پست\n\nاین پست فعلاً دکمه‌ای ندارد. می‌توانی دکمه جدید اضافه کنی.'
    send(uid,intro,prompt_edit_menu(POST_STATES[uid]))
    return True

def delete_messages(chat_id, message_ids):
    ok=True
    for mid in message_ids or []:
        if not api('deleteMessage',{'chat_id':chat_id,'message_id':mid}).get('ok'): ok=False
    return ok

def publish_edited_post(uid):
    s=POST_STATES.get(uid)
    if not s or s.get('mode')!='edit': return
    pid=s['pid']; p=DATA.get('prompts',{}).get(pid)
    if not p: return send(uid,'❌ پرامپت پیدا نشد.')
    if not s.get('buttons'): return send(uid,'⚠️ حداقل یک دکمه لازم است.')
    if not s.get('photos'):
        # Legacy posts created before publication metadata was introduced cannot be
        # safely deleted/replaced because their original Telegram media ids were
        # never stored. Update the data only instead of creating a duplicate post.
        p.update({'title':s.get('name',p.get('title','')),'suitable':s.get('suitable',p.get('suitable','')),'for':s.get('for_what',p.get('for','')),'variants':{b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']}})
        save_data(f'Edit data {pid}')
        POST_STATES.pop(uid,None); persist_conversation_states_async()
        send(uid,'✅ اطلاعات پرامپت ویرایش شد. این پست قدیمی اطلاعات رسانه‌ای لازم برای جایگزینی خودکار در کانال را ندارد؛ برای جلوگیری از ایجاد پست تکراری، پست جدیدی ارسال نشد.',admin_menu(uid))
        return
    old_channel=list(p.get('channel_message_ids',[])); old_archive=list(p.get('archive_message_ids',[]))
    # Replace the stored prompt definition first, but preserve the price.
    p.update({'title':s.get('name',p.get('title','')),'suitable':s.get('suitable',p.get('suitable','')),'for':s.get('for_what',p.get('for','')),'photos':list(s.get('photos',p.get('photos',[]))),'variants':{b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']}})
    save_data(f'Edit prepare {pid}')
    # Remove the old published copies only when their exact Telegram message ids
    # are known. This prevents duplicate channel posts for new/edited posts.
    if old_channel: delete_messages(PROMPTINO_CHAT,old_channel)
    if old_archive and ARCHIVE_CHAT: delete_messages(ARCHIVE_CHAT,old_archive)
    # Reuse the same publishing logic without creating a second prompt record.
    bot_name=BOT_USERNAME
    if not bot_name:
        me=api('getMe'); bot_name=(me.get('result',{}).get('username') or '').strip().lstrip('@') if me.get('ok') else ''
    links=kb([[urlbtn(b['label'],f'https://t.me/{bot_name}?start={pid}_{b["key"]}')] for b in s['buttons']]) if bot_name else [[btn(b['label'],f'getprompt|{pid}|{b["key"]}')] for b in s['buttons']]
    if len(s.get('photos',[]))==1:
        r=send_photo(PROMPTINO_CHAT,s['photos'][0],post_preview(s),links); channel_ids=[r.get('result',{}).get('message_id')] if r.get('ok') else []
    else:
        r=api('sendMediaGroup',{'chat_id':PROMPTINO_CHAT,'media':[{'type':'photo','media':x} for x in s.get('photos',[])]}); channel_ids=[x.get('message_id') for x in r.get('result',[]) if x.get('message_id')] if r.get('ok') else []
        if r.get('ok'):
            r2=send(PROMPTINO_CHAT,post_preview(s),links);
            if r2.get('ok'): channel_ids.append(r2.get('result',{}).get('message_id'))
    if not channel_ids:
        return send(uid,'❌ انتشار نسخه ویرایش‌شده ناموفق بود.')
    archive_ids=[]
    if ARCHIVE_CHAT:
        r=api('sendMediaGroup',{'chat_id':ARCHIVE_CHAT,'media':[{'type':'photo','media':x,'caption':post_preview(s) if i==0 else ''} for i,x in enumerate(s.get('photos',[]))]})
        if r.get('ok'):
            archive_ids=[x.get('message_id') for x in r.get('result',[]) if x.get('message_id')]
            for b in s['buttons']:
                r1=send(ARCHIVE_CHAT,f'🔘 {b["label"]}');
                if r1.get('ok'): archive_ids.append(r1.get('result',{}).get('message_id'))
                r2=send(ARCHIVE_CHAT,b['prompt']);
                if r2.get('ok'): archive_ids.append(r2.get('result',{}).get('message_id'))
    p['channel_message_ids']=channel_ids; p['archive_message_ids']=archive_ids
    save_data(f'Edit publish {pid}')
    POST_STATES.pop(uid,None); persist_conversation_states_async(); send(uid,'✅ پست با موفقیت ویرایش و جایگزین شد.',admin_menu(uid))

# ---------- admin pages ----------
def prompts_admin(cid):
    send(cid,'📝 پرامپت‌ها',kb([[btn('➕ افزودن پست','post_start')],[btn('✏️ ویرایش عنوان','prompt_edit'),btn('🔄 تغییر پرامپت','prompt_text')],[btn('🗑 حذف','prompt_delete')],[btn('🔙 مدیریت','admin_menu')]]))

def notifications_admin(cid):
    n=DATA['settings']['notifications']; labels={'new_order':'سفارش جدید','payment':'پرداخت','report':'گزارش مشکل','vip_order':'سفارش VIP'}
    send(cid,'🔔 تنظیمات اعلان‌ها',kb([[btn(('🟢 ' if n.get(k) else '🔴 ')+label,f'toggle_notify|{k}')] for k,label in labels.items()]+[[btn('🔙 مدیریت','admin_menu')]]))

def settings_admin(cid): send(cid,'⚙️ تنظیمات',kb([[btn('💳 کارت‌ها','adm_cards')],[btn('🔙 مدیریت','admin_menu')]]))

# ---------- STATE ROUTER ----------
def handle_state_message(uid,cid,m):
    """Consume exactly one Telegram message for the current state.

    A state transition is allowed only from its expected step.  The Telegram
    message_id is also remembered so the same message cannot advance a flow
    twice even if the webhook layer ever delivers it again.
    """
    if uid not in STATES: return False
    s=STATES[uid]; ensure_order_flow_state(s); text=(m.get('text') or '').strip(); photo=m.get('photo') or []
    message_id=m.get('message_id')
    if message_id is not None and s.get('last_message_id')==message_id:
        return True
    if text=='/cancel':
        clear_order_state(uid)
        if s.get('type') not in ORDER_STATE_TYPES:
            STATES.pop(uid,None)
        send(cid,'❌ عملیات لغو شد.',main_menu(uid)); return True
    if message_id is not None:
        s['last_message_id']=message_id
    typ=s.get('type'); step=s.get('step')
    # Never treat an accidental /start message as customer data while an order
    # is in progress. Re-prompt the exact current step instead.
    if text == '/start':
        reminders={
            'contact':'📱 هنوز نوبت ثبت آیدی تلگرام یا شماره است. همان را بفرست.',
            'card':'💳 لطفاً کارت مقصد را انتخاب کن.',
            'payer_name':'👤 لطفاً نام و نام خانوادگی شخص واریزکننده را بفرست.',
            'receipt':'🧾 لطفاً عکس فیش پرداخت را ارسال کن.',
            'reference':'📸 لطفاً عکس مرجع را ارسال کن.',
            'what':'✍️ اطلاعات موردنیاز سفارش را بفرست.',
            'variants':'🤖 لطفاً مدل هوش مصنوعی را از دکمه‌ها انتخاب کن.'
        }
        send(cid,reminders.get(step,'🔄 هنوز سفارش در حال ثبت است؛ مرحله فعلی را کامل کن.'))
        return True

    if typ=='card_manage':
        if not text: send(cid,'❌ لطفاً متن را ارسال کن.'); return True
        if step=='name': s['name']=text; s['step']='bank'; send(cid,'🏦 نام بانک؟'); return True
        if step=='bank': s['bank']=text; s['step']='number'; send(cid,'💳 شماره کارت؟'); return True
        if step=='number':
            card={'name':s['name'],'bank':s['bank'],'number':text}
            idx=s.get('index')
            if idx is None: DATA['cards'].append(card)
            else: DATA['cards'][idx]=card
            save_data('Card save'); STATES.pop(uid,None); send(cid,'✅ کارت ذخیره شد.'); cards_admin(cid); return True

    if typ=='price':
        clean=re.sub(r'[,٬\s]','',text)
        if not clean.isdigit(): send(cid,'❌ قیمت باید فقط عدد باشد. مثال: 250000'); return True
        value=int(clean); key=s['key']
        if key in DATA.get('vip',{}): DATA['vip'][key]['price']=value
        elif key in DATA.get('prompts',{}): DATA['prompts'][key]['price']=value
        else: DATA.setdefault('prices',{})[key]=value
        save_data(f'Price {key}'); STATES.pop(uid,None); send(cid,f'✅ قیمت جدید ذخیره شد: {money(value)} تومان')
        if key in DATA.get('vip',{}): vip_prices_admin(cid)
        else: prices_admin(cid)
        return True

    if typ=='vip_add':
        if step=='title':
            if not text: send(cid,'❌ عنوان VIP نمی‌تواند خالی باشد.'); return True
            s['title']=text; s['step']='price'; send(cid,'💰 قیمت VIP را بفرست.'); return True
        if step=='price':
            clean=re.sub(r'[,٬\s]','',text)
            if not clean.isdigit(): send(cid,'❌ قیمت باید عدد باشد.'); return True
            s['price']=int(clean); s['step']='buttons'; send(cid,'🔘 حالا دکمه‌ها و پرامپت‌های VIP را مثل ساخت پست اضافه کن.',vip_add_buttons()); return True
        if step=='buttons':
            send(cid,'🔘 از دکمه «➕ اضافه کردن دکمه» برای ساخت هر مدل استفاده کن.',vip_add_buttons()); return True
        if step=='button_label':
            if not text: send(cid,'❌ نام دکمه نمی‌تواند خالی باشد.'); return True
            s['pending_label']=text; s['step']='button_prompt'; send(cid,'✍️ متن کامل پرامپت این دکمه را بفرست.'); return True
        if step=='button_prompt':
            label=s.pop('pending_label','').strip()
            if not label or not text: send(cid,'❌ نام دکمه و پرامپت لازم است.'); return True
            key=re.sub(r'[^a-z0-9]+','_',label.lower()).strip('_')[:25] or f'v{len(s.get("buttons",[]))+1}'
            base=key; n=2
            while any(b.get('key')==key for b in s.get('buttons',[])):
                key=f'{base}_{n}'; n+=1
            s.setdefault('buttons',[]).append({'key':key,'label':label,'prompt':text})
            s['step']='buttons'; send(cid,'✅ دکمه و پرامپت ذخیره شد.',vip_add_buttons()); return True

    if typ=='vip_edit':
        if not text: return True
        key=s['key']; v=DATA['vip'].get(key)
        if not v: STATES.pop(uid,None); send(cid,'❌ VIP پیدا نشد.'); return True
        if step=='title': v['title']=text; s['step']='prompt'; send(cid,'✍️ متن جدید پرامپت VIP را بفرست یا فقط - بفرست تا متن قبلی بماند.'); return True
        if step=='prompt':
            if text!='-': v['prompt']=text
            save_data(f'Edit {key}'); STATES.pop(uid,None); send(cid,'✅ VIP ویرایش شد.'); vip_admin(cid); return True

    if typ=='prompt_manage':
        if step=='id':
            pid=normalize_pid(text)
            if pid not in DATA.get('prompts',{}): send(cid,'❌ پرامپت پیدا نشد.'); return True
            if s['action']=='delete': DATA['prompts'].pop(pid); save_data(f'Delete {pid}'); STATES.pop(uid,None); send(cid,'🗑 حذف شد.'); prompts_admin(cid); return True
            s['pid']=pid
            if s['action']=='edit_post':
                STATES.pop(uid,None)
                start_prompt_editor(uid,pid)
                return True
            if s['action']=='title': s['step']='value'; send(cid,'📝 عنوان جدید را بفرست.'); return True
            s['step']='variant'; send(cid,'🔘 کلید یا نام variant را بفرست. مثال: chatgpt یا Gemini'); return True
        if step=='variant':
            p=DATA['prompts'][s['pid']]; q=text.lower(); found=None
            for k,v in p.get('variants',{}).items():
                if k.lower()==q or v.get('label','').lower()==q or q in v.get('label','').lower(): found=k; break
            if not found: send(cid,'❌ variant پیدا نشد.'); return True
            s['variant']=found; s['step']='value'; send(cid,'✍️ متن جدید پرامپت را بفرست.'); return True
        if step=='value':
            if s['action']=='title': DATA['prompts'][s['pid']]['title']=text
            else: DATA['prompts'][s['pid']]['variants'][s['variant']]['prompt']=text
            save_data(f'Edit {s["pid"]}'); STATES.pop(uid,None); send(cid,'✅ ذخیره شد.'); prompts_admin(cid); return True

    if typ=='testimonial':
        if step=='kind':
            q=text.lower()
            mapping={'vip':'vip','وی آی پی':'vip','وی‌آی‌پی':'vip','logo':'logo','لوگو':'logo','customer':'customer','مشتری':'customer','normal':'normal','کانال':'normal'}
            kind=mapping.get(q)
            if not kind: send(cid,'❌ یکی از گزینه‌های VIP، لوگو، ساخت عکس با پرامپت مشتری یا ساخت عکس با پرامپت کانال را انتخاب کن.'); return True
            s['kind']=kind; s['step']='number'; send(cid,'🔢 فقط عدد سفارش را بفرست. مثال: 2'); return True
        if step=='number':
            if not text.isdigit(): send(cid,'❌ فقط عدد را بفرست.'); return True
            s['number']=int(text); s['step']='photos'; send(cid,'📸 عکس یا عکس‌های رضایت را بفرست. می‌توانی چند عکس پشت سر هم ارسال کنی؛ بعد روی «✅ ثبت رضایت» بزن.',kb([[btn('📸 ثبت عکس‌های رضایت','testimonial_submit')],[btn('❌ لغو','ord_cancel')]])); return True
        if step=='photos':
            if photo:
                s['photos'].append(photo[-1]['file_id']); send(cid,f'✅ عکس {len(s["photos"])} دریافت شد. عکس بعدی را بفرست یا ثبت رضایت را بزن.'); return True
            send(cid,'📸 لطفاً عکس رضایت ارسال کن یا «ثبت رضایت» را بزن.'); return True

    if typ=='report':
        if step=='description':
            if not text: send(cid,'❌ توضیح مشکل را به صورت متن بفرست.'); return True
            s['description']=text; s['step']='contact'; send(cid,'📱 آیدی تلگرام یا شماره خود را قرار دهید'); return True
        if step=='contact':
            if photo:
                s['evidence']=photo[-1]['file_id']; send(cid,'📎 مدرک دریافت شد. حالا آیدی تلگرام یا شماره را به صورت متن بفرست.'); return True
            if not text: return True
            s['contact']=text; s['step']='evidence'; send(cid,'📎 اگر مدرک/عکس داری ارسال کن؛ در غیر این صورت روی «رد کردن» بزن.',kb([[btn('⏭ رد کردن مدرک','report_skip_evidence')]])); return True
        if step=='evidence':
            if photo:
                s['evidence']=photo[-1]['file_id']
                save_report(uid,s); return True
            send(cid,'📎 عکس مدرک را بفرست یا «رد کردن مدرک» را بزن.'); return True

    if typ=='msg_user':
        if not text: return True
        send(int(s['user_id']),f'💬 پیام ادمین:\n\n{text}'); STATES.pop(uid,None); send(cid,'✅ پیام برای مشتری ارسال شد.');
        if s.get('oid'): view_order(cid,s['oid'])
        return True

    if typ=='report_result':
        if not text: return True
        r=find_report(s['rid'])
        if r:
            r['result']=text; r['status']='🟢 پاسخ داده شد'; save_data('Report answer'); send(r['user_id'],DATA['messages']['report_result'].format(result=text))
        rid=s['rid']; STATES.pop(uid,None); send(cid,'✅ پاسخ برای کاربر ارسال شد.'); report_detail(cid,rid); return True

    if typ=='training_add':
        if step=='title': s['title']=text; s['step']='content'; send(cid,'📝 متن آموزش یا لینک را بفرست. اگر لینک تلگرام/وب است همان لینک را بفرست.'); return True
        if step=='content':
            item={'title':s['title']}
            if re.match(r'^https?://',text): item['url']=text
            else: item['text']=text
            DATA['trainings'].append(item); save_data('Training add'); STATES.pop(uid,None); send(cid,'✅ آموزش اضافه شد.'); training_admin(cid); return True

    if typ=='training_edit':
        idx=s['index']; t=DATA['trainings'][idx]
        if step=='title': t['title']=text; s['step']='content'; send(cid,'📝 متن یا لینک جدید را بفرست.'); return True
        if step=='content':
            t.pop('url',None); t.pop('text',None)
            if re.match(r'^https?://',text): t['url']=text
            else: t['text']=text
            save_data('Training edit'); STATES.pop(uid,None); send(cid,'✅ آموزش ویرایش شد.'); training_admin(cid); return True

    if typ in {'ad_add','ad_edit'}:
        if step=='username': s['username']=text if text.startswith('@') else '@'+text.lstrip('@'); s['step']='title'; send(cid,'📝 عنوان کانال؟'); return True
        if step=='title': s['title']=text; s['step']='url'; send(cid,'🔗 لینک کانال؟ مثال https://t.me/...'); return True
        if step=='url':
            item={'username':s['username'],'title':s['title'],'url':text,'required':True}
            if typ=='ad_add': DATA['ads'].append(item)
            else: DATA['ads'][s['index']]=item
            save_data('Ad channel save'); STATES.pop(uid,None); send(cid,'✅ کانال تبلیغاتی ذخیره شد.'); ads_admin(cid); return True

    if typ=='admin_add':
        if step=='id':
            aid=resolve_user_id(text)
            if not aid:
                send(cid,'❌ این کاربر در ربات پیدا نشد. یا عدد Telegram ID را بفرست، یا اگر می‌خواهی @username بدهی، ابتدا همان شخص یک‌بار ربات را باز کند/Start بزند.'); return True
            s['aid']=aid; s['step']='name'; send(cid,'👤 نام ادمین را بفرست.'); return True
        if step=='name':
            s['name']=text; s['step']='role'; send(cid,'👥 نوع ادمین را انتخاب کن:',kb([[btn('🟣 ادمین پلاس','admin_role_plus'),btn('⚪ ادمین عادی','admin_role_normal')],[btn('❌ لغو','ord_cancel')]])); return True
        if step=='role':
            if text: return True

    if typ=='deliver_media':
        oid=s.get('oid'); o=find_order(oid)
        if not o: STATES.pop(uid,None); send(cid,'❌ سفارش پیدا نشد.'); return True
        if not photo: send(cid,'📸 لطفاً عکس نهایی تحویل را ارسال کن.'); return True
        fid=photo[-1]['file_id']
        send(o['user_id'],f'🔵 سفارش {oid} تحویل داده شد.\n\n📸 فایل تحویلی شما:')
        send_photo(o['user_id'],fid,'📦 تحویل سفارش')
        note=(f'\n\n📌 اگر چیزی دریافت نکردی، از بخش «⚠️ گزارش مشکل» گزارش بده.\n🆔 شماره سفارش: {oid}')
        send(o['user_id'],note)
        o['status']='🔵 تحویل داده شد'; save_data(f'Delivery {oid}')
        saved_msgid=s.get('msgid'); STATES.pop(uid,None); edit_order_message(cid,saved_msgid,o) or send(cid,view_order_text(o),order_action_kb(oid)); return True

    if typ in {'normal','customer','logo'}:
        if step=='payer_name':
            if not text: send(cid,'❌ نام و نام خانوادگی واریزکننده را بفرست.'); return True
            s['payer_name']=text; s['step']='receipt'; send(cid,'🧾 فیش پرداخت را ارسال کن.'); return True
        if step=='receipt':
            if not photo: send(cid,'🧾 لطفاً عکس فیش پرداخت را ارسال کن.'); return True
            s['receipt']=photo[-1]['file_id']
            if typ=='logo':
                finish_order(uid); return True
            s['step']='reference'; send(cid,'📸 حالا عکس مرجع را ارسال کن.'); return True
        if step=='reference':
            if not photo: send(cid,'📸 لطفاً عکس مرجع را ارسال کن.'); return True
            s['ref_photo']=photo[-1]['file_id']; finish_order(uid); return True
        if text: order_text(uid,text); return True
        return True

    if typ=='vip_order':
        if step=='contact': s['contact']=text; s['step']='card'; send(cid,'💳 کارت مقصد را انتخاب کن:',card_kb(s.get('flow_id'))); return True
        if step=='payer_name':
            if not text: send(cid,'❌ نام و نام خانوادگی واریزکننده را بفرست.'); return True
            s['payer_name']=text; s['step']='receipt'; send(cid,'🧾 فیش پرداخت را ارسال کن.'); return True
        if step=='receipt':
            if not photo: send(cid,'🧾 عکس فیش را ارسال کن.'); return True
            s['receipt']=photo[-1]['file_id']; oid=next_id('vip'); now=datetime.now().strftime('%Y-%m-%d %H:%M')
            o={'id':oid,'type':'vip','user_id':uid,'contact':s['contact'],'item':s['vip'],'price':s['price'],'payer_name':s.get('payer_name',''),'status':'🟠 در حال تأیید','payment':'pending','created_at':now,'channel_chat':VIP_ORDERS_CHAT}
            DATA['orders'].append(o); save_data(f'VIP order {oid}')
            vip_details=(f'👑 سفارش VIP {oid}\n{vip_label(s["vip"],DATA["vip"][s["vip"]])}\n💰 {money(s["price"])} تومان\n🕐 {now}\n📌 وضعیت: {o["status"]}')
            if VIP_ORDERS_CHAT:
                ids=[]
                if s.get('receipt'):
                    r=send_photo(VIP_ORDERS_CHAT,s['receipt'],'')
                    if r.get('ok'): ids=[r.get('result',{}).get('message_id')]
                r2=send(VIP_ORDERS_CHAT,vip_details,order_action_kb(oid))
                if r2.get('ok'):
                    o['channel_action_message_id']=r2.get('result',{}).get('message_id'); ids.append(o['channel_action_message_id'])
                o['channel_message_ids']=ids
                o['channel_expires_at']=(datetime.now()+timedelta(days=ORDER_RETENTION_DAYS)).isoformat()
                save_data(f'VIP channel retention {oid}')
            clear_order_state(uid); send(cid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid)); return True
    return True

def handle_post_message(uid,cid,m):
    s=ensure_post_state(uid)
    if not s: return False
    text=(m.get('text') or '').strip(); photos=m.get('photo') or []
    message_id=m.get('message_id')

    # Telegram message_id is monotonic inside a chat.  A webhook retry can arrive
    # with a different update_id, so update_id-only deduplication is not enough.
    # Never let an older/already-consumed user message run the next post-builder
    # stage a second time.  Keep a small persisted set as an additional guard for
    # non-monotonic/recovered states.
    try:
        mid=int(message_id) if message_id is not None else None
    except Exception:
        mid=None
    processed=s.setdefault('processed_message_ids',[])
    if mid is not None:
        if mid in processed or (int(s.get('last_user_message_id',0) or 0) and mid<=int(s.get('last_user_message_id',0) or 0)):
            return True
        processed.append(mid)
        if len(processed)>80:
            del processed[:-80]
        s['last_user_message_id']=mid
        s['last_message_id']=mid
    if text=='/cancel': POST_STATES.pop(uid,None); persist_conversation_states_async(); send(cid,'❌ ساخت پست لغو شد.',admin_menu(uid)); return True
    # A short debounce protects against the same human input arriving twice from
    # overlapping webhook workers. Legitimate repeated text after a stage change
    # is allowed because post_advance clears this fingerprint.
    fp=post_input_fingerprint(text,photos)
    now=time.monotonic()
    if fp and fp==s.get('last_input_fingerprint') and now-float(s.get('last_input_at',0) or 0)<8:
        return True
    s['last_input_fingerprint']=fp
    s['last_input_at']=now
    step=s['step']
    if s.get('mode')=='edit':
        if step=='edit_variant_label':
            key=s.get('edit_key')
            if not text:
                send(cid,'❌ نام دکمه نمی‌تواند خالی باشد.')
                return True
            for b in s.get('buttons',[]):
                if b.get('key')==key:
                    s['pending_edit_label']=text
                    break
            else:
                post_advance(s,'buttons')
                send(cid,'⚠️ دکمه پیدا نشد.',prompt_edit_menu(s))
                return True
            post_advance(s,'edit_variant_prompt')
            send(cid,'✍️ متن کامل پرامپت این دکمه را بفرست.')
            return True
        if step=='edit_variant_prompt':
            key=s.get('edit_key')
            found=False
            for b in s.get('buttons',[]):
                if b.get('key')==key:
                    if s.get('pending_edit_label'):
                        b['label']=s.pop('pending_edit_label')
                    b['prompt']=text
                    found=True
                    break
            post_advance(s,'buttons')
            send(cid,'✅ دکمه با موفقیت ویرایش شد.',prompt_edit_menu(s) if found else prompt_edit_menu(s))
            return True
        if step=='edit_add_label':
            if not text:
                send(cid,'❌ نام دکمه نمی‌تواند خالی باشد.')
                return True
            s['pending_label']=text
            post_advance(s,'edit_add_prompt')
            send(cid,'✍️ متن کامل پرامپت دکمه جدید را بفرست.')
            return True
        if step=='edit_add_prompt':
            label=s.pop('pending_label','').strip()
            if not label or not text:
                send(cid,'❌ نام دکمه و پرامپت لازم است.')
                return True
            key=re.sub(r'[^a-z0-9]+','_',label.lower()).strip('_')[:25] or f'v{len(s["buttons"])+1}'
            base=key; n=2
            while any(b.get('key')==key for b in s.get('buttons',[])):
                key=f'{base}_{n}'; n+=1
            s['buttons'].append({'key':key,'label':label,'prompt':text})
            post_advance(s,'buttons')
            send(cid,'✅ دکمه جدید اضافه شد.',prompt_edit_menu(s))
            return True
        if step=='buttons':
            send(cid,'از دکمه‌های زیر استفاده کن.',prompt_edit_menu(s))
            return True
        return True
    if step=='photos':
        if photos:
            fid=photos[-1].get('file_id')
            if fid and fid not in s.setdefault('photos',[]):
                s['photos'].append(fid)
            s['revision']=int(s.get('revision',0))+1
            # Persist the actual photo immediately. This prevents a restart or
            # a delayed callback from seeing a photo-less snapshot.
            persist_conversation_states_async()
            send(cid,f'✅ عکس {len(s["photos"])} دریافت شد. عکس بعدی را بفرست یا «اتمام عکس‌ها» را بزن.')
            return True
        send(cid,'🖼 لطفاً عکس ارسال کن یا «اتمام عکس‌ها» را بزن.')
        return True
    if not text: return True
    if step=='name':
        s['name']=text; post_advance(s,'number'); send(cid,'🔢 شماره پرامپت را بفرست.'); return True
    if step=='number':
        if not text.isdigit(): send(cid,'❌ شماره باید عدد باشد.'); return True
        s['number']=int(text); post_advance(s,'suitable'); send(cid,'🎯 مناسب برای چه کاری؟'); return True
    if step=='suitable':
        s['suitable']=text; post_advance(s,'for'); send(cid,'📌 در بخش «برای» چه بنویسم؟'); return True
    if step=='for':
        s['for_what']=text; post_advance(s,'buttons'); send(cid,'✅ اطلاعات ثبت شد. حالا دکمه و پرامپت را اضافه کن.',post_buttons(s.get('flow_id'),s.get('stage_token'))); return True
    if step=='button_label':
        s['pending_label']=text; post_advance(s,'button_prompt'); send(cid,'✍️ متن کامل پرامپت این دکمه را بفرست.'); return True
    if step=='button_prompt':
        label=s.pop('pending_label','').strip()
        if not label: send(cid,'❌ ابتدا نام دکمه را بفرست.'); return True
        key=re.sub(r'[^a-z0-9]+','_',label.lower()).strip('_')[:25] or f'v{len(s["buttons"])+1}'
        base=key; n=2
        while any(b['key']==key for b in s['buttons']): key=f'{base}_{n}'; n+=1
        s['buttons'].append({'key':key,'label':label,'prompt':text})
        post_advance(s,'buttons')
        send(cid,'✅ دکمه و پرامپت ذخیره شد.',post_buttons(s.get('flow_id'),s.get('stage_token'))); return True
    send(cid,'از دکمه‌های زیر استفاده کن.',post_buttons(s.get('flow_id'),s.get('stage_token'))); return True

def view_order_text(o):
    # Normal channel-prompt orders intentionally show the registered contact in
    # the channel details so the admin can use it when requesting satisfaction.
    # VIP/customer/logo private data remains protected as before.
    text=f'🆔 {o["id"]}\n🛒 {o.get("item","")}\n'
    if o.get('type')=='normal':
        text+=f'📱 تماس: {o.get("contact", "ثبت نشده")}\n'
    if o.get('model'): text+=f'🤖 مدل: {o.get("model")}\n'
    text+=f'💰 {money(o.get("price",0))} تومان\n'
    if o.get('type') not in {'vip','customer','logo'} and o.get('payer_name'):
        text+=f'👤 واریزکننده: {o.get("payer_name")}\n'
    text+=f'🕐 {o.get("created_at","")}\n📌 وضعیت: {o.get("status","")}\n💳 پرداخت: {o.get("payment","pending")}'
    return text

def view_order(cid,oid):
    o=find_order(oid)
    if not o: return send(cid,'❌ سفارش پیدا نشد.')
    send(cid,view_order_text(o),order_action_kb(oid))

def edit_order_message(cid,msgid,o):
    # The action/details message is stored separately from the photo/album messages.
    # This prevents Telegram caption editing from touching the photos.
    target=o.get('channel_action_message_id') or msgid
    if not target: return False
    text=view_order_text(o)
    r=api('editMessageText',{'chat_id':cid,'message_id':target,'text':text,'reply_markup':order_action_kb(o.get('id'))})
    if r.get('ok'): return True
    r=api('editMessageCaption',{'chat_id':cid,'message_id':target,'caption':text,'reply_markup':order_action_kb(o.get('id'))})
    return bool(r.get('ok'))

def can_manage_order(uid,o):
    if not is_admin(uid): return False
    return has_perm(uid, 'vip_orders' if o.get('type')=='vip' else 'logo_orders' if o.get('type')=='logo' else 'orders')

# ---------- callback router ----------
def deliver_order_to_user(o):
    uid=o.get('user_id'); oid=o.get('id')
    note=(f'\n\n📌 اگر چیزی دریافت نکردی، از بخش «⚠️ گزارش مشکل» گزارش بده.\n'
          f'🆔 شماره سفارش: {oid}')
    if o.get('type')=='vip':
        v=DATA.get('vip',{}).get(o.get('item'),{})
        variants=v.get('variants',{})
        if variants:
            rows=[[btn(x.get('label',k),f'vip_prompt|{oid}|{k}')] for k,x in variants.items()]
            send(uid,f'👑 سفارش {oid} تحویل داده شد.\n\nیکی از گزینه‌های زیر را انتخاب کن تا پرامپت همان مدل برایت ارسال شود.'+note,kb(rows))
        else:
            prompt=v.get('prompt','')
            if prompt: send(uid,f'👑 پرامپت VIP شما\n\n{prompt}'+note)
            else: send(uid,'❌ متن پرامپت VIP پیدا نشد.'+note)
    elif o.get('type')=='customer':
        if o.get('ref_photo'): send_photo(uid,o['ref_photo'],'🖼 عکس سفارش شما')
        send(uid,'🔵 سفارش ساخت عکس شما تحویل داده شد.'+note)
    elif o.get('type')=='logo':
        send(uid,'🔵 سفارش ساخت لوگو برای شما تحویل داده شد.'+note)
    else:
        sv=o.get('selected_variant') or []
        if len(sv)==2:
            pid,vk=sv; v=DATA.get('prompts',{}).get(pid,{}).get('variants',{}).get(vk,{})
            prompt=v.get('prompt','')
            if prompt: send(uid,f'📋 پرامپت سفارش {o.get("item","")}\n🤖 مدل: {v.get("label",vk)}\n\n{prompt}'+note)
            else: send(uid,'❌ متن پرامپت سفارش پیدا نشد.'+note)
        else:
            send(uid,'🔵 سفارش شما تحویل داده شد.'+note)

def callback(uid,cid,data,msgid=None,callback_id=None):
    # Telegram can retry a callback update.  callback_query.id is unique for the
    # actual click, so it is the correct idempotency key; update_id alone is not
    # sufficient when a webhook retry is represented by another update delivery.
    cbid=callback_id or CURRENT_CB
    if cbid:
        with UPDATE_LOCK:
            if cbid in PROCESSED_CALLBACKS:
                return
            PROCESSED_CALLBACKS[cbid]=time.monotonic()
            if len(PROCESSED_CALLBACKS)>PROCESSED_CALLBACK_LIMIT:
                oldest=sorted(PROCESSED_CALLBACKS,key=PROCESSED_CALLBACKS.get)[:len(PROCESSED_CALLBACKS)-PROCESSED_CALLBACK_LIMIT]
                for key in oldest: PROCESSED_CALLBACKS.pop(key,None)
    answer(cbid)
    # A duplicate click can occasionally arrive with another callback_query.id.
    # Treat the same action for the same admin as idempotent for a few seconds.
    action_key=f'{uid}|{data}'
    now_cb=time.monotonic()
    with UPDATE_LOCK:
        stale=[k for k,t in PROCESSED_CALLBACK_ACTIONS.items() if now_cb-t>PROCESSED_CALLBACK_ACTION_TTL]
        for k in stale: PROCESSED_CALLBACK_ACTIONS.pop(k,None)
        if action_key in PROCESSED_CALLBACK_ACTIONS:
            return
        PROCESSED_CALLBACK_ACTIONS[action_key]=now_cb
    if data.startswith('vip_customer|'):
        if not can_private_data(uid): return send(cid,'⛔ این اطلاعات فقط برای مالک و ادمین پلاس دارای دسترسی مجاز است.')
        oid=data.split('|',1)[1]; ref=DATA.get('vip_customer_refs',{}).get(oid)
        if not ref: return send(cid,'❌ اطلاعات مشتری این سفارش پیدا نشد.')
        send(uid,f'👤 {ref.get("vip_label",oid)}\n📱 آیدی/شماره ثبت‌شده مشتری: {ref.get("contact","")}')
        return
    if data=='check_membership':
        if require_membership(cid,uid): send(cid,'✅ عضویت تأیید شد.',main_menu(uid))
        return
    if not is_admin(uid) and not require_membership(cid,uid):
        return
    if data=='back_main': send(cid,WELCOME,main_menu(uid)); return
    if data=='admin_menu': send(cid,'مدیریت',admin_menu(uid)); return
    if data=='user_training': training_user(cid); return
    if data=='testimonial_channel_missing': send(cid,'❌ لینک کانال رضایت مشتری در تنظیمات ربات ثبت نشده است.'); return
    if data=='testimonial_start' and (guard(uid,'orders') or guard(uid,'vip_orders')):
        reset_admin_flow(uid)
        STATES[uid]={'type':'testimonial','step':'kind','photos':[],'last_message_id':None}
        testimonial_admin(cid); return
    if data.startswith('testimonial_type|') and (guard(uid,'orders') or guard(uid,'vip_orders')):
        kind=data.split('|',1)[1]
        st=STATES.get(uid)
        if not st or st.get('type')!='testimonial': return send(cid,'⚠️ ابتدا «ثبت رضایت» را شروع کن.')
        st.update({'step':'number','kind':kind,'photos':[]})
        send(cid,'🔢 فقط عدد سفارش را بفرست. مثال: 2'); return
    if data=='testimonial_submit' and (guard(uid,'orders') or guard(uid,'vip_orders')):
        s=STATES.get(uid)
        if not s or s.get('type')!='testimonial' or not s.get('photos'): return send(cid,'❌ حداقل یک عکس رضایت لازم است.')
        publish_testimonial_album(uid,s['kind'],s['number'],s['photos']); STATES.pop(uid,None); return
    if data=='testimonial_channel_admin' and (guard(uid,'orders') or guard(uid,'vip_orders')):
        u=os.getenv('TESTIMONIALS_CHANNEL_URL','').strip()
        send(cid,'📣 کانال رضایت مشتری',kb([[urlbtn('📣 ورود به کانال',u)]] if u else [])); return
    if data.startswith('training_view|'):
        i=int(data.split('|')[1]); t=DATA['trainings'][i]; send(cid,f'📚 {t.get("title","")}\n\n{t.get("text","")}'); return
    if data=='user_vip': vip_user(cid); return
    if data=='order_menu':
        if uid in STATES: return send(cid,'⚠️ یک سفارش در حال ثبت است. ابتدا همان سفارش را کامل یا لغو کن.')
        start_order(uid); return
    if data=='customer_order':
        if uid in STATES: return send(cid,'⚠️ یک سفارش در حال ثبت است. ابتدا همان سفارش را کامل یا لغو کن.')
        start_order(uid,True); return
    if data=='logo_order':
        if uid in STATES: return send(cid,'⚠️ یک سفارش در حال ثبت است. ابتدا همان سفارش را کامل یا لغو کن.')
        start_order(uid,logo=True); return
    if data=='report_start': start_report(uid); return
    if data=='report_skip_evidence':
        s=STATES.get(uid)
        if s and s.get('type')=='report' and s.get('step')=='evidence': save_report(uid,s)
        return
    if data.startswith('ord_cancel'):
        parts=data.split('|',1); flow_id=parts[1] if len(parts)==2 else None
        s=STATES.get(uid)
        if s and s.get('type') in ORDER_STATE_TYPES:
            ensure_order_flow_state(s)
            if flow_id is not None and s.get('flow_id') != flow_id: return
            if flow_id is None: return
            clear_order_state(uid)
        else:
            STATES.pop(uid,None); persist_order_states_async()
        send(cid,'❌ لغو شد.',main_menu(uid)); return
    if data.startswith('getprompt|'):
        parts=data.split('|',2)
        if len(parts)!=3: return
        _,pid,vkey=parts
        p=DATA.get('prompts',{}).get(pid)
        v=p.get('variants',{}).get(vkey) if p else None
        if not v: return send(cid,'❌ این پرامپت دیگر در دسترس نیست.')
        if not require_membership(cid,uid): return
        send(cid,v.get('prompt',''),delivery_kb(v))
        return
    if data.startswith('ord_price_ok|'):
        flow_id=data.split('|',1)[1]
        if not order_state_matches(uid,flow_id,{'customer','logo'},{'confirm_price'}): return
        order_price_confirm(uid); return
    if data=='ord_price_ok':
        # Legacy price buttons are rejected for an active flow. This prevents an old
        # keyboard from moving a new order backwards/forwards.
        return
    if data.startswith('ord_variant|'):
        parts=data.split('|')
        if len(parts)!=4: return
        _,pid,k,flow_id=parts
        if not order_state_matches(uid,flow_id,{'normal'},{'variants'}): return
        s=STATES[uid]
        if normalize_pid(pid)!=pid or pid not in DATA.get('prompts',{}): return
        s['selected_variant']=(pid,k); s['price']=price_for(pid); s['step']='contact'; send(cid,f'💰 قیمت: {money(s["price"])} تومان\n\n📱 آیدی تلگرام یا شماره خود را قرار دهید'); return
    if data.startswith('card|'):
        parts=data.split('|')
        if len(parts)!=3: return
        _,idx,flow_id=parts
        try: i=int(idx)
        except Exception: return
        if not order_state_matches(uid,flow_id,{'normal','customer','logo','vip_order'},{'card'}): return
        if i<0 or i>=len(DATA.get('cards',[])): return
        s=STATES[uid]
        s['card']=DATA['cards'][i]; s['step']='payer_name'; send(cid,f'💳 شماره کارت: {s["card"]["number"]}\nبه نام: {s["card"]["name"]}\n\n👤 نام و نام خانوادگی شخص واریزکننده را بفرست.'); return
    if data.startswith('vip_prompt|'):
        parts=data.split('|',2)
        if len(parts)!=3: return
        _,oid,vkey=parts
        o=find_order(oid)
        if not o or o.get('type')!='vip' or o.get('user_id')!=uid: return send(cid,'⛔ این گزینه برای این سفارش معتبر نیست.')
        if o.get('status')!='🔵 تحویل داده شد': return send(cid,'⏳ این سفارش هنوز تحویل نشده است.')
        v=DATA.get('vip',{}).get(o.get('item'),{})
        variant=v.get('variants',{}).get(vkey)
        if not variant: return send(cid,'❌ پرامپت این گزینه پیدا نشد.')
        send(cid,f'📋 {variant.get("label",vkey)}\n\n{variant.get("prompt","")}')
        return
    if data.startswith('vip_buy|'):
        if uid in STATES: return send(cid,'⚠️ یک سفارش در حال ثبت است. ابتدا همان سفارش را کامل یا لغو کن.')
        k=data.split('|',1)[1]; v=DATA.get('vip',{}).get(k)
        if not v: return send(cid,'❌ VIP پیدا نشد.')
        STATES[uid]={'type':'vip_order','step':'contact','vip':k,'price':price_for(k),'flow_id':new_order_flow_id()}
        send(cid,f'👑 {vip_label(k,v)}\n💰 قیمت: {money(price_for(k))} تومان\n\n📱 آیدی تلگرام یا شماره خود را قرار دهید'); return
    if data.startswith('pay_ok|') or data.startswith('pay_bad|'):
        oid=data.split('|')[1]; o=find_order(oid)
        if not o: return
        if not can_manage_order(uid,o): return send(cid,'⛔ دسترسی نداری.')
        ok=data.startswith('pay_ok')
        # Idempotent payment buttons: a second callback cannot restart the same stage.
        if ok and o.get('payment')=='approved':
            edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
            return
        if (not ok) and o.get('payment')=='rejected':
            edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
            return
        o['payment']='approved' if ok else 'rejected'
        if ok:
            o['status']='🟡 در حال انجام'
            user_msg=f'✅ پرداخت سفارش {oid} تأیید شد.\n\n🟡 سفارش شما تأیید شد و در حال انجام است.'
        else:
            o['status']='❌ رد شده'
            user_msg=f'❌ پرداخت سفارش {oid} رد شد.\n\nاگر اشتباهی رخ داده، از بخش «⚠️ گزارش مشکل» گزارش بده.'
        # Update the channel action message immediately so the next-stage button is
        # visible without needing a second or third click. Persist afterwards.
        edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
        send(o['user_id'],user_msg)
        if not ok:
            # Rejected payments are terminal: keep the channel message, remove from active admin lists.
            if o.get('id') not in {x.get('id') for x in DATA.setdefault('completed_orders',[])}:
                DATA['completed_orders'].append(deepcopy(o))
            save_data(f'Payment {oid}')
            DATA['orders']=[x for x in DATA.get('orders',[]) if x.get('id')!=oid]
            save_data(f'Remove rejected {oid}')
        else:
            save_data(f'Payment {oid}')
        return
    if data.startswith('status|'):
        _,oid,st=data.split('|'); o=find_order(oid)
        if not o: return
        if not can_manage_order(uid,o): return send(cid,'⛔ دسترسی نداری.')
        if st=='delivered':
            if o.get('status')=='🔵 تحویل داده شد':
                edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
                return
            if o.get('status')!='🟡 در حال انجام' or o.get('payment')!='approved':
                return send(cid,'⏳ ابتدا پرداخت را تأیید کن.')
            o['status']='🔵 تحویل داده شد'
            # For customer-prompt/logo orders, the actual file/ID delivery can be
            # handled separately; the customer still gets the standard notice.
            if o.get('type') not in {'customer','logo'}:
                deliver_order_to_user(o)
            else:
                send(o['user_id'],f'🔵 سفارش {oid} تحویل داده شد.\n\n📌 اگر چیزی دریافت نکردی، از بخش «⚠️ گزارش مشکل» گزارش بده.\n🆔 شماره سفارش: {oid}')
            edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
            save_data(f'Status {oid}')
        elif st=='satisfied':
            if o.get('status')=='🟢 تمام شد' or o.get('satisfied_at'): return
            if o.get('status')!='🔵 تحویل داده شد': return
            o['status']='🟢 تمام شد'; o['satisfied_at']=datetime.now().isoformat()
            # Copy only the non-sensitive record to completed history, then delete
            # customer contact/private prompt data from active memory.
            completed=deepcopy(o)
            completed.pop('contact',None)
            completed.pop('prompt_text',None)
            completed.pop('payer_name',None)
            DATA.setdefault('completed_orders',[]).append(completed)
            edit_order_message(cid,o.get('channel_action_message_id') or msgid,o)
            DATA['orders']=[x for x in DATA.get('orders',[]) if x.get('id')!=oid]
            DATA.get('vip_customer_refs',{}).pop(oid,None)
            save_data(f'Satisfaction {oid}')
            # Deliberately do not post a satisfaction confirmation in the Orders channel.
            send(o['user_id'],f'🟢 رضایت سفارش {oid} ثبت شد و سفارش تمام شد.')
        else:
            return
        return
    if data.startswith('order_contact|') or data.startswith('order_prompt|') or data.startswith('order_request|'):
        oid=data.split('|',1)[1]; o=find_order(oid)
        if not o or o.get('type') not in {'vip','customer','logo'}: return
        if not can_manage_order(uid,o): return send(cid,'⛔ دسترسی نداری.')
        if data.startswith('order_contact|'):
            return send(cid,f'💬 اطلاعات تماس سفارش {oid}\n\n📱 آیدی/شماره مشتری:\n{o.get("contact", "ثبت نشده")}')
        if data.startswith('order_prompt|') and o.get('type')=='customer':
            return send(cid,f'📝 پرامپت مشتری — {oid}\n\n{o.get("prompt_text", "ثبت نشده")}')
        if data.startswith('order_request|') and o.get('type')=='logo':
            return send(cid,f'🎨 خواسته مشتری برای ساخت لوگو — {oid}\n\n{o.get("prompt_text", "ثبت نشده")}')
        return
    if data.startswith('msg_user|'):
        oid=data.split('|')[1]; o=find_order(oid)
        if not o: return
        if not can_manage_order(uid,o): return send(cid,'⛔ دسترسی نداری.')
        if o and o.get('type') in {'vip','customer','logo'}: STATES[uid]={'type':'msg_user','step':'text','user_id':o['user_id'],'oid':oid}; send(cid,f'💬 متن پیام به مشتری {oid} را بفرست.\n\nاطلاعات تماس ثبت‌شده: {o.get("contact","")}')
        return
    if data.startswith('view_order|'): view_order(cid,data.split('|',1)[1]); return

    # Admin menu callbacks
    if data.startswith('adm_') or data in {'post_start','prompt_edit','prompt_text','prompt_delete'}:
        if not is_admin(uid): return send(cid,'⛔ دسترسی نداری.')
    if data=='adm_prompts' and guard(uid,'prompts'): prompts_admin(cid); return
    if data=='adm_prices' and guard(uid,'prices'): prices_admin(cid); return
    if data=='price_vip' and guard(uid,'prices'): vip_prices_admin(cid); return
    if data in {'price_channel','price_customer','price_logo'} and guard(uid,'prices'):
        key={'price_channel':'channel_prompt','price_customer':'customer_prompt','price_logo':'logo'}[data]
        reset_admin_flow(uid)
        STATES[uid]={'type':'price','step':'value','key':key,'last_message_id':None}; send(cid,f'💰 قیمت جدید را برای {"ساخت عکس با پرامپت کانال" if key=="channel_prompt" else "ساخت عکس با پرامپت مشتری" if key=="customer_prompt" else "سفارش ساخت لوگو"} به تومان بفرست.'); return
    if data=='adm_vip' and guard(uid,'vip'): vip_admin(cid); return
    if data=='adm_orders' and guard(uid,'orders'): orders_admin(cid,False); return
    if data=='adm_vip_orders' and guard(uid,'vip_orders'): orders_admin(cid,True); return
    if data=='adm_logo_orders' and guard(uid,'logo_orders'): orders_admin(cid,False,order_type='logo'); return
    if data=='adm_reports' and guard(uid,'reports'): reports_admin(cid); return
    if data=='adm_ads' and guard(uid,'ads'): ads_admin(cid); return
    if data=='adm_training' and guard(uid,'training'): training_admin(cid); return
    if data=='adm_admins' and guard(uid,'admins'): admins_admin(cid); return
    if data=='adm_stats' and guard(uid,'stats'):
        all_orders=DATA.get('orders',[])+DATA.get('completed_orders',[])
        counts={'normal':0,'customer':0,'logo':0,'vip':0}
        for x in all_orders: counts[x.get('type','normal')]=counts.get(x.get('type','normal'),0)+1
        stats=(f'📊 آمار\n\n📝 پرامپت‌ها: {len(DATA["prompts"])}\n👑 VIP: {len(DATA["vip"])}\n\n🖼 پرامپت کانال: {counts.get("normal",0)}\n✍️ پرامپت مشتری: {counts.get("customer",0)}\n🎨 لوگو: {counts.get("logo",0)}\n👑 سفارش VIP: {counts.get("vip",0)}\n\n📦 فعال: {len(DATA["orders"])}\n✅ تکمیل‌شده: {len(DATA.get("completed_orders",[]))}\n⚠️ گزارش‌ها: {len(DATA["reports"])}')
        send(cid,stats,admin_menu(uid)); return
    if data=='adm_notifications' and guard(uid,'notifications'): notifications_admin(cid); return
    if data=='adm_settings' and guard(uid,'settings'): settings_admin(cid); return
    if data=='adm_cards' and guard(uid,'settings'): cards_admin(cid); return

    if data.startswith('setprice|'):
        if not guard(uid,'prices'): return send(cid,'⛔ دسترسی نداری.')
        key=data.split('|',1)[1]; reset_admin_flow(uid); STATES[uid]={'type':'price','step':'value','key':key,'last_message_id':None}; send(cid,f'💰 قیمت جدید برای {key} را به تومان بفرست.'); return
    if data=='card_add' and guard(uid,'settings'):
        STATES[uid]={'type':'card_manage','step':'name','index':None}; send(cid,'👤 به نام؟'); return
    if data.startswith('card_edit|') and guard(uid,'settings'):
        i=int(data.split('|')[1]); STATES[uid]={'type':'card_manage','step':'name','index':i}; send(cid,'👤 نام صاحب کارت را بفرست.'); return
    if data.startswith('card_delete|') and guard(uid,'settings'):
        i=int(data.split('|')[1]);
        if i<len(DATA['cards']): DATA['cards'].pop(i); save_data('Card delete')
        cards_admin(cid); return

    if data=='vip_add' and guard(uid,'vip'):
        reset_admin_flow(uid)
        STATES[uid]={'type':'vip_add','step':'title','buttons':[],'last_message_id':None}
        send(cid,'👑 عنوان VIP را بفرست.')
        return
    if data=='vip_add_button' and guard(uid,'vip'):
        s=STATES.get(uid)
        if not s or s.get('type')!='vip_add': return send(cid,'⚠️ ابتدا افزودن VIP را شروع کن.')
        if s.get('step')!='buttons': return send(cid,'⚠️ ابتدا مراحل قبلی افزودن VIP را کامل کن.')
        s['step']='button_label'; send(cid,'🔘 نام دکمه را بفرست. مثال: Gemini'); return
    if data=='vip_add_done' and guard(uid,'vip'):
        s=STATES.get(uid)
        if not s or s.get('type')!='vip_add': return send(cid,'⚠️ عملیات افزودن VIP پیدا نشد.')
        if s.get('step')!='buttons' or not s.get('buttons'): return send(cid,'⚠️ حداقل یک دکمه و پرامپت برای VIP اضافه کن.')
        nums=[int(re.sub(r'\D','',k)) for k in DATA.get('vip',{}) if re.sub(r'\D','',k)]
        key=f'VIP-{max(nums,default=0)+1}'
        value={'title':s['title'],'price':s['price'],'variants':{b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']}}
        DATA['vip'][key]=value
        save_data(f'Add {key}')
        if ARCHIVE_CHAT:
            send(ARCHIVE_CHAT,f'👑 {vip_label(key,value)}\n\n🆔 {key}')
            for b in s['buttons']:
                send(ARCHIVE_CHAT,f'🔘 {b["label"]}')
                send(ARCHIVE_CHAT,b['prompt'])
        STATES.pop(uid,None)
        send(cid,f'✅ {vip_label(key,value)} اضافه شد.')
        vip_admin(cid)
        return
    if data.startswith('vip_edit|') and guard(uid,'vip'):
        key=data.split('|',1)[1]; STATES[uid]={'type':'vip_edit','step':'title','key':key}; send(cid,'✏️ عنوان جدید VIP را بفرست.'); return
    if data.startswith('vip_delete|') and guard(uid,'vip'):
        key=data.split('|',1)[1]; DATA['vip'].pop(key,None); save_data(f'Delete {key}'); send(cid,'🗑 VIP حذف شد.'); vip_admin(cid); return

    if data=='prompt_edit' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'edit_post','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return
    if data=='prompt_text' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'text','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return
    if data=='prompt_delete' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'delete','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return

    if data=='post_start' and guard(uid,'prompts'):
        post_start(uid); return
    if data.startswith('post_photos_done|') and guard(uid,'prompts'):
        parts=data.split('|')
        flow_id=parts[1] if len(parts)>1 else ''
        stage_token=parts[3] if len(parts)>3 else ''
        s=post_state_matches(uid,flow_id)
        if not s or s.get('stage_token')!=stage_token:
            return
        if s.get('step')!='photos': return
        if not s.get('photos'):
            return send(cid,'⚠️ حداقل یک عکس بفرست.')
        post_advance(s,'name')
        send(cid,'📝 نام پست را بفرست.')
        return
    if data.startswith('post_add|') and guard(uid,'prompts'):
        parts=data.split('|')
        flow_id=parts[1] if len(parts)>1 else ''
        stage_token=parts[3] if len(parts)>3 else ''
        s=post_state_matches(uid,flow_id)
        if not s or s.get('stage_token')!=stage_token or s.get('step')!='buttons': return
        post_advance(s,'button_label')
        send(cid,'🔘 نام دکمه را بفرست. مثال: Gemini')
        return
    if data.startswith('post_publish|') and guard(uid,'prompts'):
        parts=data.split('|')
        flow_id=parts[1] if len(parts)>1 else ''
        stage_token=parts[3] if len(parts)>3 else ''
        s=post_state_matches(uid,flow_id)
        if not s or s.get('stage_token')!=stage_token or s.get('step')!='buttons': return
        publish_post(uid)
        return
    if data.startswith('post_cancel|'):
        parts=data.split('|')
        flow_id=parts[1] if len(parts)>1 else ''
        stage_token=parts[2] if len(parts)>2 else ''
        s=post_state_matches(uid,flow_id)
        if not s or (stage_token and s.get('stage_token')!=stage_token): return
        POST_STATES.pop(uid,None)
        persist_conversation_states_async()
        send(cid,'❌ ساخت پست لغو شد.',admin_menu(uid))
        return

    if data.startswith('post_edit_variant|') and guard(uid,'prompts'):
        parts=data.split('|'); flow_id=parts[1] if len(parts)>1 else ''; key=parts[2] if len(parts)>2 else ''; tok=parts[3] if len(parts)>3 else ''
        s=POST_STATES.get(uid)
        if not s or s.get('flow_id')!=flow_id or s.get('stage_token')!=tok or s.get('step')!='buttons': return
        current=next((b for b in s.get('buttons',[]) if b.get('key')==key),None)
        if not current: return
        s['edit_key']=key
        s['pending_edit_label']=current.get('label','')
        post_advance(s,'edit_variant_label')
        send(cid,f'✏️ نام جدید دکمه «{current.get("label","")}» را بفرست.\nاگر نامش را هم نمی‌خواهی عوض کنی، همان نام فعلی را بفرست.')
        return
    if data.startswith('post_delete_variant|') and guard(uid,'prompts'):
        parts=data.split('|'); flow_id=parts[1] if len(parts)>1 else ''; key=parts[2] if len(parts)>2 else ''; tok=parts[3] if len(parts)>3 else ''
        s=POST_STATES.get(uid)
        if not s or s.get('flow_id')!=flow_id or s.get('stage_token')!=tok or s.get('step')!='buttons': return
        before=len(s.get('buttons',[]))
        s['buttons']=[b for b in s.get('buttons',[]) if b.get('key')!=key]
        if len(s['buttons'])==before: return
        s['revision']=int(s.get('revision',0))+1
        persist_conversation_states_async()
        send(cid,'🗑 دکمه حذف شد.',prompt_edit_menu(s))
        return
    if data=='post_edit_noop' and guard(uid,'prompts'):
        return
    if data.startswith('post_edit_add|') and guard(uid,'prompts'):
        parts=data.split('|'); flow_id=parts[1] if len(parts)>1 else ''; tok=parts[2] if len(parts)>2 else ''
        s=POST_STATES.get(uid)
        if not s or s.get('flow_id')!=flow_id or s.get('stage_token')!=tok or s.get('step')!='buttons': return
        post_advance(s,'edit_add_label'); send(cid,'🔘 نام دکمه جدید را بفرست.'); return
    if data.startswith('post_edit_publish|') and guard(uid,'prompts'):
        parts=data.split('|'); flow_id=parts[1] if len(parts)>1 else ''; tok=parts[2] if len(parts)>2 else ''
        s=POST_STATES.get(uid)
        if not s or s.get('flow_id')!=flow_id or s.get('stage_token')!=tok or s.get('step')!='buttons': return
        publish_edited_post(uid); return
    if data.startswith('post_edit_cancel|'):
        parts=data.split('|'); flow_id=parts[1] if len(parts)>1 else ''; tok=parts[2] if len(parts)>2 else ''
        s=POST_STATES.get(uid)
        if not s or s.get('flow_id')!=flow_id or s.get('stage_token')!=tok: return
        POST_STATES.pop(uid,None); persist_conversation_states_async(); send(cid,'❌ ویرایش لغو شد.',admin_menu(uid)); return

    if data.startswith('report_view|') and guard(uid,'reports'): report_detail(cid,data.split('|',1)[1]); return
    if data.startswith('report_resolve|') and guard(uid,'reports'):
        rid=data.split('|',1)[1]; r=find_report(rid)
        if not r: return
        r['status']='🟢 حل شد'; r['resolved_at']=datetime.now().strftime('%Y-%m-%d %H:%M'); save_data(f'Resolve report {rid}')
        send(r['user_id'],f'🟢 گزارش {rid} بررسی و حل شد.\n\nاگر هنوز مشکلی داری، یک گزارش جدید ثبت کن.')
        DATA['reports']=[x for x in DATA.get('reports',[]) if x.get('id')!=rid]; save_data(f'Delete resolved report {rid}')
        send(cid,'🗑 گزارش حل‌شده پاک شد.'); reports_admin(cid); return
    if data.startswith('report_reply|') and guard(uid,'reports'):
        rid=data.split('|',1)[1]; r=find_report(rid)
        if r: STATES[uid]={'type':'report_result','step':'text','rid':rid}; send(cid,f'💬 پاسخ به گزارش {rid} را بفرست.\n\nمتن گزارش:\n{r.get("description","")}\n\nتماس: {r.get("contact","")}')
        return

    if data=='training_add' and guard(uid,'training'): STATES[uid]={'type':'training_add','step':'title'}; send(cid,'📚 عنوان آموزش را بفرست.'); return
    if data.startswith('training_edit|') and guard(uid,'training'):
        i=int(data.split('|')[1]); STATES[uid]={'type':'training_edit','step':'title','index':i}; send(cid,'✏️ عنوان جدید آموزش را بفرست.'); return
    if data.startswith('training_delete|') and guard(uid,'training'):
        i=int(data.split('|')[1]);
        if i<len(DATA['trainings']): DATA['trainings'].pop(i); save_data('Training delete')
        training_admin(cid); return

    if data=='ad_add' and guard(uid,'ads'): STATES[uid]={'type':'ad_add','step':'username'}; send(cid,'📢 یوزرنیم کانال را بفرست. مثال @Channel'); return
    if data.startswith('ad_edit|') and guard(uid,'ads'):
        i=int(data.split('|')[1]); STATES[uid]={'type':'ad_edit','step':'username','index':i}; send(cid,'📢 یوزرنیم جدید کانال را بفرست.'); return
    if data.startswith('ad_delete|') and guard(uid,'ads'):
        i=int(data.split('|')[1]);
        if i<len(DATA['ads']): DATA['ads'].pop(i); save_data('Ad delete')
        ads_admin(cid); return

    if data=='admin_add':
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند ادمین اضافه کند.')
        STATES[uid]={'type':'admin_add','step':'id'}; send(cid,'🆔 Telegram ID یا @username ادمین را بفرست.'); return
    if data=='admin_role_plus' or data=='admin_role_normal':
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند ادمین اضافه کند.')
        st=STATES.get(uid)
        if not st or st.get('type')!='admin_add': return
        role='plus' if data=='admin_role_plus' else 'normal'
        DATA['admins'][st['aid']]={'name':st['name'],'role':role,'permissions':[]}
        save_data('Admin add'); aid=st['aid']; STATES.pop(uid,None); send(cid,f'✅ ادمین {"پلاس" if role=="plus" else "عادی"} اضافه شد. حالا دسترسی‌ها را مشخص کن.'); admin_permissions_page(cid,aid); return
    if data.startswith('admin_role_toggle|'):
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند نوع ادمین را تغییر دهد.')
        aid=data.split('|',1)[1]; a=DATA.get('admins',{}).get(aid)
        if not a: return
        a['role']='plus' if a.get('role','normal')!='plus' else 'normal'; save_data('Admin role change'); admin_permissions_page(cid,aid); return

    if data.startswith('admin_delete|'):
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند ادمین حذف کند.')
        aid=data.split('|',1)[1]; DATA['admins'].pop(aid,None); save_data('Admin delete'); admins_admin(cid); return
    if data.startswith('admin_perms|'):
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند دسترسی‌ها را تغییر دهد.')
        admin_permissions_page(cid,data.split('|',1)[1]); return
    if data.startswith('admin_perm_toggle|'):
        if not is_owner(uid): return send(cid,'⛔ فقط مالک می‌تواند دسترسی‌ها را تغییر دهد.')
        _,aid,p=data.split('|',2); a=DATA['admins'].get(aid)
        if a:
            perms=a.setdefault('permissions',[])
            if isinstance(perms,dict): perms=[k for k,v in perms.items() if v]; a['permissions']=perms
            if p in perms: perms.remove(p)
            else: perms.append(p)
            save_data('Admin permissions'); admin_permissions_page(cid,aid)
        return
    if data.startswith('toggle_notify|') and guard(uid,'notifications'):
        k=data.split('|',1)[1]; n=DATA['settings']['notifications']; n[k]=not n.get(k,False); save_data('Notifications'); notifications_admin(cid); return

# Repair legacy order messages after all order helpers are defined.
try:
    repair_existing_order_messages()
except Exception as e:
    print('Legacy order repair',e)

# Start the periodic cleanup after all helpers are defined.
start_cleanup_thread()

# ---------- webhook ----------
def is_supported_start_deep_link(text):
    # Accept only known deep-link payloads, never arbitrary text after /start.
    if not isinstance(text,str) or not text.startswith('/start '):
        return False
    payload=text.split(maxsplit=1)[1].strip()
    if not payload:
        return False
    token=payload.lower()
    if token.startswith(('vipcust_','ordercontact_','orderprompt_','orderlogo_','orderinfo_')):
        return True
    pid,sep,vk=payload.partition('_')
    prompt=DATA.get('prompts',{}).get(pid)
    return bool(sep and prompt and vk in prompt.get('variants',{}))


def _process_update(update):
    global CURRENT_CB
    cq=update.get('callback_query')
    if cq:
        CURRENT_CB=cq.get('id'); uid=cq.get('from',{}).get('id'); cid=cq.get('message',{}).get('chat',{}).get('id')
        callback(uid,cid,cq.get('data',''),cq.get('message',{}).get('message_id'),cq.get('id')); return
    m=update.get('message')
    if not m: return
    uid=m.get('from',{}).get('id'); cid=m.get('chat',{}).get('id'); text=(m.get('text') or '').strip()
    remember_user(uid,m)

    # /cancel always cancels the active flow. Other messages are consumed by an active
    # state before any /start/deep-link fallback, so order entry cannot accidentally
    # jump back to the main menu in the middle of contact/payment/photo steps.
    if text=='/cancel':
        STATES.pop(uid,None); POST_STATES.pop(uid,None); persist_order_states_async(); send(cid,'❌ عملیات لغو شد.',main_menu(uid)); return
    # If a user is in an active flow, consume the message there first. If the
    # worker restarted between messages, restore only that user's persisted order
    # state before routing anything through the global /start/menu handler.
    if uid not in STATES:
        persisted=DATA.get('active_order_states',{}).get(str(uid))
        if isinstance(persisted,dict) and persisted.get('type') in ORDER_STATE_TYPES:
            STATES[uid]=deepcopy(persisted)
            ensure_order_flow_state(STATES[uid])
    # Back-office post creation has its own state bucket. Consume it before the
    # generic admin/customer state router so an old admin state cannot steal a
    # photo that belongs to the current post.
    if handle_post_message(uid,cid,m): return
    if uid in STATES:
        if handle_state_message(uid,cid,m): return
    # Only the exact command /start opens the welcome menu. Deep links are
    # accepted separately and only when they match a supported action.
    if text == '/start' or is_supported_start_deep_link(text):
        parts=text.split(maxsplit=1)
        if len(parts)==2:
            token=parts[1].lower()
            if token.startswith('vipcust_'):
                oid=token.split('_',1)[1].upper()
                if not is_owner(uid): return
                ref=DATA.get('vip_customer_refs',{}).get(oid)
                if ref: send(uid,f'👤 {ref.get("vip_label",oid)}\n📱 آیدی/شماره ثبت‌شده مشتری: {ref.get("contact","")}')
                else: send(uid,'❌ اطلاعات مشتری پیدا نشد.')
                return
            if token.startswith('ordercontact_'):
                oid=token.split('_',1)[1].upper(); o=find_order(oid) or next((x for x in DATA.get('completed_orders',[]) if x.get('id')==oid),None)
                if not o: return send(cid,'❌ اطلاعات سفارش پیدا نشد.')
                if not can_private_data(uid): return send(cid,'⛔ این اطلاعات فقط برای مالک و ادمین پلاس دارای دسترسی مجاز است.')
                contact=o.get('contact','') or 'ثبت نشده'
                send(cid,f'💬 اطلاعات تماس سفارش {oid}\n\n📱 آیدی/شماره مشتری:\n{contact}')
                return
            if token.startswith('orderprompt_'):
                oid=token.split('_',1)[1].upper(); o=find_order(oid) or next((x for x in DATA.get('completed_orders',[]) if x.get('id')==oid),None)
                if not o: return send(cid,'❌ اطلاعات سفارش پیدا نشد.')
                if not can_private_data(uid): return send(cid,'⛔ این اطلاعات فقط برای مالک و ادمین پلاس دارای دسترسی مجاز است.')
                if o.get('type')!='customer': return send(cid,'❌ این سفارش پرامپت مشتری ندارد.')
                prompt=o.get('prompt_text','') or 'ثبت نشده'
                send(cid,f'📝 پرامپت مشتری — {oid}\n\n{prompt}')
                return
            if token.startswith('orderlogo_'):
                oid=token.split('_',1)[1].upper(); o=find_order(oid) or next((x for x in DATA.get('completed_orders',[]) if x.get('id')==oid),None)
                if not o: return send(cid,'❌ اطلاعات سفارش پیدا نشد.')
                if not can_private_data(uid): return send(cid,'⛔ این اطلاعات فقط برای مالک و ادمین پلاس دارای دسترسی مجاز است.')
                if o.get('type')!='logo': return send(cid,'❌ این سفارش توضیحات لوگو ندارد.')
                brief=o.get('prompt_text','') or 'ثبت نشده'
                send(cid,f'🎨 توضیحات ساخت لوگو — {oid}\n\n{brief}')
                return
            if token.startswith('orderinfo_'):
                # Backward-compatible legacy link: keep it private, but do not expose it in channels.
                oid=token.split('_',1)[1].upper(); o=find_order(oid) or next((x for x in DATA.get('completed_orders',[]) if x.get('id')==oid),None)
                if not o: return send(cid,'❌ اطلاعات سفارش پیدا نشد.')
                if not can_private_data(uid): return send(cid,'⛔ این اطلاعات فقط برای مالک و ادمین پلاس دارای دسترسی مجاز است.')
                send(cid,'🔐 اطلاعات خصوصی سفارش\n\n'+private_info_text(o))
                return
            pid,_,vk=token.partition('_'); p=DATA.get('prompts',{}).get(pid)
            if p and vk in p.get('variants',{}) and require_membership(cid,uid):
                v=p['variants'][vk]; send(cid,v['prompt'],delivery_kb(v)); return
        if require_membership(cid,uid): send(cid,WELCOME,main_menu(uid))
        return
    # Unsupported /start payloads are not commands and must not fall through
    # to the generic welcome-menu response.
    if text.startswith('/start '): return
    # No generic membership/menu fallback belongs here. Unknown ordinary messages
    # are intentionally silent; membership is checked when the user explicitly
    # starts the bot or presses a menu button.
    # Active state flows have priority over generic fallbacks.
    if handle_state_message(uid,cid,m): return

    # Unknown ordinary messages are intentionally ignored. The welcome menu is
    # opened only by the exact /start command above or by explicit inline-keyboard
    # callbacks. This prevents arbitrary text such as "سلام" from being treated
    # as a start/menu action. Active customer/admin flows were already consumed
    # above, so their free-form input continues to belong to the current step.
    return

# Webhook safety wrapper: Telegram's update_id is unique per bot update.
# This prevents duplicate/retried updates from consuming the next conversation step.
def handle_update(update):
    update_id = update.get('update_id') if isinstance(update, dict) else None
    now = time.monotonic()
    with UPDATE_LOCK:
        # Small bounded TTL cache; old IDs are irrelevant after a few minutes.
        if PROCESSED_UPDATES:
            cutoff = now - PROCESSED_UPDATE_TTL
            stale = [k for k, t in PROCESSED_UPDATES.items() if t < cutoff]
            for k in stale:
                PROCESSED_UPDATES.pop(k, None)
            if len(PROCESSED_UPDATES) > PROCESSED_UPDATE_LIMIT:
                for k in sorted(PROCESSED_UPDATES, key=PROCESSED_UPDATES.get)[:len(PROCESSED_UPDATES)-PROCESSED_UPDATE_LIMIT]:
                    PROCESSED_UPDATES.pop(k, None)

        if update_id is not None and update_id in PROCESSED_UPDATES:
            print(f'Skip duplicate Telegram update: {update_id}')
            return

        if update_id is not None:
            PROCESSED_UPDATES[update_id] = now

        try:
            result=_process_update(update)
            # Persist after the update has been fully consumed, outside the webhook
            # response path. This keeps Telegram responses fast while making the
            # next message recoverable after a worker restart.
            persist_order_states_async()
            persist_conversation_states_async()
            return result
        except Exception:
            # If processing really failed, allow Telegram to retry the update.
            if update_id is not None:
                PROCESSED_UPDATES.pop(update_id, None)
            raise

# delivery keyboard after all helpers are defined
def ai_url(label):
    x=label.lower()
    if 'flow' in x: return FLOW_URL
    if 'gemini' in x or 'nano' in x: return GEMINI_URL
    if 'chatgpt' in x or 'gpt' in x: return CHATGPT_URL
    return ''

def copy_rows(text):
    # Keep the prompt complete in one message. No numbered chunks.
    return []

def delivery_kb(v):
    rows=[]
    u=ai_url(v.get('label',''))
    if u: rows.append([urlbtn('🚀 ورود به مدل',u)])
    rows.append([urlbtn('📢 کانال پرامپتینو',PROMPTINO_CHANNEL)])
    return kb(rows)

@app.get('/')
def home(): return 'Promptino Bot is running ✅',200

@app.post('/webhook')
def webhook():
    handle_update(request.get_json(silent=True) or {}); return 'OK',200

if TOKEN and os.getenv('RENDER_EXTERNAL_URL'):
    try:
        webhook_url=os.getenv('RENDER_EXTERNAL_URL').rstrip('/')+'/webhook'
        print('Webhook:',api('setWebhook',{'url':webhook_url}))
    except Exception as e: print('Webhook setup',e)
