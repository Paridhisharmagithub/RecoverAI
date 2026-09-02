from __future__ import annotations

import json, random
from datetime import datetime, timezone, timedelta
from typing import Any
from app.db import get_conn, q, exec_sql
from app.config import USE_RAZORPAY
from app.integrations import payment_provider

ACTION_ORDER = [
    'RETRY_PAYMENT', 'SCHEDULE_RETRY', 'GENERATE_PAYMENT_LINK',
    'SEND_CHECKOUT_NUDGE', 'RETRY_MANDATE', 'SEND_INVOICE_REMINDER', 'ESCALATE', 'STOP'
]

PLAYBOOKS = {
    'PAYMENT_FAILURE': {
        'BANK_TIMEOUT': ('RETRY_PAYMENT', 'Transient bank response. Retry once while the payment context is still fresh.'),
        'UPI_TIMEOUT': ('RETRY_PAYMENT', 'Transient UPI/network issue. A bounded retry is lower friction than restarting checkout.'),
        'NETWORK_ERROR': ('RETRY_PAYMENT', 'Network/provider failure. Try one bounded retry before changing the payment surface.'),
        'INSUFFICIENT_FUNDS': ('SCHEDULE_RETRY', 'Funds may become available later; delay instead of repeatedly charging the same instrument.'),
        'CARD_EXPIRED': ('GENERATE_PAYMENT_LINK', 'The payment instrument is stale. Give the buyer a fresh payment surface.'),
        'AUTHENTICATION_FAILED': ('GENERATE_PAYMENT_LINK', 'Authentication failed. Avoid repeating the same instrument; create a fresh payment surface.'),
        'UNKNOWN': ('SEND_CHECKOUT_NUDGE', 'Cause is unclear. Start with a low-cost nudge and avoid unnecessary retries.'),
    },
    'CHECKOUT_ABANDONMENT': {
        'CHECKOUT_IDLE': ('SEND_CHECKOUT_NUDGE', 'The buyer showed intent but did not finish. A timely, low-friction reminder is the safest first action.'),
        'PRICE_SENSITIVITY': ('GENERATE_PAYMENT_LINK', 'High intent with price friction. Offer a fresh checkout surface without silently discounting.'),
        'PAYMENT_METHOD_DROP': ('GENERATE_PAYMENT_LINK', 'Payment-method friction suggests restarting checkout may recover the intent.'),
    },
    'SUBSCRIPTION_DUNNING': {
        'MANDATE_FAILED': ('RETRY_MANDATE', 'Recurring payment failure is eligible for a bounded mandate retry before human escalation.'),
        'CARD_EXPIRED': ('GENERATE_PAYMENT_LINK', 'The saved instrument is stale; route the customer to an updated payment surface.'),
        'BALANCE_LOW': ('SCHEDULE_RETRY', 'Balance-related failure should wait before another attempt.'),
    },
    'RECEIVABLE': {
        'OVERDUE_7D': ('SEND_INVOICE_REMINDER', 'Early overdue invoices respond well to a polite automated reminder.'),
        'OVERDUE_30D': ('SEND_INVOICE_REMINDER', 'Longer overdue accounts need escalation language and a clear promise-to-pay request.'),
        'DISPUTED': ('ESCALATE', 'A disputed receivable should not be auto-collected; route it to a human.'),
    },
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema():
    conn = get_conn()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS recovery_opportunities (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      customer_id TEXT,
      payment_id TEXT,
      title TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      amount REAL NOT NULL,
      age_hours REAL NOT NULL DEFAULT 0,
      intent_score REAL NOT NULL DEFAULT 0,
      recovery_score REAL NOT NULL DEFAULT 0,
      expected_value REAL NOT NULL DEFAULT 0,
      recovery_cost REAL NOT NULL DEFAULT 0,
      recommended_action TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open',
      outcome TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS recovery_outcomes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      opportunity_id TEXT NOT NULL,
      action TEXT NOT NULL,
      expected_value REAL NOT NULL,
      actual_value REAL NOT NULL DEFAULT 0,
      success INTEGER NOT NULL DEFAULT 0,
      tool_result TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS decision_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      opportunity_id TEXT NOT NULL,
      step TEXT NOT NULL,
      label TEXT NOT NULL,
      detail TEXT NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS playbook_results (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      playbook TEXT NOT NULL,
      action TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      successes INTEGER NOT NULL DEFAULT 0,
      recovered_value REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL
    );
    ''')
    conn.commit(); conn.close()


def _segment(c):
    return c['segment'] if c else 'growth'


def seed_opportunities():
    ensure_schema()
    if q('SELECT COUNT(*) c FROM recovery_opportunities', one=True)['c']:
        return False
    customers = [dict(r) for r in q('SELECT * FROM customers')]
    payments = [dict(r) for r in q('SELECT * FROM payments WHERE status="failed" ORDER BY created_at DESC')]
    rng = random.Random(77)
    now = datetime.now(timezone.utc)
    rows=[]

    # Payment failures become first-class recovery opportunities.
    for p in payments:
        c = next((x for x in customers if x['id']==p['customer_id']), None)
        success_rate = (c['successful_transactions']/max(1,c['total_transactions'])) if c else .65
        score = max(.03, min(.97, .46 + .42*(success_rate-.65) + (0.17 if p['failure_reason'] in ('BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR') else 0) - .22*p['fraud_flag'] - .08*p['retry_count']))
        action, _ = PLAYBOOKS['PAYMENT_FAILURE'].get(p['failure_reason'] or 'UNKNOWN', PLAYBOOKS['PAYMENT_FAILURE']['UNKNOWN'])
        if p['fraud_flag'] or p['amount'] > 50000: action='ESCALATE'
        ev=p['amount']*score
        rows.append((f'opp_pay_{p["id"]}','PAYMENT_FAILURE',p['customer_id'],p['id'],f'Failed payment {p["id"]}',p['failure_reason'] or 'UNKNOWN',p['amount'],
                     max(0,(now-datetime.fromisoformat(p['created_at'])).total_seconds()/3600),0,score,ev,2.0,action,'open',None,p['created_at'],_now(),json.dumps({'segment':_segment(c),'fraud_flag':p['fraud_flag'],'retry_count':p['retry_count']})))

    # Checkout intent: high-intent sessions that never created a successful payment.
    for i in range(1, 851):
        c=rng.choice(customers); amount=round(rng.choice([1299,2499,3999,6999,9999,14999])*rng.uniform(.9,1.15),2)
        reason=rng.choices(['CHECKOUT_IDLE','PRICE_SENSITIVITY','PAYMENT_METHOD_DROP'],weights=[.55,.2,.25])[0]
        intent=max(.55,min(.99,rng.gauss(.77,.11)))
        score=max(.08,min(.92,intent - (.12 if reason=='PRICE_SENSITIVITY' else 0) + (.08 if c['segment']=='vip' else 0)))
        age=rng.uniform(1,72); action=PLAYBOOKS['CHECKOUT_ABANDONMENT'][reason][0]
        created=(now-timedelta(hours=age)).isoformat()
        rows.append((f'opp_chk_{i:04d}','CHECKOUT_ABANDONMENT',c['id'],None,f'Checkout intent #{i:04d}',reason,amount,age,intent,score,amount*score,1.5,action,'open',None,created,_now(),json.dumps({'segment':c['segment'],'cart_items':rng.randint(1,5),'source':rng.choice(['mobile_web','desktop','in_app'])})))

    # Subscription / dunning: a separate recurring-revenue lane.
    for i in range(1, 651):
        c=rng.choice(customers); amount=round(rng.choice([499,799,1499,2499,4999,9999])*rng.uniform(.9,1.08),2)
        reason=rng.choices(['MANDATE_FAILED','CARD_EXPIRED','BALANCE_LOW'],weights=[.5,.22,.28])[0]
        score=max(.08,min(.94,.48 + .28*(c['successful_transactions']/max(1,c['total_transactions'])-.6) + (.08 if c['segment']=='loyal' else 0) + (.08 if reason=='MANDATE_FAILED' else 0)))
        age=rng.uniform(4,96); action=PLAYBOOKS['SUBSCRIPTION_DUNNING'][reason][0]
        created=(now-timedelta(hours=age)).isoformat()
        rows.append((f'opp_sub_{i:04d}','SUBSCRIPTION_DUNNING',c['id'],None,f'Subscription #{i:04d}',reason,amount,age,0,score,amount*score,2.5,action,'open',None,created,_now(),json.dumps({'segment':c['segment'],'plan':rng.choice(['Starter','Growth','Pro']),'cycle':rng.choice(['monthly','annual'])})))

    # B2B receivables: lower volume, higher value and a human-gated exception class.
    for i in range(1, 221):
        c=rng.choice(customers); amount=round(rng.choice([15000,25000,50000,75000,120000])*rng.uniform(.85,1.1),2)
        reason=rng.choices(['OVERDUE_7D','OVERDUE_30D','DISPUTED'],weights=[.58,.3,.12])[0]
        score=.72 if reason=='OVERDUE_7D' else .54 if reason=='OVERDUE_30D' else .12
        action=PLAYBOOKS['RECEIVABLE'][reason][0]
        age={'OVERDUE_7D':7*24,'OVERDUE_30D':30*24,'DISPUTED':12*24}[reason]
        created=(now-timedelta(hours=age)).isoformat()
        rows.append((f'opp_rec_{i:04d}','RECEIVABLE',c['id'],None,f'Invoice #{800000+i}',reason,amount,age,0,score,amount*score,8.0,action,'open',None,created,_now(),json.dumps({'segment':c['segment'],'invoice_age_days':round(age/24),'terms':'Net 30'})))

    conn=get_conn(); conn.executemany('''INSERT INTO recovery_opportunities
    (id,kind,customer_id,payment_id,title,reason_code,amount,age_hours,intent_score,recovery_score,expected_value,recovery_cost,recommended_action,status,outcome,created_at,updated_at,metadata)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows); conn.commit(); conn.close(); return True


def refresh_opportunities():
    rows=q('SELECT * FROM recovery_opportunities WHERE status="open"')
    now=_now(); conn=get_conn()
    for r in rows:
        meta=json.loads(r['metadata'] or '{}')
        score=float(r['recovery_score'])
        # Age decay prevents stale opportunities from dominating the queue forever.
        age=max(0,float(r['age_hours']))
        if r['kind']=='CHECKOUT_ABANDONMENT': decay=max(.55,1-(age/240))
        elif r['kind']=='PAYMENT_FAILURE': decay=max(.60,1-(age/720))
        else: decay=max(.72,1-(age/2160))
        if r['kind']=='RECEIVABLE' and r['reason_code']=='DISPUTED': score=.10
        score=max(.02,min(.98,score*decay))
        # Customer value increases priority, not raw authority.
        multiplier=1.15 if meta.get('segment')=='vip' else 1.05 if meta.get('segment')=='loyal' else 1.0
        ev=r['amount']*score - r['recovery_cost']
        conn.execute('UPDATE recovery_opportunities SET recovery_score=?,expected_value=?,updated_at=? WHERE id=?',(score,max(0,ev*multiplier),now,r['id']))
    conn.commit(); conn.close()


def get(op_id):
    r=q('''SELECT o.*,c.name customer_name,c.email customer_email,c.segment,c.lifetime_value,p.status payment_status,p.retry_count,p.reminder_count,p.fraud_flag
            FROM recovery_opportunities o LEFT JOIN customers c ON c.id=o.customer_id LEFT JOIN payments p ON p.id=o.payment_id
            WHERE o.id=?''',(op_id,),one=True)
    return dict(r) if r else None


def policy(op: dict):
    settings=q('SELECT * FROM settings WHERE id=1',one=True)
    reasons=[]; allowed=True
    if not settings['auto_recovery_enabled']:
        allowed=False; reasons.append('Automatic recovery is disabled by the merchant.')
    if op['amount'] > settings['max_auto_amount'] and op['recommended_action'] not in ('ESCALATE','STOP'):
        allowed=False; reasons.append(f"Amount exceeds auto-recovery limit of ₹{settings['max_auto_amount']:,.0f}.")
    if op.get('fraud_flag') and op['recommended_action']!='ESCALATE':
        allowed=False; reasons.append('Fraud flag requires escalation.')
    if op['kind']=='RECEIVABLE' and op['reason_code']=='DISPUTED':
        allowed=False; reasons.append('Disputed receivables require human review.')
    retry_count = op.get("retry_count") or 0
    reminder_count = op.get("reminder_count") or 0

    if (
        retry_count >= settings["max_retry_count"]
        and op["recommended_action"] in (
            "RETRY_PAYMENT",
            "RETRY_MANDATE",
            "SCHEDULE_RETRY",
        )
    ):
        allowed=False; reasons.append('Maximum retry count reached.')
    if reminder_count >= settings['max_reminders'] and op['recommended_action'] in ('SEND_CHECKOUT_NUDGE','SEND_INVOICE_REMINDER'):
        allowed=False; reasons.append('Maximum reminder count reached.')
    if op['amount'] >= settings['high_value_threshold'] and op['recommended_action'] not in ('ESCALATE','STOP'):
        allowed=False; reasons.append('High-value opportunity requires merchant review.')
    if op['recovery_cost'] > settings['max_recovery_cost'] and op['recommended_action'] not in ('ESCALATE','STOP'):
        allowed=False; reasons.append('Estimated recovery cost exceeds merchant limit.')
    if not reasons and op['recommended_action'] not in ('ESCALATE','STOP'):
        reasons.append('Within merchant policy and recovery-cost budget.')
    return allowed, reasons


def decision_trace(op: dict):
    allowed, reasons=policy(op)
    action=op['recommended_action']
    play=PLAYBOOKS.get(op['kind'],{}).get(op['reason_code'],('',''))
    return [
      {'step':'OBSERVE','label':'Opportunity detected','detail':f"{op['kind'].replace('_',' ').title()} · ₹{op['amount']:,.0f} · {op['reason_code'].replace('_',' ')}",'status':'done'},
      {'step':'PREDICT','label':'Recovery probability','detail':f"{op['recovery_score']*100:.1f}% estimated recoverability; expected net value ₹{max(0,op['expected_value']):,.0f}",'status':'done'},
      {'step':'PLAN','label':'Best intervention','detail':f"{action.replace('_',' ')} · {play[1] if play else 'Bounded playbook selected.'}",'status':'done'},
      {'step':'COUNTERFACTUAL','label':'Alternatives considered','detail':_counterfactual_text(op),'status':'done'},
      {'step':'GATE','label':'Merchant policy','detail':'Allowed · '+reasons[0] if allowed else 'Blocked · '+reasons[0],'status':'passed' if allowed else 'blocked'},
      {'step':'EXECUTE','label':'Tool execution','detail':'Ready to execute one bounded action; outcome becomes part of the audit record.','status':'ready'},
    ]


def _counterfactual_text(op):
    opts=[]
    kind=op['kind']; reason=op['reason_code']
    candidates={
      'PAYMENT_FAILURE':['RETRY_PAYMENT','SCHEDULE_RETRY','GENERATE_PAYMENT_LINK','SEND_CHECKOUT_NUDGE','ESCALATE'],
      'CHECKOUT_ABANDONMENT':['SEND_CHECKOUT_NUDGE','GENERATE_PAYMENT_LINK','ESCALATE'],
      'SUBSCRIPTION_DUNNING':['RETRY_MANDATE','SCHEDULE_RETRY','GENERATE_PAYMENT_LINK','ESCALATE'],
      'RECEIVABLE':['SEND_INVOICE_REMINDER','ESCALATE'],
    }.get(kind,['ESCALATE'])
    for a in candidates:
        if a==op['recommended_action']: continue
        penalty=.08
        if 'TIMEOUT' in reason and a in ('RETRY_PAYMENT','RETRY_MANDATE'): penalty=-.10
        if reason=='DISPUTED' and a!='ESCALATE': penalty=.65
        opts.append((a,max(.02,op['recovery_score']-penalty)))
    opts=sorted(opts,key=lambda x:x[1],reverse=True)[:2]
    return ' · '.join(f"{a.replace('_',' ')} {s*100:.0f}%" for a,s in opts) or 'Escalation remains the safe fallback.'


def execute(op_id: str, force_action: str|None=None):
    op=get(op_id)
    if not op: raise ValueError('Opportunity not found')
    action=force_action or op['recommended_action']
    now=_now(); allowed,reasons=policy({**op,'recommended_action':action})
    for step,label,status in [('OBSERVE','Opportunity loaded','done'),('PREDICT',f"Recovery probability {op['recovery_score']*100:.1f}%",'done'),('PLAN',action.replace('_',' '),'done')]:
        exec_sql('INSERT INTO decision_events(opportunity_id,step,label,detail,status,created_at) VALUES(?,?,?,?,?,?)',(op_id,step,label,label,status,now))
    if not allowed:
        exec_sql('INSERT INTO decision_events(opportunity_id,step,label,detail,status,created_at) VALUES(?,?,?,?,?,?)',(op_id,'GATE','Policy blocked', '; '.join(reasons),'blocked',now))
        exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(op.get('payment_id') or '', 'POLICY_BLOCK', json.dumps({'opportunity_id':op_id,'action':action,'reasons':reasons}),now))
        return {'ok':False,'status':'blocked','action':action,'reasons':reasons,'amount':op['amount'],'score':op['recovery_score']}

    # Local deterministic tool simulator. Razorpay Test Mode can take the real fresh-payment-surface action.
    success=False; result={}; actual=0; status='failed'; provider_fail=False
    if action=='ESCALATE':
        success=True; result={'status':'ESCALATED','queue':'merchant_review'}; status='escalated'
    elif action=='STOP':
        success=True; result={'status':'STOPPED'}; status='stopped'
    elif USE_RAZORPAY and action=='GENERATE_PAYMENT_LINK' and op.get('payment_id'):
        result=payment_provider.execute(action,op)
        success=bool(result.get('success')); status='recovered' if success else 'failed'; actual=op['amount'] if success else 0
    else:
        rng=random.Random(sum(ord(ch) for ch in op_id)+int(op['recovery_score']*1000))
        # Curated demo cases: one guaranteed recovery and one provider failure.
        provider_fail=(op_id=='opp_pay_pay_000007')
        success=(True if op_id=='opp_pay_pay_000010' else False if provider_fail else rng.random() < min(.96,max(.08,op['recovery_score'] + .10)))
        result={'status':'SUCCESS' if success else 'PROVIDER_ERROR' if provider_fail else 'NO_RECOVERY','action':action}
        status='recovered' if success else 'graceful_failure' if provider_fail else 'failed'; actual=op['amount'] if success else 0

    exec_sql('INSERT INTO decision_events(opportunity_id,step,label,detail,status,created_at) VALUES(?,?,?,?,?,?)',(op_id,'GATE','Policy passed','Action is within the merchant-configured bounds.','passed',now))
    exec_sql('INSERT INTO decision_events(opportunity_id,step,label,detail,status,created_at) VALUES(?,?,?,?,?,?)',(op_id,'EXECUTE','Tool result',json.dumps(result),'done' if success else 'failed',now))
    exec_sql('INSERT INTO recovery_outcomes(opportunity_id,action,expected_value,actual_value,success,tool_result,created_at) VALUES(?,?,?,?,?,?,?)',(op_id,action,op['expected_value'],actual,int(success),json.dumps(result),now))
    new_status=status if status in ('recovered','failed') else status
    exec_sql('UPDATE recovery_opportunities SET recommended_action=?,status=?,outcome=?,updated_at=? WHERE id=?',(action,new_status,json.dumps(result),now,op_id))
    exec_sql('INSERT INTO agent_actions(payment_id,action,reason,confidence,policy_result,api_result,success,created_at) VALUES(?,?,?,?,?,?,?,?)',(op.get('payment_id') or '',action,f"{op['reason_code']} · {op['kind']}",op['recovery_score'],'PASSED',json.dumps(result),int(success),now))
    exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(op.get('payment_id') or '', 'ACTION_EXECUTED', json.dumps({'opportunity_id':op_id,'kind':op['kind'],'amount':op['amount'],'action':action,'result':result}),now))
    if success:
        exec_sql('INSERT INTO playbook_results(playbook,action,attempts,successes,recovered_value,updated_at) VALUES(?,?,?,?,?,?)',(op['reason_code'],action,1,1,actual,now))
        # Keep payment table in sync for payment-failure opportunities.
        if op.get('payment_id') and action=='RETRY_PAYMENT':
            exec_sql('UPDATE payments SET status="captured",recovered_at=? WHERE id=?',(now,op['payment_id']))
            exec_sql('UPDATE recovery_cases SET status="recovered",actual_action=?,revenue_recovered=?,updated_at=? WHERE payment_id=?',(action,actual,now,op['payment_id']))
    elif status=='graceful_failure':
        exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(op.get('payment_id') or '', 'GRACEFUL_FAILURE', json.dumps({'opportunity_id':op_id,'action':action,'fallback':'ESCALATE','provider_result':result}),now))
        exec_sql('INSERT INTO playbook_results(playbook,action,attempts,successes,recovered_value,updated_at) VALUES(?,?,?,?,?,?)',(op['reason_code'],action,1,0,0,now))
    return {'ok':success,'status':status,'action':action,'score':op['recovery_score'],'result':result,'reasons':reasons,'amount':op['amount'],'expected_value':op['expected_value'],'actual_value':actual,'fallback':'ESCALATE' if status=='graceful_failure' else None}


def bulk(limit=100):
    rows=q('SELECT id FROM recovery_opportunities WHERE status="open" ORDER BY expected_value DESC LIMIT ?',(limit,))
    out={'processed':0,'recovered':0,'revenue_recovered':0.0,'blocked':0,'failed':0,'escalated':0}
    for r in rows:
        result=execute(r['id']); out['processed']+=1
        if result['status']=='recovered': out['recovered']+=1;out['revenue_recovered']+=result['actual_value']
        elif result['status']=='blocked':out['blocked']+=1
        elif result['status']=='escalated':out['escalated']+=1
        else:out['failed']+=1
    return out
