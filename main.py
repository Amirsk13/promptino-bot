import os, json, base64, requests, re
from datetime import datetime
from flask import Flask, request

app = Flask(__name__)
TOKEN=os.getenv('BOT_TOKEN','').strip()
ADMIN_IDS={int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
PROMPTINO_CHANNEL=os.getenv('PROMPTINO_CHANNEL','https://t.me/PromptinoChannel').strip()
OWNER_USERNAME=os.getenv('OWNER_USERNAME','').strip().lstrip('@')
BOT_USERNAME=os.getenv('BOT_USERNAME','').strip().lstrip('@')
GITHUB_TOKEN=os.getenv('GITHUB_TOKEN','').strip(); GITHUB_REPO=os.getenv('GITHUB_REPO','Amirsk13/promptino-bot').strip(); GITHUB_BRANCH=os.getenv('GITHUB_BRANCH','main').strip()
DATA_FILE='promptino_data.json'; CHANNELS_FILE='channels.json'
CHATGPT_URL=os.getenv('CHATGPT_URL','https://chatgpt.com/').strip(); GEMINI_URL=os.getenv('GEMINI_URL','https://gemini.google.com/').strip(); FLOW_URL=os.getenv('FLOW_URL','https://labs.google/fx/tools/flow').strip()
TRAINING_POST_URL=os.getenv('TRAINING_POST_URL',PROMPTINO_CHANNEL).strip(); ORDERS_CHAT=os.getenv('ORDERS_CHAT','').strip(); ARCHIVE_CHAT=os.getenv('ARCHIVE_CHAT','').strip()
API=f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''; GH='https://api.github.com'

# Runtime states. Media files are deliberately not persisted in GitHub.
STATES={}; POST_STATES={}; ADD_CHANNEL_STATES={}

DEFAULT_DATA={
 'prompts':{}, 'vip':{}, 'orders':[], 'reports':[], 'cards':[], 'trainings':[], 'ads':[],
 'admins':{}, 'settings':{'notifications':{'new_order':True,'payment':True,'report':True,'vip_order':True}},
 'counters':{'order':0,'vip':0},
 'messages':{
  'order_created':'✅ سفارش شما با موفقیت ثبت شد و برای ادمین ارسال شد.\n🆔 {order_id}',
  'payment_ok':'💳 پرداخت شما تأیید شد.\n🆔 {order_id}',
  'payment_bad':'❌ پرداخت سفارش {order_id} تأیید نشد.\nاگر فکر می‌کنی مشکلی پیش آمده، از بخش «⚠️ گزارش مشکل» پیام بده و ساعت پرداخت، پرامپت و مدرک پرداخت را ارسال کن.',
  'report_created':'✅ گزارش شما برای ادمین ارسال شد.',
  'report_result':'📩 نتیجه بررسی گزارش شما:\n{result}'
 }
}

def api(method,data=None):
 try:
  if not API:return {}
  r=requests.post(f'{API}/{method}',json=data or {},timeout=25); return r.json()
 except Exception as e: print('API',method,e); return {}

def send(chat_id,text,keyboard=None,parse_mode=None):
 d={'chat_id':chat_id,'text':text}
 if keyboard:d['reply_markup']=keyboard
 if parse_mode:d['parse_mode']=parse_mode
 return api('sendMessage',d)

def send_photo(chat_id,photo,caption='',keyboard=None):
 d={'chat_id':chat_id,'photo':photo,'caption':caption}
 if keyboard:d['reply_markup']=keyboard
 return api('sendPhoto',d)

def answer(cid): api('answerCallbackQuery',{'callback_query_id':cid})
def edit(chat_id,message_id,text,keyboard=None):
 d={'chat_id':chat_id,'message_id':message_id,'text':text}
 if keyboard:d['reply_markup']=keyboard
 return api('editMessageText',d)
def is_admin(uid): return uid in ADMIN_IDS or str(uid) in DATA.get('admins',{})
def github_headers(): return {'Authorization':f'Bearer {GITHUB_TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

def gh_get(path):
 if not GITHUB_TOKEN:return None,None
 r=requests.get(f'{GH}/repos/{GITHUB_REPO}/contents/{path}',headers=github_headers(),params={'ref':GITHUB_BRANCH},timeout=25)
 if r.status_code==404:return None,None
 r.raise_for_status(); j=r.json(); return json.loads(base64.b64decode(j['content']).decode()),j.get('sha')

def gh_save(path,obj,msg):
 if not GITHUB_TOKEN:return False
 try:
  old,sha=gh_get(path); raw=json.dumps(obj,ensure_ascii=False,indent=2).encode(); enc=base64.b64encode(raw).decode()
  d={'message':msg,'content':enc,'branch':GITHUB_BRANCH};
  if sha:d['sha']=sha
  r=requests.put(f'{GH}/repos/{GITHUB_REPO}/contents/{path}',headers=github_headers(),json=d,timeout=25); r.raise_for_status(); return True
 except Exception as e: print('GitHub save',path,e); return False

def load_data():
 try:
  obj,_=gh_get(DATA_FILE)
  if isinstance(obj,dict):
   d=DEFAULT_DATA.copy(); d.update(obj)
   for k,v in DEFAULT_DATA.items():
    if isinstance(v,dict): d.setdefault(k,v.copy())
   return d
 except Exception as e: print('GitHub load data',e)
 return json.loads(json.dumps(DEFAULT_DATA))

def save_data(msg='Update Promptino data'): return gh_save(DATA_FILE,DATA,msg)
DATA=load_data()

def load_channels():
 try:
  x,_=gh_get(CHANNELS_FILE); return x if isinstance(x,list) else []
 except: return []
REQUIRED_CHANNELS=load_channels()
def save_channels(msg): return gh_save(CHANNELS_FILE,REQUIRED_CHANNELS,msg)

def kb(rows): return {'inline_keyboard':rows}
def btn(text,data): return {'text':text,'callback_data':data}
def urlbtn(text,url): return {'text':text,'url':url}
def owner_button(): return urlbtn('👤 ارتباط با مالک',f'https://t.me/{OWNER_USERNAME}') if OWNER_USERNAME else None

def is_member(uid,ch):
 r=api('getChatMember',{'chat_id':ch['username'],'user_id':uid});
 if not r.get('ok'):return False
 s=r.get('result',{}).get('status'); return s in {'creator','administrator','member'} or (s=='restricted' and r.get('result',{}).get('is_member',False))

def require_membership(chat_id,uid):
 miss=[c for c in REQUIRED_CHANNELS if not is_member(uid,c)]
 if not miss:return True
 rows=[[urlbtn(f"📢 عضویت در {c['title']}",c['url'])] for c in miss]; rows.append([btn('✅ بررسی عضویت','check_membership')])
 send(chat_id,'🔒 برای استفاده از این بخش، ابتدا در کانال‌های زیر عضو شو:\n\n'+'\n'.join('• '+c['title'] for c in miss)+'\n\nبعد روی «بررسی عضویت» بزن.',kb(rows)); return False

WELCOME='''🤖 سلام! به پرامپتینو خوش اومدی 👋\n\nاینجا می‌تونی به پرامپت‌های کاربردی و تست‌شده هوش مصنوعی، آموزش‌ها و خدمات ساخت تصویر دسترسی داشته باشی.\n\nاز منوی پایین انتخاب کن:'''

def main_menu(uid):
 rows=[[btn('📚 آموزش','user_training'),btn('👑 VIP','user_vip')],[btn('🖼 سفارش ساخت عکس','order_menu')],[btn('✍️ سفارش با پرامپت مشتری','customer_order')],[btn('⚠️ گزارش مشکل','report_start')],[urlbtn('📢 کانال پرامپتینو',PROMPTINO_CHANNEL)]]
 if is_admin(uid): rows.append([btn('⚙️ مدیریت','admin_menu')])
 return kb(rows)

def next_id(kind):
 DATA['counters'][kind]=int(DATA['counters'].get(kind,0))+1
 return f"{kind.upper()}-{DATA['counters'][kind]}"

def contact_text(m):
 c=m.get('contact'); u=m.get('from',{}); return f"شماره: {c.get('phone_number','')}" if c else (f"@{u.get('username')}" if u.get('username') else f"Telegram ID: {u.get('id')}")

def price_for(key):
 if key in DATA['prompts']: return DATA['prompts'][key].get('price',0)
 if key in DATA['vip']: return DATA['vip'][key].get('price',0)
 return DATA.get('prices',{}).get(key,0)

def money(x):
 try:return f'{int(x):,}'
 except:return str(x)

def admin_menu(uid):
 return kb([[btn('📝 پرامپت‌ها','adm_prompts'),btn('💰 قیمت‌ها','adm_prices')],[btn('👑 VIP','adm_vip'),btn('📦 سفارشات','adm_orders')],[btn('👑 سفارشات VIP','adm_vip_orders'),btn('⚠️ گزارش مشکلات','adm_reports')],[btn('📢 کانال‌ها','adm_channels'),btn('📣 تبلیغات','adm_ads')],[btn('📚 آموزش','adm_training'),btn('🗄 آرشیو','adm_archive')],[btn('👥 ادمین‌ها','adm_admins'),btn('📊 آمار','adm_stats')],[btn('🔔 تنظیمات اعلان‌ها','adm_notifications'),btn('⚙️ تنظیمات','adm_settings')],[btn('➕ افزودن پست','post_start')],[btn('🔙 بازگشت','back_main')]])

def has_perm(uid,perm):
 if uid in ADMIN_IDS:return True
 return DATA.get('admins',{}).get(str(uid),{}).get('permissions',{}).get(perm,False)

def notify_admins(text,keyboard=None,photo=None):
 targets=set(ADMIN_IDS)|set(int(x) for x in DATA.get('admins',{}).keys() if str(x).isdigit())
 for a in targets:
  if photo: send_photo(a,photo,text,keyboard)
  else: send(a,text,keyboard)

def ai_url(label):
 x=label.lower();
 if 'flow' in x:return FLOW_URL
 if 'gemini' in x or 'nano' in x:return GEMINI_URL
 if 'chatgpt' in x or 'gpt' in x:return CHATGPT_URL
 return ''

def delivery_kb(v):
 rows=[[{'text':'📋 کپی پرامپت','copy_text':{'text':v['prompt']}}]]; u=ai_url(v.get('label',''))
 if u:rows.append([urlbtn('🚀 ورود به '+re.sub(r'^[^A-Za-z]*','',v.get('label','')),u)])
 rows.append([urlbtn('📢 کانال پرامپتینو',PROMPTINO_CHANNEL)])
 return kb(rows)

# -------- post builder --------
def post_start(uid):
 POST_STATES[uid]={'step':'photo','buttons':[]}; send(uid,'📝 ساخت پست جدید\n\nمرحله ۱: عکس پست را ارسال کن.\nبرای لغو: /cancel')
def post_buttons(uid): return kb([[btn('➕ اضافه کردن دکمه','post_add'),btn('🔄 اصلاح اطلاعات','post_edit')],[btn('✅ انتشار','post_publish'),btn('❌ لغو','post_cancel')]])
def post_preview(s):
 lines=[f"🔥 پرامپت شماره {s['number']} | {s['name']}"]
 if s.get('suitable'):lines+=['',f"🎯 مناسب: {s['suitable']}"]
 if s.get('for_what'):lines+=['',f"📌 برای: {s['for_what']}"]
 lines+=['','✨ برای نتیجه بهتر:','یک عکس واضح و باکیفیت از خودت به مدل بده.','','⚠️ توجه:','نتیجه نهایی ممکنه بسته به مدل تصویرساز و عکس مرجع کمی متفاوت باشه.']
 return '\n'.join(lines)

def publish_post(uid):
 s=POST_STATES.get(uid)
 if not s or not s.get('photo') or not s.get('buttons'):send(uid,'⚠️ عکس و حداقل یک دکمه لازم است.');return
 pid=f"p{s['number']}"; variants={b['key']:{'label':b['label'],'prompt':b['prompt']} for b in s['buttons']}
 DATA['prompts'][pid]={'title':s['name'],'suitable':s.get('suitable',''),'for':s.get('for_what',''),'price':s.get('price',0),'variants':variants}
 if not save_data(f'Add prompt {pid}'):send(uid,'❌ ذخیره در GitHub انجام نشد.');return
 rows=[[urlbtn(b['label'],f'https://t.me/{BOT_USERNAME}?start={pid}_{b["key"]}')] for b in s['buttons']]
 r=send_photo(PROMPTINO_CHANNEL,s['photo'],post_preview(s),kb(rows))
 if not r.get('ok'):send(uid,'❌ انتشار در کانال ناموفق بود. ربات باید ادمین کانال باشد.');return
 if ARCHIVE_CHAT: send_photo(ARCHIVE_CHAT,s['photo'],f"🗄 آرشیو — {post_preview(s)}\n\n"+'\n\n'.join(f"🔘 {b['label']}\n{b['prompt']}" for b in s['buttons']))
 POST_STATES.pop(uid,None);send(uid,'🎉 پست با موفقیت منتشر و آرشیو شد.')

def post_text(uid,text):
 s=POST_STATES.get(uid)
 if not s:return
 if text.lower()=='/cancel':POST_STATES.pop(uid,None);send(uid,'❌ ساخت پست لغو شد.');return
 step=s['step']
 if step=='name':s['name']=text;s['step']='number';send(uid,'مرحله بعد: شماره پرامپت را بفرست.');return
 if step=='number':
  if not text.isdigit():send(uid,'❌ شماره باید عدد باشد.');return
  s['number']=int(text);s['step']='suitable';send(uid,'🎯 «مناسب» را بنویس. مثال: تبدیل عکس پرتره به تصاویر سینمایی');return
 if step=='suitable':s['suitable']=text;s['step']='for';send(uid,'📌 «برای چیست» را بنویس. مثال: پروفایل');return
 if step=='for':s['for_what']=text;s['step']='buttons';send(uid,'اطلاعات ثبت شد. حالا دکمه‌های مدل را بساز.',post_buttons(uid));return
 if step=='button_label':s['pending_label']=text;s['step']='button_prompt';send(uid,f'🔘 دکمه «{text}» ثبت شد. متن پرامپت را بفرست.');return
 if step=='button_prompt':
  label=s.pop('pending_label'); key=re.sub(r'[^a-z0-9]+','_',label.lower()).strip('_')[:25] or f'v{len(s["buttons"])+1}';s['buttons'].append({'key':key,'label':label,'prompt':text});s['step']='buttons';send(uid,'✅ اضافه شد.',post_buttons(uid));return
 if step=='buttons':send(uid,'از دکمه‌های پایین استفاده کن.',post_buttons(uid))

# -------- orders --------
def start_order(uid,customer_prompt=False):
 STATES[uid]={'type':'customer' if customer_prompt else 'normal','step':'what','items':[],'contact':'','ref_photo':None,'receipt':None}
 send(uid,'🛒 چی می‌خوای؟\n\n'+('پرامپت خودت را کامل ارسال کن.' if customer_prompt else 'شماره پرامپت را بفرست. برای چند پرامپت: 1.2.3')+'\n\n/cancel برای لغو')

def order_text(uid,text):
 s=STATES.get(uid)
 if not s:return
 if text.lower()=='/cancel':STATES.pop(uid,None);send(uid,'❌ سفارش لغو شد.');return
 if s['step']=='what':
  if s['type']=='customer':s['prompt_text']=text;s['price']=price_for('customer_prompt');s['step']='confirm_price';send(uid,f'💰 قیمت سفارش: {money(s["price"])} تومان\n\nتأیید می‌کنی؟',kb([[btn('✅ تأیید','ord_price_ok'),btn('❌ لغو','ord_cancel')]]));return
  ids=[x.strip() for x in text.split('.') if x.strip()]
  if not ids or any(x not in DATA['prompts'] for x in ids):send(uid,'❌ شماره پرامپت پیدا نشد.');return
  s['prompt_ids']=ids;s['variants']=[]
  for pid in ids:
   p=DATA['prompts'][pid]
   for k,v in p.get('variants',{}).items():s['variants'].append((pid,k,v))
  rows=[[btn(f'{pid} | {v["label"]}',f'ord_variant|{pid}|{k}')] for pid,k,v in s['variants']]
  rows.append([btn('❌ لغو','ord_cancel')]);s['step']='variants';send(uid,'مدل/نسخه موردنظر را انتخاب کن:',kb(rows));return
 if s['step']=='contact':
  s['contact']=text;s['step']='card';send(uid,'💳 کارت مقصد را انتخاب کن:',card_kb());return
 if s['step']=='report_contact':return

def card_kb():
 rows=[]
 for i,c in enumerate(DATA.get('cards',[])):rows.append([btn(f"💳 {c['bank']} | {c['number']}",f'card|{i}')])
 rows.append([btn('❌ لغو','ord_cancel')]);return kb(rows)

def order_price_confirm(uid):
 s=STATES.get(uid);s['step']='contact';send(uid,'📱 آیدی تلگرام یا شماره خود را قرار دهید.');
def finish_order(uid):
 s=STATES[uid]; oid=next_id('order'); now=datetime.now().strftime('%Y-%m-%d %H:%M')
 item=s.get('prompt_text') or ', '.join(s.get('prompt_ids',[]))
 o={'id':oid,'user_id':uid,'contact':s['contact'],'item':item,'price':s['price'],'card':s.get('card'),'status':'🟡 در حال انجام','created_at':now,'payment':'pending'}
 DATA['orders'].append(o);save_data(f'New order {oid}')
 # Order media goes to Orders; media ids are not stored in GitHub.
 if ORDERS_CHAT:
  send(ORDERS_CHAT,f"🆔 {oid}\n🛒 سفارش: {item}\n💰 قیمت: {money(o['price'])}\n👤 {s['contact']}\n🕐 {now}\n📌 وضعیت: {o['status']}",kb([[btn('✅ تأیید پرداخت',f'pay_ok|{oid}'),btn('❌ رد پرداخت',f'pay_bad|{oid}')],[btn('🟡 در حال انجام',f'status|{oid}|doing'),btn('🔵 تحویل داده شد',f'status|{oid}|delivered')],[btn('🟢 رضایت دریافت شد',f'status|{oid}|satisfied'),btn('🔴 مشکل دارد',f'status|{oid}|problem')],[btn('💬 پیام به مشتری',f'msg_user|{oid}')]]))
  if s.get('ref_photo'):send_photo(ORDERS_CHAT,s['ref_photo'],f'📸 عکس مرجع — {oid}')
  if s.get('receipt'):send_photo(ORDERS_CHAT,s['receipt'],f'🧾 فیش پرداخت — {oid}')
 STATES.pop(uid,None);send(uid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid))

# -------- reports/admin operations --------
def start_report(uid): STATES[uid]={'type':'report','step':'text'};send(uid,'سلام 👋\nمشکلت را کامل توضیح بده.');
def report_text(uid,text):
 s=STATES[uid]
 if s['step']=='text':s['description']=text;s['step']='contact';send(uid,'📱 آیدی تلگرام یا شماره خود را قرار دهید.');return
 if s['step']=='contact':
  rid=f'R-{len(DATA["reports"])+1}';r={'id':rid,'user_id':uid,'description':s['description'],'contact':text,'status':'در حال بررسی','created_at':datetime.now().strftime('%Y-%m-%d %H:%M'),'result':''};DATA['reports'].append(r);save_data(f'New report {rid}')
  if ORDERS_CHAT:send(ORDERS_CHAT,f'⚠️ یک گزارش مشکل جدید ثبت شد\n🆔 {rid}\n👤 {text}')
  STATES.pop(uid,None);send(uid,DATA['messages']['report_created'],main_menu(uid))

def admin_text(uid,text):
 st=STATES.get(uid)
 if not st:return False
 if text.lower()=='/cancel':STATES.pop(uid,None);send(uid,'❌ لغو شد.');return True
 typ,step=st.get('type'),st.get('step')
 if typ=='card':
  if step=='name':st['name']=text;st['step']='bank';send(uid,'🏦 نام بانک؟');return True
  if step=='bank':st['bank']=text;st['step']='number';send(uid,'💳 شماره کارت؟');return True
  if step=='number':st['number']=text;DATA['cards'].append({'name':st['name'],'bank':st['bank'],'number':text});save_data('Add card');STATES.pop(uid,None);send(uid,'✅ کارت ذخیره شد.',admin_menu(uid));return True
 if typ=='price':
  key=st['key'];
  if text.isdigit():
   if key.startswith('vip'):DATA['vip'].setdefault(key,{});DATA['vip'][key]['price']=int(text)
   elif key in DATA['prompts']:DATA['prompts'][key]['price']=int(text)
   else:DATA.setdefault('prices',{})[key]=int(text)
   save_data('Update price');STATES.pop(uid,None);send(uid,'✅ قیمت ذخیره شد.',admin_menu(uid));return True
  send(uid,'❌ قیمت باید عدد باشد.');return True
 if typ=='msg_user':
  send(int(st['user_id']),text);STATES.pop(uid,None);send(uid,'✅ پیام ارسال شد.',admin_menu(uid));return True
 if typ=='report_result':
  r=next((x for x in DATA['reports'] if x['id']==st['rid']),None)
  if r:r['result']=text;r['status']='پاسخ داده شد';save_data('Report result');send(r['user_id'],DATA['messages']['report_result'].format(result=text));
  STATES.pop(uid,None);send(uid,'✅ نتیجه ارسال شد.',admin_menu(uid));return True
 return False

# -------- callbacks --------
def callback(uid,chat_id,data,msgid):
 answer(CURRENT_CB); 
 if data=='check_membership':
  if require_membership(chat_id,uid):send(chat_id,'✅ عضویت تأیید شد.');return
 if data=='back_main':send(chat_id,WELCOME,main_menu(uid));return
 if data=='admin_menu':send(chat_id,'⚙️ مدیریت',admin_menu(uid));return
 if data=='user_training':
  if DATA.get('trainings'):rows=[[urlbtn(t['title'],t['url'])] for t in DATA['trainings']]
  else:rows=[[urlbtn('📚 آموزش Promptino',TRAINING_POST_URL)]]
  send(chat_id,'📚 آموزش:',kb(rows));return
 if data=='user_vip':
  rows=[]
  for k,v in DATA.get('vip',{}).items():rows.append([btn(f"{k} | {v.get('title','') } — {money(v.get('price',0))}",f'vip_buy|{k}')])
  send(chat_id,'👑 VIP\n\nیک مورد را انتخاب کن:',kb(rows) if rows else None);return
 if data=='order_menu':start_order(uid);return
 if data=='customer_order':start_order(uid,True);return
 if data=='report_start':start_report(uid);return
 if data=='ord_cancel':STATES.pop(uid,None);send(chat_id,'❌ لغو شد.',main_menu(uid));return
 if data=='ord_price_ok':order_price_confirm(uid);return
 if data.startswith('ord_variant|'):
  _,pid,k=data.split('|',2);s=STATES[uid];v=DATA['prompts'][pid]['variants'][k];s['items']=[(pid,k)];s['price']=DATA['prompts'][pid].get('price',0);s['step']='contact';send(chat_id,f"💰 قیمت: {money(s['price'])} تومان\n\n📱 آیدی تلگرام یا شماره خود را قرار دهید.");return
 if data.startswith('card|'):
  i=int(data.split('|')[1]);s=STATES[uid];s['card']=DATA['cards'][i];s['step']='receipt';send(chat_id,f"💳 شماره کارت {s['card']['number']}\nبه نام {s['card']['name']}\n\n🧾 فیش پرداخت را ارسال کن.");return
 if data.startswith('vip_buy|'):
  k=data.split('|',1)[1];v=DATA['vip'][k];STATES[uid]={'type':'vip','step':'contact','vip':k,'price':v.get('price',0),'contact':''};send(chat_id,f"👑 {k} | {v.get('title','')}\n💰 قیمت: {money(v.get('price',0))} تومان\n\n📱 آیدی تلگرام یا شماره خود را قرار دهید.");return
 if data.startswith('pay_ok|') or data.startswith('pay_bad|'):
  oid=data.split('|')[1];o=next((x for x in DATA['orders'] if x['id']==oid),None)
  if not o:return
  ok=data.startswith('pay_ok');o['payment']='approved' if ok else 'rejected';save_data(f'Payment {oid}')
  send(o['user_id'],(DATA['messages']['payment_ok'] if ok else DATA['messages']['payment_bad']).format(order_id=oid))
  send(chat_id,'✅ انجام شد.' if ok else '❌ پرداخت رد شد.');return
 if data.startswith('status|'):
  _,oid,st=data.split('|');o=next((x for x in DATA['orders'] if x['id']==oid),None);labels={'doing':'🟡 در حال انجام','delivered':'🔵 تحویل داده شد','satisfied':'🟢 رضایت دریافت شد','problem':'🔴 مشکل دارد'}
  if o:o['status']=labels.get(st,st);save_data(f'Status {oid}');send(chat_id,f'✅ وضعیت {oid}: {o["status"]}')
  return
 if data.startswith('msg_user|'):
  oid=data.split('|')[1];o=next((x for x in DATA['orders'] if x['id']==oid),None)
  if o:STATES[uid]={'type':'msg_user','step':'text','user_id':o['user_id']};send(chat_id,'💬 متن پیام به مشتری را بفرست.')
  return
 if data.startswith('report_reply|'):
  rid=data.split('|')[1];r=next((x for x in DATA['reports'] if x['id']==rid),None)
  if r:STATES[uid]={'type':'report_result','rid':rid};send(chat_id,'📩 پاسخ ادمین را بفرست.')
  return
 if data=='adm_channels':channel_admin(chat_id);return
 if data=='adm_cards':cards_admin(chat_id);return
 if data=='adm_reports':reports_admin(chat_id);return
 if data=='adm_orders':orders_admin(chat_id);return
 if data=='adm_vip_orders':vip_orders_admin(chat_id);return
 if data=='adm_prompts':prompts_admin(chat_id);return
 if data=='adm_prices':prices_admin(chat_id);return
 if data=='adm_vip':vip_admin(chat_id);return
 if data=='adm_training':training_admin(chat_id);return
 if data=='adm_ads':ads_admin(chat_id);return
 if data=='adm_admins':admins_admin(chat_id);return
 if data=='adm_stats':send(chat_id,f"📊 آمار\nپرامپت‌ها: {len(DATA['prompts'])}\nVIP: {len(DATA['vip'])}\nسفارش‌ها: {len(DATA['orders'])}\nگزارش‌ها: {len(DATA['reports'])}",admin_menu(uid));return
 if data=='adm_notifications':notifications_admin(chat_id);return
 if data=='adm_settings':settings_admin(chat_id);return
 if data=='adm_archive':send(chat_id,'🗄 آرشیو از طریق کانال خصوصی Archive مدیریت می‌شود.');return
 if data=='system_messages': system_messages(chat_id);return
 if data.startswith('setprice|'):
  key=data.split('|',1)[1]; STATES[uid]={'type':'price','step':'value','key':key};send(chat_id,f'💰 قیمت جدید برای {key} را به تومان بفرست.');return
 if data.startswith('view_order|'):
  oid=data.split('|',1)[1];o=next((x for x in DATA['orders'] if x['id']==oid),None)
  if o: send(chat_id,f"🆔 {o['id']}\n🛒 {o['item']}\n💰 {money(o['price'])} تومان\n👤 {o['contact']}\n🕐 {o['created_at']}\n📌 {o['status']}\n💳 پرداخت: {o.get('payment','pending')}",kb([[btn('💬 پیام به مشتری',f'msg_user|{oid}')],[btn('🔙 سفارشات','adm_orders')]]))
  return
 if data=='vip_add':
  STATES[uid]={'type':'vip_add','step':'title'};send(chat_id,'👑 عنوان VIP را بفرست. مثال: Cinematic Portrait');return
 if data=='vip_edit':send(chat_id,'✏️ ویرایش VIP از طریق انتخاب VIP و سپس تغییر اطلاعات انجام می‌شود.');return
 if data=='vip_delete':send(chat_id,'🗑 حذف VIP: در این نسخه برای جلوگیری از حذف اشتباه، حذف از طریق ویرایش داده انجام می‌شود.');return
 if data=='adm_prompt_add':
  POST_STATES[uid]={'step':'name','buttons':[]};send(chat_id,'📝 نام پرامپت جدید را بفرست.');return
 if data in {'adm_prompt_edit','adm_prompt_text','adm_prompt_delete'}:
  STATES[uid]={'type':'prompt_manage','action':data,'step':'id'};send(chat_id,'شماره پرامپت را بفرست. مثال: 4');return
 if data=='post_start':post_start(uid);return
 if data=='post_add':
  s=POST_STATES.get(uid);s['step']='button_label';send(uid,'🔘 نام دکمه را بفرست.');return
 if data=='post_publish':publish_post(uid);return
 if data=='post_cancel':POST_STATES.pop(uid,None);send(uid,'❌ ساخت پست لغو شد.',admin_menu(uid));return
 if data=='post_edit':send(uid,'برای اصلاح، ساخت پست را لغو و دوباره شروع کن.');return
 if data=='add_card':STATES[uid]={'type':'card','step':'name'};send(uid,'👤 به نام؟');return
 if data=='add_channel':ADD_CHANNEL_STATES[uid]={'step':'username'};send(uid,'📢 یوزرنیم کانال؟ مثال @Channel');return
 if data.startswith('toggle_notify|'):
  k=data.split('|')[1];DATA['settings']['notifications'][k]=not DATA['settings']['notifications'].get(k,False);save_data('Notification setting');notifications_admin(chat_id);return

# -------- admin pages --------
def prompts_admin(cid):
 rows=[[btn('➕ افزودن','adm_prompt_add')],[btn('✏️ ویرایش اطلاعات','adm_prompt_edit'),btn('🔄 تغییر پرامپت','adm_prompt_text')],[btn('🗑 حذف','adm_prompt_delete')],[btn('🔙 مدیریت','admin_menu')]];send(cid,'📝 پرامپت‌ها',kb(rows))
def prices_admin(cid):
 rows=[]
 for k,v in DATA['prompts'].items():rows.append([btn(f'{k} | {money(v.get("price",0))} تومان',f'setprice|{k}')])
 for k,v in DATA['vip'].items():rows.append([btn(f'{k} | {money(v.get("price",0))} تومان',f'setprice|{k}')])
 rows.append([btn('✍️ customer_prompt','setprice|customer_prompt'),btn('🔙 مدیریت','admin_menu')]);send(cid,'💰 قیمت‌ها',kb(rows))
def vip_admin(cid):send(cid,'👑 VIP',kb([[btn('➕ افزودن VIP','vip_add'),btn('✏️ ویرایش','vip_edit')],[btn('🗑 حذف','vip_delete')],[btn('🔙 مدیریت','admin_menu')]]))
def orders_admin(cid):
 rows=[]
 for o in DATA['orders'][-30:]:rows.append([btn(f"{o['id']} | {o['status']}",f'view_order|{o["id"]}')])
 send(cid,'📦 سفارشات',kb(rows+[[btn('🔙 مدیریت','admin_menu')]]))
def vip_orders_admin(cid):
 vs=[o for o in DATA['orders'] if o.get('type')=='vip']
 send(cid,'👑 سفارشات VIP\n\n'+('\n'.join(f"{o['id']} | {o['item']} | {o['status']}" for o in vs) if vs else 'موردی نیست.'),admin_menu(cid))
def reports_admin(cid):
 rows=[[btn(f"{r['id']} | {r['status']}",f'report_reply|{r["id"]}') for r in DATA['reports'][-20:]]]
 flat=[x for row in rows for x in row];send(cid,'⚠️ گزارش مشکلات',kb([[x] for x in flat]+[[btn('🔙 مدیریت','admin_menu')]]))
def cards_admin(cid):send(cid,'💳 کارت‌ها\n\n'+('\n'.join(f"{i+1}. {c['bank']} — {c['number']} — {c['name']}" for i,c in enumerate(DATA['cards'])) if DATA['cards'] else 'کارت ثبت نشده.'),kb([[btn('➕ افزودن کارت','add_card')],[btn('🔙 تنظیمات','adm_settings')]]))
def channel_admin(cid):send(cid,'📢 کانال‌ها\n\n'+('\n'.join(f"{i+1}. {c['title']} — {c['username']}" for i,c in enumerate(REQUIRED_CHANNELS)) if REQUIRED_CHANNELS else 'کانال اجباری ثبت نشده.'),kb([[btn('➕ افزودن کانال','add_channel')],[btn('🗑 حذف با command','admin_menu')],[btn('🔙 مدیریت','admin_menu')]]))
def training_admin(cid):send(cid,'📚 آموزش\n\n'+('\n'.join(f"{i+1}. {t['title']}" for i,t in enumerate(DATA['trainings'])) if DATA['trainings'] else 'آموزشی ثبت نشده.'),admin_menu(cid))
def ads_admin(cid):send(cid,'📣 تبلیغات\n\n'+('\n'.join(f"{i+1}. {a.get('title','')}" for i,a in enumerate(DATA['ads'])) if DATA['ads'] else 'کانال تبلیغاتی ثبت نشده.'),admin_menu(cid))
def admins_admin(cid):send(cid,'👥 ادمین‌ها\n\n'+('\n'.join(f"{x} — {v.get('name','')}" for x,v in DATA['admins'].items()) if DATA['admins'] else 'ادمین معمولی ثبت نشده.'),admin_menu(cid))
def notifications_admin(cid):
 n=DATA['settings']['notifications'];labels={'new_order':'سفارش جدید','payment':'تغییر پرداخت','report':'گزارش مشکل','vip_order':'سفارش VIP'};send(cid,'🔔 تنظیمات اعلان‌ها',kb([[btn(('🟢 ' if n.get(k) else '🔴 ')+v,f'toggle_notify|{k}')] for k,v in labels.items()]+[[btn('🔙 مدیریت','admin_menu')]]))
def settings_admin(cid):send(cid,'⚙️ تنظیمات',kb([[btn('💳 کارت‌ها','adm_cards')],[btn('💬 پیام‌های سیستم','system_messages')],[btn('🔙 مدیریت','admin_menu')]]))

def system_messages(cid):send(cid,'💬 پیام‌های سیستم\n\nبرای ویرایش پیام‌ها از پنل کدنویسی/تنظیمات داده استفاده می‌شود.',kb([[btn('🔙 تنظیمات','adm_settings')]]))

# extra callbacks for menus/states

def handle_update(update):
 global CURRENT_CB
 cb=update.get('callback_query')
 if cb:
  CURRENT_CB=cb.get('id'); uid=cb.get('from',{}).get('id'); cid=cb.get('message',{}).get('chat',{}).get('id'); callback(uid,cid,cb.get('data',''),cb.get('message',{}).get('message_id'));return
 m=update.get('message');
 if not m:return
 uid=m.get('from',{}).get('id');cid=m.get('chat',{}).get('id');text=m.get('text','')
 if is_admin(uid) and uid in POST_STATES:
  s=POST_STATES[uid]
  if s['step']=='photo':
   p=m.get('photo',[])
   if p:s['photo']=p[-1]['file_id'];s['step']='name';send(cid,'📸 عکس دریافت شد.\n\nنام پست را بفرست.');return
  post_text(uid,text);return
 if is_admin(uid) and uid in ADD_CHANNEL_STATES:
  st=ADD_CHANNEL_STATES[uid]
  if st['step']=='username':st['username']=text;st['step']='title';send(cid,'📝 عنوان کانال؟');return
  if st['step']=='title':st['title']=text;st['step']='url';send(cid,'🔗 لینک کانال؟');return
  if st['step']=='url':REQUIRED_CHANNELS.append({'username':st['username'],'title':st['title'],'url':text});save_channels('Add required channel');ADD_CHANNEL_STATES.pop(uid,None);send(cid,'✅ کانال اضافه شد.',admin_menu(uid));return
 if uid in STATES:
  s=STATES[uid]
  if s.get('type')=='vip_add':
   if s['step']=='title': s['title']=text;s['step']='price';send(uid,'💰 قیمت VIP را به تومان بفرست.');return
   if s['step']=='price' and text.isdigit(): s['price']=int(text);s['step']='prompt';send(uid,'✍️ متن کامل پرامپت VIP را بفرست.');return
   if s['step']=='prompt':
    key=f"VIP-{len(DATA['vip'])+1}";DATA['vip'][key]={'title':s['title'],'price':s['price'],'prompt':text};save_data(f'Add {key}');STATES.pop(uid,None);send(uid,f'✅ {key} اضافه شد.',admin_menu(uid));return
   send(uid,'❌ مقدار نامعتبر است.');return
  if s.get('type')=='prompt_manage':
    if s['step']=='id':
     pid=text.strip().lower();pid=pid if pid.startswith('p') else 'p'+pid
     if pid not in DATA['prompts']:send(uid,'❌ پرامپت پیدا نشد.');return
     s['pid']=pid;s['step']='value'
     if s['action']=='adm_prompt_delete':
      DATA['prompts'].pop(pid,None);save_data(f'Delete {pid}');STATES.pop(uid,None);send(uid,'🗑 حذف شد.',admin_menu(uid));return
     send(uid,'متن/اطلاعات جدید را بفرست.');return
    if s['step']=='value':
     p=DATA['prompts'][s['pid']]
     if s['action']=='adm_prompt_edit':p['title']=text
     else:
      vars=list(p.get('variants',{}).items())
      if not vars:send(uid,'❌ این پرامپت variant ندارد.');return
      vars[0][1]['prompt']=text
     save_data(f'Update {s["pid"]}');STATES.pop(uid,None);send(uid,'✅ ذخیره شد.',admin_menu(uid));return
  if s.get('type')=='report':
    if m.get('photo') and s.get('step')=='contact':s['evidence']=m['photo'][-1]['file_id'];return
    report_text(uid,text);return
  if s.get('type')=='msg_user':admin_text(uid,text);return
  if s.get('type') in {'normal','customer'}:
   if s.get('step')=='receipt' and m.get('photo'):
    s['receipt']=m['photo'][-1]['file_id'];s['step']='reference';send(cid,'📸 حالا عکس مرجع را ارسال کن.');return
   if s.get('step')=='reference' and m.get('photo'):
    s['ref_photo']=m['photo'][-1]['file_id'];finish_order(uid);return
   order_text(uid,text);return
  if s.get('type')=='vip':
   if s['step']=='contact':s['contact']=text;s['step']='card';send(cid,'💳 کارت مقصد را انتخاب کن:',card_kb());return
   if s['step']=='receipt' and m.get('photo'):s['receipt']=m['photo'][-1]['file_id'];s['step']='reference';send(cid,'📸 عکس مرجع را ارسال کن.');return
   if s['step']=='reference' and m.get('photo'):
    oid=next_id('vip');DATA['orders'].append({'id':oid,'type':'vip','vip':s['vip'],'item':s['vip'],'price':s['price'],'user_id':uid,'contact':s['contact'],'status':'🟡 در حال انجام','payment':'pending','created_at':datetime.now().strftime('%Y-%m-%d %H:%M')});save_data(f'New VIP {oid}')
    if ORDERS_CHAT:send(ORDERS_CHAT,f'👑 یک سفارش VIP ثبت شد — {oid}')
    for a in set(ADMIN_IDS):
     send(a,f'👑 سفارش VIP {oid}\n{ s["vip"] }\n💰 {money(s["price"])}\n👤 {s["contact"]}')
     if s.get('receipt'):send_photo(a,s['receipt'],f'🧾 فیش VIP — {oid}')
     if s.get('ref_photo'):send_photo(a,s['ref_photo'],f'📸 عکس مرجع VIP — {oid}')
    STATES.pop(uid,None);send(cid,DATA['messages']['order_created'].format(order_id=oid),main_menu(uid));return
   return
 if text=='/myid':send(cid,f'🆔 Telegram ID شما:\n{uid}');return
 if text=='/cancel':STATES.pop(uid,None);POST_STATES.pop(uid,None);ADD_CHANNEL_STATES.pop(uid,None);send(cid,'❌ عملیات لغو شد.');return
 if text.startswith('/start'):
  parts=text.split(maxsplit=1)
  if len(parts)==2:
   token=parts[1].lower();pid,_,vk=token.partition('_');p=DATA['prompts'].get(pid)
   if p:
    v=p.get('variants',{}).get(vk)
    if v and require_membership(cid,uid):send(cid,v['prompt'],delivery_kb(v));return
  send(cid,WELCOME,main_menu(uid));return
 if text=='/price':
  send(cid,'برای تغییر قیمت از پنل مدیریت وارد «💰 قیمت‌ها» شو.');return
 if text=='/post' and is_admin(uid):post_start(uid);return
 if text=='/channels' and is_admin(uid):channel_admin(cid);return
 if text.startswith('/removechannel') and is_admin(uid):
  name=text.split(maxsplit=1)[1] if len(text.split())>1 else ''; REQUIRED_CHANNELS[:]=[c for c in REQUIRED_CHANNELS if c['username'].lower()!=name.lower()];save_channels('Remove required channel');send(cid,'✅ انجام شد.',admin_menu(uid));return
 send(cid,WELCOME,main_menu(uid))

# monkey-patched callback additions kept here to avoid sprawling handlers
def _old_callback(): pass

@app.get('/')
def home():return 'Promptino Bot is running ✅',200
@app.post('/webhook')
def webhook():handle_update(request.get_json(silent=True) or {});return 'OK',200

if TOKEN and os.getenv('RENDER_EXTERNAL_URL'):
 try:
  u=os.getenv('RENDER_EXTERNAL_URL').rstrip('/')+'/webhook';print('Webhook:',api('setWebhook',{'url':u}))
 except Exception as e:print('Webhook setup',e)
