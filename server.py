#!/usr/bin/env python3
"""DevArena: Python standard-library LAN service with JSON storage."""
import argparse, copy, hashlib, hmac, json, os, pathlib, secrets, threading, uuid, base64, time, urllib.request, urllib.error, ssl, logging
from http.cookies import SimpleCookie
from urllib.parse import urlencode
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, quote

ROOT=pathlib.Path(__file__).resolve().parent
DATA=ROOT/'data'; UPLOADS=DATA/'uploads'; STATE=DATA/'state.json'
LOCK=threading.RLock(); MAX_JSON=5*1024*1024
# File limits are configured via app_config.json; no database is required.
APP_CONFIG_FILE=ROOT/'app_config.json'
def load_app_config():
    default={'max_upload_mb':8192}
    if APP_CONFIG_FILE.exists():
        values=json.loads(APP_CONFIG_FILE.read_text(encoding='utf-8'))
        if not isinstance(values,dict):raise SystemExit('app_config.json 必须是 JSON 对象')
        default.update(values)
    mb=default['max_upload_mb']
    if isinstance(mb,bool) or not isinstance(mb,int) or not (1<=mb<=65536):
        raise SystemExit('app_config.json 的 max_upload_mb 必须是 1 到 65536 之间的整数')
    return default
APP_CONFIG=load_app_config()
MAX_FILE=APP_CONFIG['max_upload_mb']*1024*1024
FILE_CHUNK=4*1024*1024
ADMIN_PASSWORD='dbzz123'
DATA.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True)
# OAuth2 / OIDC Authorization Code + PKCE; credentials never sent to the browser.
OAUTH_FILE=ROOT/'oauth_config.json'
OAUTH_DEFAULT={'enabled':False,'client_id':'','client_secret':'','authorization_endpoint':'','token_endpoint':'','userinfo_endpoint':'','redirect_uri':'','scopes':'openid profile email','identity_claim':'sub','name_claim':'name','employee_id_claim':'employee_id','admin_subjects':[],'admin_employee_ids':[],'token_auth_method':'client_secret_basic','session_hours':8,'cookie_secure':True}
def config():
    cfg=dict(OAUTH_DEFAULT)
    if OAUTH_FILE.exists():cfg.update(json.loads(OAUTH_FILE.read_text('utf-8')))
    return cfg
OAUTH=config()
if OAUTH['enabled']:
    for k in ('client_id','authorization_endpoint','token_endpoint','userinfo_endpoint','redirect_uri'):
        if not OAUTH.get(k):raise SystemExit(f'OAuth 配置缺少 {k}')
    for k in ('authorization_endpoint','token_endpoint','userinfo_endpoint','redirect_uri'):
        if urlsplit(OAUTH[k]).scheme!='https':raise SystemExit(f'启用 OAuth 时 {k} 必须使用 HTTPS')
    if OAUTH.get('token_auth_method') not in ('client_secret_basic','client_secret_post','none'):raise SystemExit('不支持的 token_auth_method')
SESSIONS={}; AUTH_PENDING={}; ATTEMPT_LOCK=LOCK

def iso_utc():return datetime.now().astimezone().isoformat(timespec='seconds')
def timestamp():return time.time()
def public_auth_state():return {'enabled':bool(OAUTH['enabled']),'loginUrl':'/auth/login' if OAUTH['enabled'] else None}
def get_claim(obj,key):
    value=obj
    for part in str(key).split('.'):
        if not isinstance(value,dict):return ''
        value=value.get(part)
    return str(value).strip() if value is not None else ''
def remote_json(url,form=None,headers=None):
    payload=urlencode(form).encode() if form is not None else None
    req=urllib.request.Request(url,data=payload,headers=headers or {},method='POST' if form is not None else 'GET')
    with urllib.request.urlopen(req,timeout=12,context=ssl.create_default_context()) as res:
        if res.status!=200:raise ValueError('认证服务异常')
        data=res.read(1024*1024)
    return json.loads(data)

if not STATE.exists(): STATE.write_text(json.dumps({'exams':[],'submissions':[],'files':[],'attempts':[]},ensure_ascii=False,indent=2),'utf-8')

def load(): return json.loads(STATE.read_text('utf-8'))
def save(db):
    temp=DATA/('state-'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(db,ensure_ascii=False,indent=2),'utf-8')
    os.replace(temp,STATE)
def sanitized(db,admin):
    result=copy.deepcopy(db)
    if not admin:
        result['exams']=[e for e in result['exams'] if e.get('published')]
        for e in result['exams']:
            for q in e['qs']:
                if not e.get('answersReleased'): q.pop('answer',None);q.pop('explain',None)
        result['leaderboard']=[{'name':v['name'],'total':v['total'],'n':v['n']} for v in sorted((lambda scores:list(scores.values()))(aggregate_scores(db)),key=lambda x:-x['total'])]
        result['submissions']=[]  # never expose IDs, raw answers, or individual submissions
        result.pop('attempts',None) # ongoing exams include sensitive employee identifiers
        result['files']=[f for f in result['files'] if f.get('kind')!='submission' and (f.get('kind')!='answer' or any(e['id']==f['examId'] and e.get('answersReleased') for e in result['exams']))]
        result['files']=[f for f in result['files'] if any(e['id']==f['examId'] for e in result['exams'])]
    return result

def aggregate_scores(db):
    users={}
    for s in db['submissions']:
        if not s.get('graded'):continue
        key=s['employeeId']
        if key not in users:users[key]={'name':s['name'],'total':0,'n':0}
        users[key]['total']+=s['score'];users[key]['n']+=1
    return users

def scoring(e,answers):
    total=0
    for q in e['qs']:
        if q['type']=='text': continue
        user=str(answers.get(q['id'],''))
        if q['type']=='multi':
            def canonical(s):return ','.join(sorted(set(x.strip() for x in s.split(',') if x.strip()),key=lambda x:int(x)))
            try: correct=canonical(user)==canonical(str(q.get('answer','')))
            except ValueError:correct=False
        else:correct=user==str(q.get('answer',''))
        if correct:total+=float(q['score'])
    return total

def good_exam(e):
    if not isinstance(e,dict) or not str(e.get('title','')).strip() or not e.get('start') or not e.get('end'):raise ValueError('请填写考试名称和时间')
    if datetime.fromisoformat(e['end'])<=datetime.fromisoformat(e['start']):raise ValueError('结束时间须晚于开始时间')
    qs=e.get('qs',[])
    if not isinstance(qs,list) or len(qs)>200:raise ValueError('题目数量不合法')
    for q in qs:
        if q.get('type') not in ('single','multi','text') or not str(q.get('title','')).strip() or float(q.get('score',0))<=0:raise ValueError('题目类型、内容或分值错误')
        # Optional fields: legacy JSON exams remain readable.
        for field in ('description','scenario','requirements','deliverable','rubric','knowledge','difficulty','explain'):
            value=q.get(field,'')
            if not isinstance(value,str) or len(value)>20000:
                raise ValueError(f'题目字段 {field} 格式错误或内容过长')
        if not isinstance(q.get('attachments',[]),list):raise ValueError('附件字段无效')
        if q['type']!='text':
            opts=q.get('opts',[])
            if not isinstance(opts,list) or len(opts)<2:raise ValueError('选择题至少需要两个选项')
            ids=str(q.get('answer','')).split(',')
            if any(not x.strip().isdigit() or int(x.strip())>=len(opts) for x in ids):raise ValueError('正确答案索引无效')
            if q['type']=='single' and len(ids)!=1:raise ValueError('单选题只能有一个正确选项')

class Handler(BaseHTTPRequestHandler):
    def principal(self):
        if not OAUTH['enabled']:return None
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return None
        sid=cookie.get('devarena_session')
        if not sid:return None
        with LOCK:
            record=SESSIONS.get(sid.value)
            if record and record['expires']>timestamp():return record
            SESSIONS.pop(sid.value,None)
        return None
    def authenticated(self):return not OAUTH['enabled'] or bool(self.principal())
    def auth_admin(self):
        if OAUTH['enabled']:
            p=self.principal();return bool(p and p['admin'])
        return hmac.compare_digest(self.headers.get('X-Admin-Password',''),ADMIN_PASSWORD)
    def redirect(self,where,cookie=None):
        self.send_response(302);self.send_header('Location',where)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Cache-Control','no-store');self.end_headers()
    def auth_login(self):
        cfg=OAUTH;state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48);nonce=secrets.token_urlsafe(24)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        with LOCK:
            for k,v in list(AUTH_PENDING.items()):
                if v['expires']<timestamp():AUTH_PENDING.pop(k,None)
            AUTH_PENDING[state]={'verifier':verifier,'nonce':nonce,'expires':timestamp()+600}
        params={'response_type':'code','client_id':cfg['client_id'],'redirect_uri':cfg['redirect_uri'],'scope':cfg['scopes'],'state':state,'nonce':nonce,'code_challenge':challenge,'code_challenge_method':'S256'}
        self.redirect(cfg['authorization_endpoint']+'?'+urlencode(params))
    def auth_callback(self):
        from urllib.parse import parse_qs
        args=parse_qs(urlsplit(self.path).query);state=args.get('state',[''])[0];code=args.get('code',[''])[0]
        with LOCK:pending=AUTH_PENDING.pop(state,None)
        if not pending or pending['expires']<timestamp() or not code:return self.reply(400,{'error':'OAuth state 无效、过期或授权失败，请重新登录'})
        cfg=OAUTH
        token_args={'grant_type':'authorization_code','code':code,'redirect_uri':cfg['redirect_uri'],'client_id':cfg['client_id'],'code_verifier':pending['verifier']}
        headers={'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'}
        if cfg['token_auth_method']=='client_secret_basic':
            cred=(cfg['client_id']+':'+cfg.get('client_secret','')).encode()
            headers['Authorization']='Basic '+base64.b64encode(cred).decode()
        elif cfg['token_auth_method']=='client_secret_post':token_args['client_secret']=cfg.get('client_secret','')
        try:
            tok=remote_json(cfg['token_endpoint'],token_args,headers)
            access=tok.get('access_token','')
            if not access:raise ValueError('Token 响应缺少 access_token')
            info=remote_json(cfg['userinfo_endpoint'],headers={'Authorization':'Bearer '+access,'Accept':'application/json'})
            subject=get_claim(info,cfg['identity_claim'])
            if not subject:raise ValueError('用户信息缺少唯一身份字段')
            employee=get_claim(info,cfg['employee_id_claim']);name=get_claim(info,cfg['name_claim'])
            is_admin=subject in cfg.get('admin_subjects',[]) or bool(employee and employee in cfg.get('admin_employee_ids',[]))
            sid=secrets.token_urlsafe(48)
            with LOCK:SESSIONS[sid]={'subject':subject,'employeeId':employee,'name':name,'admin':is_admin,'expires':timestamp()+int(cfg['session_hours'])*3600}
            flags='; HttpOnly; SameSite=Lax; Path=/' + ('; Secure' if cfg['cookie_secure'] else '')
            self.redirect('/', 'devarena_session='+sid+flags)
        except Exception as exc:
            logging.exception('OAuth callback failed')
            self.reply(502,{'error':'身份认证失败，请核对公司 OAuth 配置与网络连接'})
    def allowed_origin(self):
        # Browser session cookies must not authorize cross-origin state-changing requests.
        if not OAUTH['enabled']:return True
        origin=self.headers.get('Origin','')
        return bool(origin and origin.rstrip('/')==OAUTH['redirect_uri'].rsplit('/auth/callback',1)[0].rstrip('/'))

    def reply(self,code,doc):
        body=json.dumps(doc,ensure_ascii=False).encode('utf-8');self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
    def admin(self):return self.auth_admin()
    def raw(self,limit=MAX_JSON):
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise ValueError('无效的请求大小')
        if length<=0 or length>limit:raise ValueError('请求过大或为空')
        return self.rfile.read(length)
    def body(self):return json.loads(self.raw())
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/api/auth-config':return self.reply(200,public_auth_state())
        if path=='/api/app-config':return self.reply(200,{'max_upload_mb':APP_CONFIG['max_upload_mb']})
        if path=='/api/me':
            p=self.principal()
            if OAUTH['enabled'] and not p:return self.reply(401,{'error':'请先通过公司账号登录'})
            return self.reply(200,{'oauth':bool(OAUTH['enabled']),'admin':bool(p['admin']) if p else False,'name':p['name'] if p else '', 'employeeId':p['employeeId'] if p else ''})
        if path=='/auth/login' and OAUTH['enabled']:return self.auth_login()
        if path=='/auth/callback' and OAUTH['enabled']:return self.auth_callback()
        if path=='/auth/logout' and OAUTH['enabled']:
            p=SimpleCookie();p.load(self.headers.get('Cookie',''));sid=p.get('devarena_session')
            if sid:
                with LOCK:SESSIONS.pop(sid.value,None)
            self.redirect('/','devarena_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'+('; Secure' if OAUTH['cookie_secure'] else ''))
            return
        if path.startswith('/api/') and not self.authenticated():return self.reply(401,{'error':'请先登录公司账号'})
        if path=='/api/state':
            with LOCK:self.reply(200,sanitized(load(),self.admin()))
            return
        if path.startswith('/api/files/'):
            fid=path.rsplit('/',1)[-1]
            with LOCK:
                db=load(); f=next((f for f in db['files'] if f['id']==fid),None)
                exam=next((e for e in db['exams'] if f and e['id']==f['examId']),None)
                allowed=bool(f and exam and (self.admin() or exam.get('published') and (f['kind']!='answer' or exam.get('answersReleased'))))
            if not allowed:return self.reply(403,{'error':'文件无访问权限'})
            disk=UPLOADS/fid
            if not disk.is_file():return self.reply(404,{'error':'文件不存在'})
            self.send_response(200)
            # Never execute uploaded HTML/SVG or other active content.
            self.send_header('Content-Type','application/octet-stream');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(f['name']))
            self.send_header('Content-Length',str(disk.stat().st_size));self.end_headers()
            with disk.open('rb') as source:
                while True:
                    chunk=source.read(FILE_CHUNK)
                    if not chunk:break
                    self.wfile.write(chunk)
            return
        if path in ('/','/index.html'):
            data=(ROOT/'index.html').read_bytes();self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
        self.reply(404,{'error':'Not found'})
    def do_POST(self):
        path=urlsplit(self.path).path
        try:
            if OAUTH['enabled'] and (not self.authenticated() or not self.allowed_origin()):return self.reply(403,{'error':'未登录或请求来源不合法'})
            if path=='/api/login':return self.reply(200,{'admin':self.admin()})
            if path=='/api/upload':
                if not self.admin():return self.reply(403,{'error':'管理员密码错误'})
                eid=self.headers.get('X-Exam-Id','');qid=self.headers.get('X-Question-Id','')
                kind=self.headers.get('X-File-Kind','');name=pathlib.Path(unquote(self.headers.get('X-File-Name',''))).name
                if kind not in ('exam','answer','question') or not name or len(name)>180:raise ValueError('附件信息不合法')
                try:
                    length=int(self.headers.get('Content-Length','0'))
                except ValueError:raise ValueError('无效的文件长度')
                if length<=0:raise ValueError('上传文件为空或缺少 Content-Length')
                if length>MAX_FILE:raise ValueError(f'文件超过上传上限 {APP_CONFIG["max_upload_mb"]} MB')
                with LOCK:
                    db=load();e=next((e for e in db['exams'] if e['id']==eid),None)
                    if not e:raise ValueError('请先保存考试再上传附件')
                    if kind=='question' and not any(q['id']==qid for q in e['qs']):raise ValueError('题目不存在')
                fid='f'+uuid.uuid4().hex
                tmp=UPLOADS/(fid+'.part')
                try:
                    remaining=length
                    with tmp.open('wb') as dest:
                        while remaining:
                            part=self.rfile.read(min(FILE_CHUNK,remaining))
                            if not part:raise ConnectionError('上传连接中断')
                            dest.write(part)
                            remaining-=len(part)
                    with LOCK:
                        db=load();e=next((e for e in db['exams'] if e['id']==eid),None)
                        if not e:raise ValueError('考试已被删除，请重新上传')
                        if kind=='question' and not any(q['id']==qid for q in e['qs']):raise ValueError('题目已被删除，请重新上传')
                        record={'id':fid,'examId':eid,'questionId':qid if kind=='question' else None,'kind':kind,'name':name,'size':length,'created':datetime.now().isoformat()}
                        os.replace(tmp,UPLOADS/fid)
                        try:
                            db['files'].append(record)
                            if kind=='question':
                                q=next(q for q in e['qs'] if q['id']==qid);q.setdefault('attachments',[]).append(fid)
                            save(db)
                        except Exception:
                            (UPLOADS/fid).unlink(missing_ok=True)
                            raise
                    return self.reply(200,{'file':record})
                finally:
                    tmp.unlink(missing_ok=True)
            data=self.body()
            with LOCK:
                db=load()
                if path=='/api/exam':
                    if not self.admin():return self.reply(403,{'error':'管理员密码错误'})
                    e=data.get('exam',{});good_exam(e)
                    e['id']=e.get('id') or 'e'+uuid.uuid4().hex
                    ix=next((i for i,x in enumerate(db['exams']) if x['id']==e['id']),None)
                    if ix is not None:
                        old=db['exams'][ix]
                        if any(s['examId']==e['id'] for s in db['submissions']):
                            # Once submissions exist, question editing invalidates grades; only release controls can change.
                            raise ValueError('已有提交记录，不允许修改试题；请新建考试')
                        old_ids={q['id']:q for q in old['qs']}
                        for q in e['qs']:
                            if q['id'] in old_ids:q['attachments']=old_ids[q['id']].get('attachments',[])
                        db['exams'][ix]=e
                    else:db['exams'].append(e)
                    save(db);return self.reply(200,{'id':e['id']})
                if path=='/api/settings':
                    if not self.admin():return self.reply(403,{'error':'管理员密码错误'})
                    e=next((x for x in db['exams'] if x['id']==data.get('id')),None)
                    key=data.get('key')
                    if not e or key not in ('published','answersReleased'):raise ValueError('设置无效')
                    e[key]=bool(data.get('value'));save(db);return self.reply(200,{'ok':True})
                if path=='/api/start':
                    eid=str(data.get('examId',''));name=str(data.get('name','')).strip();empid=str(data.get('employeeId','')).strip()
                    if not name or len(name)>60 or not empid or len(empid)>40:raise ValueError('请填写姓名和工号')
                    p=self.principal()
                    if p and p.get('employeeId') and p['employeeId']!=empid:raise ValueError('工号必须与公司登录身份一致')
                    e=next((x for x in db['exams'] if x['id']==eid and x.get('published')),None)
                    if not e:raise ValueError('考试不可用')
                    now=datetime.now();start=datetime.fromisoformat(e['start']);end=datetime.fromisoformat(e['end'])
                    if not start<=now<=end:raise ValueError('当前不在考试时间内')
                    if any(x['examId']==eid and x['employeeId']==empid for x in db['submissions']):raise ValueError('该工号已提交本场考试')
                    attempts=db.setdefault('attempts',[])
                    existing=next((x for x in attempts if x['examId']==eid and x['employeeId']==empid),None)
                    if existing:
                        if p and existing.get('subject')!=p['subject']:raise ValueError('该工号已有其他账号开始考试')
                        existing['name']=name
                    else:
                        existing={'id':'a'+uuid.uuid4().hex,'examId':eid,'name':name,'employeeId':empid,'subject':p['subject'] if p else '', 'startedAt':iso_utc(),'startedTs':timestamp()}
                        attempts.append(existing)
                    save(db)
                    return self.reply(200,{'attemptId':existing['id'],'startedAt':existing['startedAt'],'durationLimitSeconds':int(e.get('duration',60))*60})
                if path=='/api/submit':
                    eid=str(data.get('examId','')); name=str(data.get('name','')).strip();empid=str(data.get('employeeId','')).strip();answers=data.get('answers',{})
                    if not name or len(name)>60 or not empid or len(empid)>40 or not isinstance(answers,dict):raise ValueError('姓名、工号或答案不合法')
                    e=next((e for e in db['exams'] if e['id']==eid and e.get('published')),None)
                    if not e:raise ValueError('考试不存在或尚未发布')
                    now=datetime.now();start=datetime.fromisoformat(e['start']);end=datetime.fromisoformat(e['end'])
                    if now<start or now>end:raise ValueError('当前不在考试时间内')
                    if any(s['examId']==eid and s['employeeId']==empid for s in db['submissions']):raise ValueError('该工号已提交本场考试')
                    attempt=next((x for x in db.get('attempts',[]) if x['id']==data.get('attemptId') and x['examId']==eid and x['employeeId']==empid),None)
                    if not attempt:raise ValueError('请先点击“开始考试”，系统才能记录作答时长')
                    p=self.principal()
                    if p and attempt.get('subject')!=p['subject']:raise ValueError('答题身份与开始考试的账号不一致')
                    if p and p.get('employeeId') and p['employeeId']!=empid:raise ValueError('工号与登录身份不一致')
                    elapsed=max(0,int(timestamp()-attempt['startedTs']))
                    if elapsed>int(e.get('duration',60))*60+10:raise ValueError('已超出规定考试时长，无法提交，请联系管理员')
                    valid={q['id']:str(answers.get(q['id'],''))[:20000] for q in e['qs']}
                    obj=scoring(e,valid);manual_needed=any(q['type']=='text' for q in e['qs'])
                    sub={'id':'s'+uuid.uuid4().hex,'examId':eid,'name':name,'employeeId':empid,'answers':valid,'objective':obj,'manual':0,'score':obj,'graded':not manual_needed,'at':now.isoformat(),'startedAt':attempt['startedAt'],'durationSeconds':elapsed,'feedback':''}
                    db['submissions'].append(sub);db['attempts']=[x for x in db.get('attempts',[]) if x['id']!=attempt['id']];save(db)
                    return self.reply(200,{'ok':True,'id':sub['id']})
                if path=='/api/grade':
                    if not self.admin():return self.reply(403,{'error':'管理员密码错误'})
                    sub=next((s for s in db['submissions'] if s['id']==data.get('id')),None)
                    if not sub:raise ValueError('试卷不存在')
                    e=next(x for x in db['exams'] if x['id']==sub['examId'])
                    max_manual=sum(float(q['score']) for q in e['qs'] if q['type']=='text')
                    manual=float(data.get('manual',0))
                    if not 0<=manual<=max_manual:raise ValueError('人工评分超出范围')
                    sub.update(manual=manual,score=sub['objective']+manual,graded=True,feedback=str(data.get('feedback',''))[:3000])
                    save(db);return self.reply(200,{'ok':True})
                if path=='/api/delete-exam':
                    if not self.admin():return self.reply(403,{'error':'管理员密码错误'})
                    eid=data.get('id');db['exams']=[e for e in db['exams'] if e['id']!=eid]
                    db['submissions']=[s for s in db['submissions'] if s['examId']!=eid]
                    db['attempts']=[a for a in db.get('attempts',[]) if a['examId']!=eid]
                    gone=[f for f in db['files'] if f['examId']==eid]
                    db['files']=[f for f in db['files'] if f['examId']!=eid]
                    save(db)
                    for f in gone:(UPLOADS/f['id']).unlink(missing_ok=True)
                    return self.reply(200,{'ok':True})
            return self.reply(404,{'error':'API not found'})
        except (ValueError,KeyError,TypeError,AssertionError,json.JSONDecodeError) as ex:return self.reply(400,{'error':str(ex)})
        except Exception as ex:
            print('Server error:',repr(ex));return self.reply(500,{'error':'服务器内部错误'})

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--host',default='0.0.0.0');a.add_argument('--port',type=int,default=8080);args=a.parse_args()
    http=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'DevArena 启动：http://127.0.0.1:{args.port}  | 局域网：http://本机IP:{args.port}  | 单文件上限：{APP_CONFIG["max_upload_mb"]} MB',flush=True)
    try:http.serve_forever()
    except KeyboardInterrupt:pass
