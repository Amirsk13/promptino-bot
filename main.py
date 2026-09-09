import os, json, base64, requests, re
from copy import deepcopy
from datetime import datetime
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
ARCHIVE_CHAT = os.getenv('ARCHIVE_CHAT','').strip()
TESTIMONIALS_CHAT = os.getenv('TESTIMONIALS_CHAT','').strip()
API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
GH = 'https://api.github.com'

# All conversational state is kept here. Every text/photo message checks active state first.
STATES = {}
POST_STATES = {}
CURRENT_CB = None

PERMISSIONS = ['prompts','prices','vip','orders','vip_orders','reports','ads','training','admins','stats','notifications','settings']
DEFAULT_DATA = {
    'prompts': {},
    'vip': {},
    'orders': [],
    'reports': [],
    'cards': [],
    'trainings': [],
    'ads': [],
    'admins': {},
    'prices': {'customer_prompt': 0},
    'settings': {'notifications': {'new_order': True, 'payment': True, 'report': True, 'vip_order': True}},
    'counters': {'order': 0, 'vip': 0, 'report': 0},
    'messages': {
        'order_created': '✅ سفارش ثبت شد و برای ادمین ارسال شد.\n🆔 {order_id}',
        'payment_ok': '✅ پرداخت شما تأیید شد.\n🆔 {order_id}',
        'payment_bad': '❌ پرداخت سفارش {order_id} تأیید نشد.\n\nلطفاً ساعت پرداخت، شماره پرامپت و مدرک پرداخت را بررسی کنید و در صورت نیاز از بخش «⚠️ گزارش مشکل» پیام بدهید.',
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

def save_data(msg='Promptino update'): return gh_save(DATA_FILE, DATA, msg)
DATA = load_data()

def is_owner(uid): return uid in ADMIN_IDS
def is_admin(uid): return is_owner(uid) or str(uid) in DATA.get('admins',{})
def has_perm(uid,perm):
    if is_owner(uid): return True
    p=DATA.get('admins',{}).get(str(uid),{}).get('permissions',[])
    if isinstance(p,dict): return bool(p.get(perm))
    return perm in p

def guard(uid,perm): return is_admin(uid) and has_perm(uid,perm)

def main_menu(uid):
    report_btn = btn('⚠️ گزارش مشکلات','adm_reports') if guard(uid,'reports') else btn('⚠️ گزارش مشکل','report_start')
    rows=[[btn('📚 آموزش استفاده پرامپت','user_training'),btn('👑 VIP','user_vip')],
          [btn('🖼 سفارش ساخت عکس','order_menu')],
          [btn('✍️ سفارش ساخت عکس با پرامپت مشتری','customer_order')],
          [report_btn],
          [urlbtn('📢 کانال پرامپتینو',PROMPTINO_CHANNEL)]]
    if is_admin(uid): rows.append([btn('⚙️ مدیریت','admin_menu')])
    return kb(rows)

WELCOME='🤖 سلام! به پرامپتینو خوش اومدی 👋\n\nاز منوی زیر انتخاب کن:'

def admin_menu(uid):
    rows=[]
    candidates=[
        ('prompts','📝 پرامپت‌ها','adm_prompts'),('prices','💰 قیمت‌ها','adm_prices'),
        ('vip','👑 VIP','adm_vip'),('orders','📦 سفارشات','adm_orders'),
        ('vip_orders','📦 سفارشات VIP','adm_vip_orders'),('reports','⚠️ گزارش مشکلات','adm_reports'),
        ('ads','📣 تبلیغات','adm_ads'),('training','📚 آموزش','adm_training'),
        ('admins','👥 ادمین‌ها','adm_admins'),('stats','📊 آمار','adm_stats'),
        ('notifications','🔔 تنظیمات اعلان‌ها','adm_notifications'),('settings','⚙️ تنظیمات','adm_settings')]
    visible=[(label,cb) for perm,label,cb in candidates if guard(uid,perm)]
    for i in range(0,len(visible),2): rows.append([btn(*x) for x in visible[i:i+2]])
    if guard(uid,'prompts'): rows.append([btn('➕ افزودن پست','post_start')])
    rows.append([btn('🔙 بازگشت','back_main')])
    return kb(rows)

def next_id(kind):
    DATA.setdefault('counters',{})[kind]=int(DATA.get('counters',{}).get(kind,0))+1
    prefix={'order':'ORD','vip':'VIP','report':'R'}.get(kind,kind.upper())
    return f'{prefix}-{DATA["counters"][kind]}'

def price_for(key):
    if key in DATA.get('prompts',{}): return int(DATA['prompts'][key].get('price',0) or 0)
    if key in DATA.get('vip',{}): return int(DATA['vip'][key].get('price',0) or 0)
    return int(DATA.setdefault('prices',{}).get(key,0) or 0)

def normalize_pid(x):
    x=x.strip().lower()
    return x if x.startswith('p') else 'p'+x

def vip_label(k,v):
    n=re.sub(r'\D','',k) or k.replace('VIP-','')
    return f'VIP{n} | {v.get("title","")}'

def find_order(oid): return next((o for o in DATA.get('orders',[]) if o.get('id')==oid),None)
def find_report(rid): return next((r for r in DATA.get('reports',[]) if r.get('id')==rid),None)

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
        send(cid,'📚 آموزش استفاده پرامپت',kb(rows)); return
    send(cid,'📚 آموزش استفاده پرامپت',kb([[urlbtn('📖 مشاهده آموزش استفاده پرامپت',TRAINING_POST_URL)]]))

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
        rows.append([btn(f'⚙️ {aid} | {a.get("name","")}',f'admin_perms|{aid}'),btn('🗑 حذف',f'admin_delete|{aid}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'👥 ادمین‌ها',kb(rows))

def admin_permissions_page(cid,aid):
    a=DATA.get('admins',{}).get(str(aid))
    if not a: return send(cid,'❌ ادمین پیدا نشد.')
    perms=a.setdefault('permissions',[])
    if isinstance(perms,dict): perms=[k for k,v in perms.items() if v]; a['permissions']=perms
    rows=[[btn(('🟢 ' if p in perms else '🔴 ')+p,f'admin_perm_toggle|{aid}|{p}')] for p in PERMISSIONS]
    rows.append([btn('🔙 ادمین‌ها','adm_admins')]); send(cid,f'⚙️ دسترسی‌های ادمین {aid}',kb(rows))

# ---------- cards ----------
def cards_admin(cid):
    lines=['💳 کارت‌ها','']
    for i,c in enumerate(DATA.get('cards',[]),1): lines.append(f'{i}. {c.get("bank","")} — {c.get("number","")} — به نام {c.get("name","")}')
    if len(lines)==2: lines.append('کارت ثبت نشده.')
    rows=[[btn('➕ افزودن کارت','card_add')]]
    for i,_ in enumerate(DATA.get('cards',[])): rows.append([btn(f'✏️ ویرایش کارت {i+1}',f'card_edit|{i}'),btn('🗑 حذف',f'card_delete|{i}')])
    rows.append([btn('🔙 تنظیمات','adm_settings')]); send(cid,'\n'.join(lines),kb(rows))

def card_kb():
    rows=[]
    for i,c in enumerate(DATA.get('cards',[])):
        rows.append([btn(f'💳 {c.get("bank","")} | {c.get("number","")}',f'card|{i}')])
    rows.append([btn('❌ لغو','ord_cancel')]); return kb(rows)

# ---------- prices / vip ----------
def prices_admin(cid):
    rows=[]
    for k,v in DATA.get('prompts',{}).items(): rows.append([btn(f'{k.replace("p","")} | {money(v.get("price",0))} تومان',f'setprice|{k}')])
    for k,v in DATA.get('vip',{}).items(): rows.append([btn(f'{vip_label(k,v)} | {money(v.get("price",0))} تومان',f'setprice|{k}')])
    rows.append([btn(f'✍️ سفارش با پرامپت مشتری | {money(price_for("customer_prompt"))} تومان','setprice|customer_prompt')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'💰 قیمت‌ها',kb(rows))

def vip_admin(cid):
    rows=[[btn('➕ افزودن VIP','vip_add')]]
    for k,v in DATA.get('vip',{}).items(): rows.append([btn(f'✏️ {vip_label(k,v)}',f'vip_edit|{k}'),btn('🗑 حذف',f'vip_delete|{k}')])
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'👑 VIP',kb(rows))

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
    if ORDERS_CHAT: send(ORDERS_CHAT,'⚠️ یک گزارش مشکل جدید ثبت شد')
    STATES.pop(uid,None); send(uid,DATA['messages']['report_created'],main_menu(uid))

def reports_admin(cid):
    rows=[[btn(f'{r.get("id")} | {r.get("status","در حال بررسی")}',f'report_view|{r.get("id")}')] for r in DATA.get('reports',[])[-30:]]
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'⚠️ گزارش مشکلات',kb(rows))

def report_detail(cid,rid):
    r=find_report(rid)
    if not r: return send(cid,'❌ گزارش پیدا نشد.')
    text=f'⚠️ گزارش {rid}\n\n📝 مشکل:\n{r.get("description","")}\n\n📱 تماس: {r.get("contact","")}\n🕐 تاریخ: {r.get("created_at","")}\n📌 وضعیت: {r.get("status","")}'
    if r.get('result'): text+=f'\n\n💬 آخرین پاسخ:\n{r["result"]}'
    send(cid,text,kb([[btn('💬 پاسخ به کاربر',f'report_reply|{rid}')],[btn('🔙 گزارش‌ها','adm_reports')]]))

# ---------- orders ----------
def start_order(uid,customer=False):
    STATES[uid]={'type':'customer' if customer else 'normal','step':'what','items':[],'contact':'','receipt':None,'ref_photo':None}
    if customer: send(uid,'✍️ پرامپت خودت را کامل بفرست.\n\n/cancel برای لغو')
    else: send(uid,'🛒 شماره پرامپت را بفرست. مثال: 4 یا برای چند مورد 1.2.3\n\n/cancel برای لغو')

def order_text(uid,text):
    s=STATES[uid]
    if s['step']=='what':
        if s['type']=='customer':
            s['prompt_text']=text; s['price']=price_for('customer_prompt'); s['step']='confirm_price'
            send(uid,f'💰 قیمت سفارش: {money(s["price"])} تومان\n\nتأیید می‌کنی؟',kb([[btn('✅ تأیید','ord_price_ok'),btn('❌ لغو','ord_cancel')]])); return
        raw=[x.strip() for x in text.split('.') if x.strip()]
        pids=[normalize_pid(x) for x in raw]
        if not pids or any(p not in DATA.get('prompts',{}) for p in pids): return send(uid,'❌ شماره پرامپت پیدا نشد.')
        # Current UI selects one variant; for one requested prompt this is exact. For many, first selection is accepted as existing behavior.
        s['prompt_ids']=pids; rows=[]
        for pid in pids:
            for k,v in DATA['prompts'][pid].get('variants',{}).items(): rows.append([btn(f'{pid[1:]} | {v.get("label",k)}',f'ord_variant|{pid}|{k}')])
        rows.append([btn('❌ لغو','ord_cancel')]); s['step']='variants'; send(uid,'مدل/نسخه موردنظر را انتخاب کن:',kb(rows)); return
    if s['step']=='contact':
        s['contact']=text; s['step']='card'; send(uid,'💳 کارت مقصد را انتخاب کن:',card_kb()); return

def order_price_confirm(uid):
    s=STATES.get(uid)
    if not s: return
    s['step']='contact'; send(uid,'📱 آیدی تلگرام یا شماره خود را قرار دهید')

def finish_order(uid):
    s=STATES[uid]; oid=next_id('order'); now=datetime.now().strftime('%Y-%m-%d %H:%M')
    item=s.get('prompt_text') or ', '.join(p[1:] if p.startswith('p') else p for p in s.get('prompt_ids',[]))
    o={'id':oid,'type':s['type'],'user_id':uid,'contact':s['contact'],'item':item,'price':s.get('price',0),'card':s.get('card'),'status':'🟡 در حال انجام','payment':'pending','created_at':now}
    DATA['orders'].append(o); save_data(f'New order {oid}')
    if ORDERS_CHAT:
        # Reference image intentionally comes before receipt.
        if s.get('ref_photo'): send_photo(ORDERS_CHAT,s['ref_photo'],f'📸 عکس مرجع — {oid}')
        if s.get('receipt'): send_photo(ORDERS_CHAT,s['receipt'],f'🧾 فیش پرداخت — {oid}')
        send(ORDERS_CHAT,f'🆔 {oid}\n🛒 سفارش: {item}\n💰 قیمت: {money(o["price"])} تومان\n👤 {s["contact"]}\n🕐 {now}\n📌 وضعیت: {o["status"]}',order_action_kb(oid))
    STATES.pop(uid,None); send(uid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid))

def order_action_kb(oid):
    return kb([[btn('✅ تأیید پرداخت',f'pay_ok|{oid}'),btn('❌ رد پرداخت',f'pay_bad|{oid}')],
               [btn('🟡 در حال انجام',f'status|{oid}|doing'),btn('🔵 تحویل داده شد',f'status|{oid}|delivered')],
               [btn('🟢 رضایت دریافت شد',f'status|{oid}|satisfied'),btn('🔴 مشکل دارد',f'status|{oid}|problem')],
               [btn('💬 پیام به مشتری',f'msg_user|{oid}')]])

def orders_admin(cid, vip_only=False):
    items=[o for o in DATA.get('orders',[]) if (o.get('type')=='vip')==vip_only]
    rows=[[btn(f'{o.get("id")} | {o.get("status")}',f'view_order|{o.get("id")}')] for o in items[-30:]]
    rows.append([btn('🔙 مدیریت','admin_menu')]); send(cid,'📦 سفارشات VIP' if vip_only else '📦 سفارشات',kb(rows))

# ---------- post builder ----------
def post_start(uid):
    POST_STATES[uid]={'step':'photos','photos':[],'buttons':[]}
    send(uid,'🖼 عکس پست را ارسال کن. می‌توانی چند عکس پشت سر هم بفرستی؛ وقتی تمام شد روی «✅ اتمام عکس‌ها» بزن.',kb([[btn('✅ اتمام عکس‌ها','post_photos_done')],[btn('❌ لغو','post_cancel')]]))

def post_preview(s):
    lines=[f'🔥 پرامپت شماره {s["number"]} | {s["name"]}']
    if s.get('suitable'): lines+=['',f'🎯 مناسب: {s["suitable"]}']
    if s.get('for_what'): lines+=['',f'📌 برای: {s["for_what"]}']
    lines+=['','✨ برای نتیجه بهتر:','یک عکس واضح و باکیفیت از خودت به مدل بده.','','⚠️ توجه:','نتیجه نهایی ممکنه بسته به مدل تصویرساز و عکس مرجع کمی متفاوت باشه.']
    return '\n'.join(lines)

def post_buttons():
    return kb([[btn('➕ اضافه کردن دکمه','post_add')],[btn('✅ انتشار','post_publish'),btn('❌ لغو','post_cancel')]])

def publish_post(uid):
    s=POST_STATES.get(uid)
    if not s or not s.get('photos') or not s.get('buttons'): return send(uid,'⚠️ حداقل یک عکس و یک دکمه لازم است.')
    pid=f'p{s["number"]}'
    DATA['prompts'][pid]={'title':s['name'],'suitable':s.get('suitable',''),'for':s.get('for_what',''),'price':DATA.get('prompts',{}).get(pid,{}).get('price',0),'variants':{b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']}}
    save_data(f'Publish {pid}')
    # Use callback buttons instead of bot deep-links. This works even when BOT_USERNAME
    # is not configured and lets the bot reliably deliver the selected prompt.
    links=kb([[btn(b['label'],f'getprompt|{pid}|{b["key"]}')] for b in s['buttons']])

    # Telegram does not allow inline keyboards on sendMediaGroup albums. For a single
    # photo we attach the buttons directly; for multiple photos we publish the album
    # first and then a dedicated text message containing the buttons.
    if len(s['photos'])==1:
        result=send_photo(PROMPTINO_CHAT,s['photos'][0],post_preview(s),links)
    else:
        media=[{'type':'photo','media':p} for p in s['photos']]
        result=api('sendMediaGroup',{'chat_id':PROMPTINO_CHAT,'media':media})
        if result.get('ok'):
            result=send(PROMPTINO_CHAT,post_preview(s),links)
    if not result.get('ok'):
        send(uid,'❌ انتشار در کانال ناموفق بود. ربات باید در کانال ادمین باشد و Chat ID کانال درست باشد.')
        return

    # Archive: keep the same photos, then store every variant separately so each
    # prompt has its own title and a native Telegram copy button.
    if ARCHIVE_CHAT:
        media=[{'type':'photo','media':p} for p in s['photos']]
        album_result=api('sendMediaGroup',{'chat_id':ARCHIVE_CHAT,'media':media})
        if not album_result.get('ok'):
            print('Archive album failed:',album_result)
        send(ARCHIVE_CHAT,post_preview(s))
        for b in s['buttons']:
            copy_kb=kb([[{'text':'📋 کپی پرامپت','copy_text':{'text':b['prompt']}}]])
            archive_text=f'🔘 {b["label"]}\n\n{b["prompt"]}'
            send(ARCHIVE_CHAT,archive_text,copy_kb)
    POST_STATES.pop(uid,None); send(uid,'🎉 پست با موفقیت منتشر شد.',admin_menu(uid))

# ---------- admin pages ----------
def prompts_admin(cid):
    send(cid,'📝 پرامپت‌ها',kb([[btn('➕ افزودن پست','post_start')],[btn('✏️ ویرایش عنوان','prompt_edit'),btn('🔄 تغییر پرامپت','prompt_text')],[btn('🗑 حذف','prompt_delete')],[btn('🔙 مدیریت','admin_menu')]]))

def notifications_admin(cid):
    n=DATA['settings']['notifications']; labels={'new_order':'سفارش جدید','payment':'پرداخت','report':'گزارش مشکل','vip_order':'سفارش VIP'}
    send(cid,'🔔 تنظیمات اعلان‌ها',kb([[btn(('🟢 ' if n.get(k) else '🔴 ')+label,f'toggle_notify|{k}')] for k,label in labels.items()]+[[btn('🔙 مدیریت','admin_menu')]]))

def settings_admin(cid): send(cid,'⚙️ تنظیمات',kb([[btn('💳 کارت‌ها','adm_cards')],[btn('🔙 مدیریت','admin_menu')]]))

# ---------- STATE ROUTER ----------
def handle_state_message(uid,cid,m):
    """Return True if the active state consumed the message."""
    if uid not in STATES: return False
    s=STATES[uid]; text=(m.get('text') or '').strip(); photo=m.get('photo') or []
    if text=='/cancel': STATES.pop(uid,None); send(cid,'❌ عملیات لغو شد.',main_menu(uid)); return True
    typ=s.get('type'); step=s.get('step')

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
        save_data(f'Price {key}'); STATES.pop(uid,None); send(cid,f'✅ قیمت جدید ذخیره شد: {money(value)} تومان'); prices_admin(cid); return True

    if typ=='vip_add':
        if not text: return True
        if step=='title': s['title']=text; s['step']='price'; send(cid,'💰 قیمت VIP را بفرست.'); return True
        if step=='price':
            clean=re.sub(r'[,٬\s]','',text)
            if not clean.isdigit(): send(cid,'❌ قیمت باید عدد باشد.'); return True
            s['price']=int(clean); s['step']='prompt'; send(cid,'✍️ متن کامل پرامپت VIP را بفرست.'); return True
        if step=='prompt':
            nums=[int(re.sub(r'\D','',k)) for k in DATA.get('vip',{}) if re.sub(r'\D','',k)]
            key=f'VIP-{max(nums,default=0)+1}'; DATA['vip'][key]={'title':s['title'],'price':s['price'],'prompt':text}; save_data(f'Add {key}')
            STATES.pop(uid,None); send(cid,f'✅ {vip_label(key,DATA["vip"][key])} اضافه شد.'); vip_admin(cid); return True

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
                if ORDERS_CHAT: send_photo(ORDERS_CHAT,s['evidence'],'📎 مدرک گزارش مشکل')
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
            if not text.isdigit(): send(cid,'❌ Telegram ID باید عدد باشد.'); return True
            s['aid']=text; s['step']='name'; send(cid,'👤 نام ادمین را بفرست.'); return True
        if step=='name':
            DATA['admins'][s['aid']]={'name':text,'permissions':[]}; save_data('Admin add'); aid=s['aid']; STATES.pop(uid,None); send(cid,'✅ ادمین اضافه شد. حالا دسترسی‌ها را مشخص کن.'); admin_permissions_page(cid,aid); return True

    if typ in {'normal','customer'}:
        if step=='receipt':
            if not photo: send(cid,'🧾 لطفاً عکس فیش پرداخت را ارسال کن.'); return True
            s['receipt']=photo[-1]['file_id']; s['step']='reference'; send(cid,'📸 حالا عکس مرجع را ارسال کن.'); return True
        if step=='reference':
            if not photo: send(cid,'📸 لطفاً عکس مرجع را ارسال کن.'); return True
            s['ref_photo']=photo[-1]['file_id']; finish_order(uid); return True
        if text: order_text(uid,text); return True
        return True

    if typ=='vip_order':
        if step=='description': s['description']=text; s['step']='contact'; send(cid,'📱 آیدی تلگرام یا شماره خود را قرار دهید'); return True
        if step=='contact': s['contact']=text; s['step']='card'; send(cid,'💳 کارت مقصد را انتخاب کن:',card_kb()); return True
        if step=='receipt':
            if not photo: send(cid,'🧾 عکس فیش را ارسال کن.'); return True
            s['receipt']=photo[-1]['file_id']; s['step']='reference'; send(cid,'📸 عکس مرجع را ارسال کن.'); return True
        if step=='reference':
            if not photo: send(cid,'📸 عکس مرجع را ارسال کن.'); return True
            s['ref_photo']=photo[-1]['file_id']; oid=next_id('vip'); now=datetime.now().strftime('%Y-%m-%d %H:%M')
            o={'id':oid,'type':'vip','user_id':uid,'contact':s['contact'],'item':s['vip'],'description':s.get('description',''),'price':s['price'],'status':'🟡 در حال انجام','payment':'pending','created_at':now}
            DATA['orders'].append(o); save_data(f'VIP order {oid}')
            if ORDERS_CHAT: send(ORDERS_CHAT,f'👑 یک سفارش VIP ثبت شد — {oid}')
            for a in ADMIN_IDS:
                send(a,f'👑 سفارش VIP {oid}\n{vip_label(s["vip"],DATA["vip"][s["vip"]])}\n💰 {money(s["price"])} تومان\n👤 {s["contact"]}\n📝 {s.get("description","")}',order_action_kb(oid))
                if s.get('ref_photo'): send_photo(a,s['ref_photo'],f'📸 عکس مرجع VIP — {oid}')
                if s.get('receipt'): send_photo(a,s['receipt'],f'🧾 فیش VIP — {oid}')
            STATES.pop(uid,None); send(cid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid)); return True
    return True

def handle_post_message(uid,cid,m):
    if uid not in POST_STATES: return False
    s=POST_STATES[uid]; text=(m.get('text') or '').strip(); photos=m.get('photo') or []
    if text=='/cancel': POST_STATES.pop(uid,None); send(cid,'❌ ساخت پست لغو شد.',admin_menu(uid)); return True
    step=s['step']
    if step=='photos':
        if photos: s['photos'].append(photos[-1]['file_id']); send(cid,f'✅ عکس {len(s["photos"])} دریافت شد. عکس بعدی را بفرست یا «اتمام عکس‌ها» را بزن.'); return True
        send(cid,'🖼 لطفاً عکس ارسال کن یا «اتمام عکس‌ها» را بزن.'); return True
    if not text: return True
    if step=='name': s['name']=text; s['step']='number'; send(cid,'🔢 شماره پرامپت؟'); return True
    if step=='number':
        if not text.isdigit(): send(cid,'❌ شماره باید عدد باشد.'); return True
        s['number']=int(text); s['step']='suitable'; send(cid,'🎯 مناسب برای چه کاری؟'); return True
    if step=='suitable': s['suitable']=text; s['step']='for'; send(cid,'📌 در بخش «برای» چه بنویسم؟'); return True
    if step=='for': s['for_what']=text; s['step']='buttons'; send(cid,'✅ اطلاعات ثبت شد. حالا دکمه و پرامپت را اضافه کن.',post_buttons()); return True
    if step=='button_label': s['pending_label']=text; s['step']='button_prompt'; send(cid,'✍️ متن کامل پرامپت این دکمه را بفرست.'); return True
    if step=='button_prompt':
        label=s.pop('pending_label'); key=re.sub(r'[^a-z0-9]+','_',label.lower()).strip('_')[:25] or f'v{len(s["buttons"])+1}'
        # avoid key collisions
        base=key; n=2
        while any(b['key']==key for b in s['buttons']): key=f'{base}_{n}'; n+=1
        s['buttons'].append({'key':key,'label':label,'prompt':text}); s['step']='buttons'; send(cid,'✅ دکمه و پرامپت ذخیره شد.',post_buttons()); return True
    send(cid,'از دکمه‌های زیر استفاده کن.',post_buttons()); return True

def view_order(cid,oid):
    o=find_order(oid)
    if not o: return send(cid,'❌ سفارش پیدا نشد.')
    send(cid,f'🆔 {o["id"]}\n🛒 {o.get("item","")}\n💰 {money(o.get("price",0))} تومان\n👤 {o.get("contact","")}\n🕐 {o.get("created_at","")}\n📌 {o.get("status","")}\n💳 پرداخت: {o.get("payment","pending")}',order_action_kb(oid))

# ---------- callback router ----------
def callback(uid,cid,data,msgid=None):
    answer(CURRENT_CB)
    if data=='check_membership':
        if require_membership(cid,uid): send(cid,'✅ عضویت تأیید شد.',main_menu(uid))
        return
    if data=='back_main': send(cid,WELCOME,main_menu(uid)); return
    if data=='admin_menu': send(cid,'مدیریت',admin_menu(uid)); return
    if data=='user_training': training_user(cid); return
    if data.startswith('training_view|'):
        i=int(data.split('|')[1]); t=DATA['trainings'][i]; send(cid,f'📚 {t.get("title","")}\n\n{t.get("text","")}'); return
    if data=='user_vip': vip_user(cid); return
    if data=='order_menu': start_order(uid); return
    if data=='customer_order': start_order(uid,True); return
    if data=='report_start': start_report(uid); return
    if data=='report_skip_evidence':
        s=STATES.get(uid)
        if s and s.get('type')=='report' and s.get('step')=='evidence': save_report(uid,s)
        return
    if data=='ord_cancel': STATES.pop(uid,None); send(cid,'❌ لغو شد.',main_menu(uid)); return
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
    if data=='ord_price_ok': order_price_confirm(uid); return
    if data.startswith('ord_variant|'):
        _,pid,k=data.split('|',2); s=STATES.get(uid)
        if not s: return
        s['selected_variant']=(pid,k); s['price']=price_for(pid); s['step']='contact'; send(cid,f'💰 قیمت: {money(s["price"])} تومان\n\n📱 آیدی تلگرام یا شماره خود را قرار دهید'); return
    if data.startswith('card|'):
        s=STATES.get(uid); i=int(data.split('|')[1])
        if not s or i>=len(DATA.get('cards',[])): return
        s['card']=DATA['cards'][i]; s['step']='receipt'; send(cid,f'💳 شماره کارت: {s["card"]["number"]}\nبه نام: {s["card"]["name"]}\n\n🧾 فیش پرداخت را ارسال کن.'); return
    if data.startswith('vip_buy|'):
        k=data.split('|',1)[1]; v=DATA.get('vip',{}).get(k)
        if not v: return send(cid,'❌ VIP پیدا نشد.')
        STATES[uid]={'type':'vip_order','step':'description','vip':k,'price':price_for(k)}
        send(cid,f'👑 {vip_label(k,v)}\n💰 قیمت: {money(price_for(k))} تومان\n\n📝 دقیقاً چه تصویری می‌خواهی؟'); return
    if data.startswith('pay_ok|') or data.startswith('pay_bad|'):
        if not guard(uid,'orders'): return send(cid,'⛔ دسترسی نداری.')
        oid=data.split('|')[1]; o=find_order(oid)
        if not o: return
        ok=data.startswith('pay_ok'); o['payment']='approved' if ok else 'rejected'; save_data(f'Payment {oid}')
        send(o['user_id'],(DATA['messages']['payment_ok'] if ok else DATA['messages']['payment_bad']).format(order_id=oid)); send(cid,'✅ پرداخت تأیید شد.' if ok else '❌ پرداخت رد شد.'); return
    if data.startswith('status|'):
        if not guard(uid,'orders'): return send(cid,'⛔ دسترسی نداری.')
        _,oid,st=data.split('|'); o=find_order(oid); labels={'doing':'🟡 در حال انجام','delivered':'🔵 تحویل داده شد','satisfied':'🟢 رضایت دریافت شد','problem':'🔴 مشکل دارد'}
        if o: o['status']=labels.get(st,st); save_data(f'Status {oid}'); send(cid,f'✅ وضعیت: {o["status"]}')
        return
    if data.startswith('msg_user|'):
        if not guard(uid,'orders'): return send(cid,'⛔ دسترسی نداری.')
        oid=data.split('|')[1]; o=find_order(oid)
        if o: STATES[uid]={'type':'msg_user','step':'text','user_id':o['user_id'],'oid':oid}; send(cid,f'💬 متن پیام به مشتری {oid} را بفرست.\n\nاطلاعات تماس ثبت‌شده: {o.get("contact","")}')
        return
    if data.startswith('view_order|'): view_order(cid,data.split('|',1)[1]); return

    # Admin menu callbacks
    if data.startswith('adm_') or data in {'post_start','prompt_edit','prompt_text','prompt_delete'}:
        if not is_admin(uid): return send(cid,'⛔ دسترسی نداری.')
    if data=='adm_prompts' and guard(uid,'prompts'): prompts_admin(cid); return
    if data=='adm_prices' and guard(uid,'prices'): prices_admin(cid); return
    if data=='adm_vip' and guard(uid,'vip'): vip_admin(cid); return
    if data=='adm_orders' and guard(uid,'orders'): orders_admin(cid,False); return
    if data=='adm_vip_orders' and guard(uid,'vip_orders'): orders_admin(cid,True); return
    if data=='adm_reports' and guard(uid,'reports'): reports_admin(cid); return
    if data=='adm_ads' and guard(uid,'ads'): ads_admin(cid); return
    if data=='adm_training' and guard(uid,'training'): training_admin(cid); return
    if data=='adm_admins' and guard(uid,'admins'): admins_admin(cid); return
    if data=='adm_stats' and guard(uid,'stats'): send(cid,f'📊 آمار\nپرامپت‌ها: {len(DATA["prompts"])}\nVIP: {len(DATA["vip"])}\nسفارش‌ها: {len(DATA["orders"])}\nگزارش‌ها: {len(DATA["reports"])}',admin_menu(uid)); return
    if data=='adm_notifications' and guard(uid,'notifications'): notifications_admin(cid); return
    if data=='adm_settings' and guard(uid,'settings'): settings_admin(cid); return
    if data=='adm_cards' and guard(uid,'settings'): cards_admin(cid); return

    if data.startswith('setprice|'):
        if not guard(uid,'prices'): return send(cid,'⛔ دسترسی نداری.')
        key=data.split('|',1)[1]; STATES[uid]={'type':'price','step':'value','key':key}; send(cid,f'💰 قیمت جدید برای {key} را به تومان بفرست.'); return
    if data=='card_add' and guard(uid,'settings'):
        STATES[uid]={'type':'card_manage','step':'name','index':None}; send(cid,'👤 به نام؟'); return
    if data.startswith('card_edit|') and guard(uid,'settings'):
        i=int(data.split('|')[1]); STATES[uid]={'type':'card_manage','step':'name','index':i}; send(cid,'👤 نام صاحب کارت را بفرست.'); return
    if data.startswith('card_delete|') and guard(uid,'settings'):
        i=int(data.split('|')[1]);
        if i<len(DATA['cards']): DATA['cards'].pop(i); save_data('Card delete')
        cards_admin(cid); return

    if data=='vip_add' and guard(uid,'vip'): STATES[uid]={'type':'vip_add','step':'title'}; send(cid,'👑 عنوان VIP را بفرست.'); return
    if data.startswith('vip_edit|') and guard(uid,'vip'):
        key=data.split('|',1)[1]; STATES[uid]={'type':'vip_edit','step':'title','key':key}; send(cid,'✏️ عنوان جدید VIP را بفرست.'); return
    if data.startswith('vip_delete|') and guard(uid,'vip'):
        key=data.split('|',1)[1]; DATA['vip'].pop(key,None); save_data(f'Delete {key}'); send(cid,'🗑 VIP حذف شد.'); vip_admin(cid); return

    if data=='prompt_edit' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'title','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return
    if data=='prompt_text' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'text','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return
    if data=='prompt_delete' and guard(uid,'prompts'): STATES[uid]={'type':'prompt_manage','action':'delete','step':'id'}; send(cid,'🔢 شماره پرامپت را بفرست.'); return

    if data=='post_start' and guard(uid,'prompts'): post_start(uid); return
    if data=='post_photos_done' and guard(uid,'prompts'):
        s=POST_STATES.get(uid)
        if not s or not s.get('photos'): return send(cid,'⚠️ حداقل یک عکس بفرست.')
        s['step']='name'; send(cid,'📝 نام پست را بفرست.'); return
    if data=='post_add' and guard(uid,'prompts'):
        s=POST_STATES.get(uid)
        if s: s['step']='button_label'; send(cid,'🔘 نام دکمه را بفرست. مثال: Gemini'); return
    if data=='post_publish' and guard(uid,'prompts'): publish_post(uid); return
    if data=='post_cancel': POST_STATES.pop(uid,None); send(cid,'❌ ساخت پست لغو شد.',admin_menu(uid)); return

    if data.startswith('report_view|') and guard(uid,'reports'): report_detail(cid,data.split('|',1)[1]); return
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
        STATES[uid]={'type':'admin_add','step':'id'}; send(cid,'🆔 Telegram ID ادمین را بفرست.'); return
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

# ---------- webhook ----------
def handle_update(update):
    global CURRENT_CB
    cq=update.get('callback_query')
    if cq:
        CURRENT_CB=cq.get('id'); uid=cq.get('from',{}).get('id'); cid=cq.get('message',{}).get('chat',{}).get('id')
        callback(uid,cid,cq.get('data',''),cq.get('message',{}).get('message_id')); return
    m=update.get('message')
    if not m: return
    uid=m.get('from',{}).get('id'); cid=m.get('chat',{}).get('id'); text=(m.get('text') or '').strip()

    # Commands that must always be recognized.
    if text=='/cancel':
        STATES.pop(uid,None); POST_STATES.pop(uid,None); send(cid,'❌ عملیات لغو شد.',main_menu(uid)); return
    if text.startswith('/start'):
        # /start is only run when the actual message starts with /start. It never catches state text.
        parts=text.split(maxsplit=1)
        if len(parts)==2:
            token=parts[1].lower(); pid,_,vk=token.partition('_'); p=DATA.get('prompts',{}).get(pid)
            if p and vk in p.get('variants',{}) and require_membership(cid,uid):
                v=p['variants'][vk]; send(cid,v['prompt'],delivery_kb(v)); return
        if require_membership(cid,uid): send(cid,WELCOME,main_menu(uid))
        return
    if text=='/myid': send(cid,f'🆔 Telegram ID شما:\n{uid}'); return

    # Active state and post flows have priority over generic fallbacks.
    if handle_post_message(uid,cid,m): return
    if handle_state_message(uid,cid,m): return

    if text=='/post' and guard(uid,'prompts'): post_start(uid); return
    if text=='/price' and guard(uid,'prices'): prices_admin(cid); return
    if require_membership(cid,uid): send(cid,WELCOME,main_menu(uid))

# delivery keyboard after all helpers are defined
def ai_url(label):
    x=label.lower()
    if 'flow' in x: return FLOW_URL
    if 'gemini' in x or 'nano' in x: return GEMINI_URL
    if 'chatgpt' in x or 'gpt' in x: return CHATGPT_URL
    return ''

def delivery_kb(v):
    rows=[[{'text':'📋 کپی پرامپت','copy_text':{'text':v.get('prompt','')}}]]
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
