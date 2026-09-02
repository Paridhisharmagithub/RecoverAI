from pathlib import Path
import hashlib,hmac,json
from datetime import datetime,timezone
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from app.config import APP_NAME,RAZORPAY_WEBHOOK_SECRET,USE_RAZORPAY
from app.db import init_db,q,exec_sql,get_conn
from app.data_generator import seed_if_empty
from app.ml import load_or_train,train
from app.agent import review as payment_review, execute as payment_execute, run_batch as payment_batch
from app.opportunities import ensure_schema,seed_opportunities,refresh_opportunities,get,execute,bulk,decision_trace,policy

app=FastAPI(title=APP_NAME,version='3.0.0')
static_dir=Path(__file__).parent/'static'; app.mount('/static',StaticFiles(directory=static_dir),name='static')

class SettingsIn(BaseModel):
 max_retry_count:int=Field(ge=0,le=5); max_auto_amount:float=Field(gt=0,le=1000000); high_value_threshold:float=Field(gt=0,le=10000000)
 max_reminders:int=Field(ge=0,le=5); auto_recovery_enabled:bool; default_delay_hours:float=Field(gt=0,le=168); max_recovery_cost:float=Field(gt=0,le=1000)
class CampaignIn(BaseModel): name:str=Field(min_length=3,max_length=80); action:str; audience:str
class DemoAction(BaseModel): action:str|None=None

@app.on_event('startup')
def startup():
 init_db(); seeded=seed_if_empty()
 if seeded: train()
 else: load_or_train()
 ensure_schema(); seed_opportunities(); refresh_opportunities()

def money(v): return round(float(v or 0),2)

@app.get('/')
def root():return FileResponse(static_dir/'index.html')
@app.get('/api/health')
def health():return {'ok':True,'app':APP_NAME,'version':'3.0.0','provider':'razorpay-test' if USE_RAZORPAY else 'local-simulator'}

@app.get('/api/dashboard')
def dashboard():
    refresh_opportunities()
    total=q('SELECT COUNT(*) c FROM payments',one=True)['c']; opp=q('SELECT COUNT(*) c FROM recovery_opportunities WHERE status="open"',one=True)['c']
    risk=q('SELECT COALESCE(SUM(amount),0) s FROM recovery_opportunities WHERE status="open"',one=True)['s']
    expected=q('SELECT COALESCE(SUM(expected_value),0) s FROM recovery_opportunities WHERE status="open"',one=True)['s']
    recovered=q('SELECT COALESCE(SUM(actual_value),0) s FROM recovery_outcomes WHERE success=1',one=True)['s']
    attempts=q('SELECT COUNT(*) c FROM recovery_outcomes',one=True)['c']; blocks=q("SELECT COUNT(*) c FROM audit_logs WHERE event='POLICY_BLOCK'",one=True)['c']
    bykind=[dict(r) for r in q('SELECT kind,COUNT(*) cases,ROUND(SUM(amount),0) at_risk,ROUND(SUM(expected_value),0) expected FROM recovery_opportunities WHERE status="open" GROUP BY kind ORDER BY expected DESC')]
    top=[dict(r) for r in q('SELECT * FROM recovery_opportunities WHERE status="open" ORDER BY expected_value DESC LIMIT 6')]
    return {'payments_analyzed':total,'opportunities':opp,'revenue_at_risk':money(risk),'expected_net_recovery':money(expected),'revenue_recovered':money(recovered),'attempts':attempts,'policy_blocks':blocks,'recovery_rate':round(recovered/max(1,risk)*100,1),'by_kind':bykind,'top_opportunities':top,'provider':'Razorpay Test Mode' if USE_RAZORPAY else 'Local deterministic simulator'}

@app.get('/api/opportunities')
def opportunities(kind:str='all',status:str='open',limit:int=300):
    refresh_opportunities(); where=['1=1'];params=[]
    if kind!='all':where.append('o.kind=?');params.append(kind)
    if status!='all':where.append('o.status=?');params.append(status)
    rows=q(f'''SELECT o.*,c.name customer_name,c.segment,c.lifetime_value FROM recovery_opportunities o LEFT JOIN customers c ON c.id=o.customer_id WHERE {' AND '.join(where)} ORDER BY o.expected_value DESC LIMIT ?''',(*params,limit))
    return [dict(r) for r in rows]

@app.get('/api/opportunities/{op_id}')
def opportunity(op_id:str):
    op=get(op_id)
    if not op: raise HTTPException(404,'Opportunity not found')
    return {**op,'trace':decision_trace(op),'policy':policy(op)}

@app.post('/api/opportunities/{op_id}/execute')
def opportunity_execute(op_id:str, body:DemoAction=DemoAction()):
    try:return execute(op_id,body.action)
    except ValueError as e: raise HTTPException(404,str(e))

@app.post('/api/opportunities/batch')
def opportunity_batch(limit:int=100): return bulk(min(max(limit,1),1000))

@app.get('/api/impact')
def impact():
    rows=q('SELECT * FROM recovery_opportunities')
    baseline=ai=0.0; baseline_hits=ai_hits=0; cost_baseline=cost_ai=0.0
    action_mix={}; kinds={}
    for r in rows:
        score=r['recovery_score']; amount=r['amount']; kind=r['kind']; reason=r['reason_code']
        # Baseline intentionally simple: retry timeout payment failures; remind everything else.
        if kind=='PAYMENT_FAILURE' and reason in ('BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR'):
            bscore=score*.72
        elif kind=='CHECKOUT_ABANDONMENT': bscore=score*.48
        elif kind=='SUBSCRIPTION_DUNNING': bscore=score*.55
        elif kind=='RECEIVABLE': bscore=score*.42
        else:bscore=score*.5
        baseline += amount*bscore
        ai += amount*score
        baseline_hits += int(bscore>=.5)
        ai_hits += int(score>=.5)
        cost_baseline += 3.5 if bscore>=.5 else 1.5
        cost_ai += r['recovery_cost']
        action_mix[r['recommended_action']]=action_mix.get(r['recommended_action'],0)+1
        z=kinds.setdefault(kind,{'cases':0,'at_risk':0,'expected_ai':0,'expected_baseline':0});z['cases']+=1;z['at_risk']+=amount;z['expected_ai']+=amount*score;z['expected_baseline']+=amount*bscore
    return {'sample_size':len(rows),'baseline_expected':money(baseline),'ai_expected':money(ai),'incremental_expected':money(ai-baseline),'lift_pct':round((ai/max(1,baseline)-1)*100,1),'baseline_net':money(baseline-cost_baseline),'ai_net':money(ai-cost_ai),'net_incremental':money((ai-cost_ai)-(baseline-cost_baseline)),'action_mix':action_mix,'by_kind':[{'kind':k,'cases':v['cases'],'at_risk':money(v['at_risk']),'ai':money(v['expected_ai']),'baseline':money(v['expected_baseline']),'delta':money(v['expected_ai']-v['expected_baseline'])} for k,v in kinds.items()]}

@app.get('/api/insights')
def insights():
    roots=[dict(r) for r in q('SELECT kind,reason_code,COUNT(*) cases,SUM(amount) at_risk,AVG(recovery_score) score FROM recovery_opportunities WHERE status="open" GROUP BY kind,reason_code ORDER BY at_risk DESC LIMIT 12')]
    segments=[dict(r) for r in q('SELECT segment,COUNT(*) customers,ROUND(AVG(successful_transactions*1.0/total_transactions),3) success_rate,ROUND(SUM(lifetime_value),0) ltv FROM customers GROUP BY segment ORDER BY ltv DESC')]
    return {'root_causes':roots,'segments':segments,'recommendations':[
      {'title':'Treat timeout failures as a speed problem','detail':'Keep transient retries close to the event, but stop after the merchant-defined attempt budget.','value':'HIGH'},
      {'title':'Use fresh payment surfaces for stale instruments','detail':'For expired/authentication failures, create a new payment surface rather than repeating the same instrument.','value':'HIGH'},
      {'title':'Separate human-gated receivables','detail':'Large or disputed invoices should move to a promise-to-pay or review queue rather than blind automation.','value':'RISK'},
      {'title':'Measure incremental value, not activity','detail':'Judge every playbook by net revenue recovered after intervention cost and false-positive downside.','value':'METRIC'},
    ]}

@app.get('/api/trace/{op_id}')
def trace(op_id:str):
    op=get(op_id)
    if not op: raise HTTPException(404,'Opportunity not found')
    events=[dict(r) for r in q('SELECT step,label,detail,status,created_at FROM decision_events WHERE opportunity_id=? ORDER BY id',(op_id,))]
    return {'opportunity':op,'trace':events or decision_trace(op)}

@app.get('/api/outcomes')
def outcomes(limit:int=100): return [dict(r) for r in q('SELECT ro.*,o.title,o.kind,o.amount FROM recovery_outcomes ro JOIN recovery_opportunities o ON o.id=ro.opportunity_id ORDER BY ro.id DESC LIMIT ?',(limit,))]

# Original payment endpoints retained for compatibility.
@app.get('/api/payments')
def payments(limit:int=150,status:str='all'):
    where='';params=[]
    if status!='all':where='WHERE p.status=?';params=[status]
    rows=q(f'''SELECT p.*, c.name customer_name,c.email customer_email,c.segment,rc.recovery_score,rc.recommended_action,rc.status recovery_status,rc.expected_revenue
               FROM payments p JOIN customers c ON c.id=p.customer_id LEFT JOIN recovery_cases rc ON rc.payment_id=p.id {where} ORDER BY p.created_at DESC LIMIT ?''',(*params,limit))
    return [dict(r) for r in rows]
@app.get('/api/payments/{payment_id}')
def payment_detail(payment_id:str):
    try:return payment_review(payment_id)
    except ValueError as e:raise HTTPException(404,str(e))
@app.post('/api/recovery/{payment_id}/review')
def recovery_review(payment_id:str):
    try:return payment_review(payment_id)
    except ValueError as e:raise HTTPException(404,str(e))
@app.post('/api/recovery/{payment_id}/execute')
def recovery_execute(payment_id:str):
    try:return payment_execute(payment_id)
    except ValueError as e:raise HTTPException(404,str(e))

@app.post('/api/recovery/batch')
def recovery_batch(limit:int=100):return payment_batch(min(max(limit,1),1000))

@app.get('/api/analytics')
def analytics():
    _,m=load_or_train(); imp=impact();
    return {'ml':m,'impact':imp}

@app.get('/api/audit')
def audit(limit:int=150):return [dict(r) for r in q('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?',(limit,))]
@app.get('/api/settings')
def get_settings():return dict(q('SELECT * FROM settings WHERE id=1',one=True))
@app.put('/api/settings')
def update_settings(body:SettingsIn):
    exec_sql('UPDATE settings SET max_retry_count=?,max_auto_amount=?,high_value_threshold=?,max_reminders=?,auto_recovery_enabled=?,default_delay_hours=?,max_recovery_cost=? WHERE id=1',(body.max_retry_count,body.max_auto_amount,body.high_value_threshold,body.max_reminders,int(body.auto_recovery_enabled),body.default_delay_hours,body.max_recovery_cost))
    exec_sql('INSERT INTO audit_logs(event,details,created_at) VALUES(?,?,?)',('SETTINGS_UPDATED',body.model_dump_json(),datetime.now(timezone.utc).isoformat()));return get_settings()

@app.get('/api/strategy-lab')
def strategy_lab():
    x=impact(); x['incremental_revenue']=x['incremental_expected']; x['ai_revenue']=x['ai_expected']; x['baseline_revenue']=x['baseline_expected']; return x
@app.get('/api/playbooks')
def playbooks():
    from app.opportunities import PLAYBOOKS
    out=[]
    for kind,items in PLAYBOOKS.items():
        for reason,(action,rationale) in items.items():out.append({'kind':kind,'reason':reason,'action':action,'rationale':rationale})
    return out

@app.get('/api/campaigns')
def campaigns():return [dict(r) for r in q('SELECT * FROM recovery_campaigns ORDER BY id DESC')]
@app.post('/api/campaigns')
def create_campaign(body:CampaignIn):
    allowed=['SEND_CHECKOUT_NUDGE','GENERATE_PAYMENT_LINK','SCHEDULE_RETRY','RETRY_MANDATE','SEND_INVOICE_REMINDER','ESCALATE']
    if body.action not in allowed:raise HTTPException(400,'Unsupported campaign action')
    expected=q('SELECT COALESCE(SUM(expected_value),0) s FROM recovery_opportunities WHERE status="open" AND recommended_action=?',(body.action,),one=True)['s']
    rid=exec_sql('INSERT INTO recovery_campaigns(name,action,audience,status,estimated_revenue,created_at) VALUES(?,?,?,?,?,?)',(body.name,body.action,body.audience,'draft',expected,datetime.now(timezone.utc).isoformat()))
    exec_sql('INSERT INTO audit_logs(event,details,created_at) VALUES(?,?,?)',('CAMPAIGN_CREATED',json.dumps({'id':rid,'name':body.name,'action':body.action,'audience':body.audience}),datetime.now(timezone.utc).isoformat()))
    return dict(q('SELECT * FROM recovery_campaigns WHERE id=?',(rid,),one=True))

@app.post('/api/simulator/reset')
def reset():
    conn=get_conn(); conn.executescript('DELETE FROM audit_logs;DELETE FROM agent_actions;DELETE FROM recovery_cases;DELETE FROM experiments;DELETE FROM recovery_campaigns;DELETE FROM recovery_outcomes;DELETE FROM decision_events;DELETE FROM playbook_results;DELETE FROM recovery_opportunities;DELETE FROM payments;DELETE FROM customers;'); conn.commit();conn.close()
    seed_if_empty();train();ensure_schema();seed_opportunities();refresh_opportunities();return {'ok':True}

@app.post('/api/webhooks/razorpay')
async def razorpay_webhook(request:Request):
    raw=await request.body();signature=request.headers.get('x-razorpay-signature','')
    if RAZORPAY_WEBHOOK_SECRET:
        expected=hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise HTTPException(400,'Invalid webhook signature')
    try:payload=json.loads(raw.decode())
    except Exception:raise HTTPException(400,'Invalid JSON')
    event=payload.get('event','unknown');pay=payload.get('payload',{}).get('payment',{}).get('entity',{});pid=pay.get('id');now=datetime.now(timezone.utc).isoformat()
    exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(pid or '', 'RAZORPAY_WEBHOOK',json.dumps({'event':event,'payment':pay}),now))
    return {'ok':True,'event':event}
