from datetime import datetime, timezone, timedelta
import json
from app.db import q, exec_sql, get_conn
from app.ml import predict
from app.policy import gate
from app.integrations import payment_provider

REASON_PLAYBOOKS={
 'BANK_TIMEOUT':('RETRY_PAYMENT','Transient bank error; retry while the context is still fresh.','retry'),
 'UPI_TIMEOUT':('RETRY_PAYMENT','Transient UPI/network failure; retry is usually lower-friction than a new payment surface.','retry'),
 'NETWORK_ERROR':('RETRY_PAYMENT','Network/provider failure; retry before escalating.','retry'),
 'INSUFFICIENT_FUNDS':('SCHEDULE_RETRY','Funds may become available later; delay rather than hammer the payment immediately.','scheduled'),
 'CARD_EXPIRED':('GENERATE_PAYMENT_LINK','The instrument is stale; a fresh payment surface is the lowest-friction recovery.','link'),
 'AUTHENTICATION_FAILED':('GENERATE_PAYMENT_LINK','Authentication failed; avoid blindly repeating the same instrument.','link'),
 'UNKNOWN':('SEND_REMINDER','Cause is unclear; start with a low-cost nudge and avoid unnecessary retries.','reminder'),
}

def get_context(payment_id):
 return q('''SELECT p.*, c.name customer_name, c.email customer_email, c.phone customer_phone, c.total_transactions total_txn, c.successful_transactions successful_txn, (CAST(c.successful_transactions AS REAL)/c.total_transactions) success_rate, c.lifetime_value, c.segment FROM payments p JOIN customers c ON c.id=p.customer_id WHERE p.id=?''',(payment_id,),one=True)

def counterfactuals(payment, score):
 reason=payment['failure_reason'] or 'UNKNOWN'; cands=[]
 options=['RETRY_PAYMENT','SCHEDULE_RETRY','SEND_REMINDER','GENERATE_PAYMENT_LINK','ESCALATE']
 for a in options:
  base=score
  if a=='RETRY_PAYMENT' and reason not in ['BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR']: base-=.32
  if a=='SCHEDULE_RETRY' and reason!='INSUFFICIENT_FUNDS': base-=.18
  if a=='GENERATE_PAYMENT_LINK' and reason not in ['CARD_EXPIRED','AUTHENTICATION_FAILED']: base-=.18
  if a=='SEND_REMINDER' and reason in ['CARD_EXPIRED','AUTHENTICATION_FAILED']: base-=.22
  if payment['fraud_flag'] and a!='ESCALATE': base-=.75
  cands.append((a,max(0,min(1,base))))
 return sorted(cands,key=lambda x:x[1],reverse=True)

def action_from_score(payment, score):
 reason=payment['failure_reason'] or 'UNKNOWN'
 if payment['status']!='failed': return {'score':score,'action':'STOP','explanation':'Payment is already successful.'}
 if payment['fraud_flag']: return {'score':score,'action':'ESCALATE','explanation':'Suspicious payment: autonomous recovery is blocked.'}
 if payment['amount']>50000: return {'score':score,'action':'ESCALATE','explanation':'High-value payment: request merchant review before recovery.'}
 if reason in ['BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR'] and score>=.52: return {'score':score,'action':'RETRY_PAYMENT','explanation':REASON_PLAYBOOKS[reason][1]}
 if reason=='INSUFFICIENT_FUNDS' and score>=.42: return {'score':score,'action':'SCHEDULE_RETRY','explanation':REASON_PLAYBOOKS[reason][1]}
 if reason in ['CARD_EXPIRED','AUTHENTICATION_FAILED'] and score>=.30: return {'score':score,'action':'GENERATE_PAYMENT_LINK','explanation':REASON_PLAYBOOKS[reason][1]}
 if score>=.22: return {'score':score,'action':'SEND_REMINDER','explanation':'Some recovery potential remains; start with a low-cost reminder.'}
 return {'score':score,'action':'ESCALATE','explanation':'Recovery probability is low; route to a human.'}

def recommend(payment): return action_from_score(payment,predict(payment))

def review(payment_id):
 p=get_context(payment_id)
 if not p: raise ValueError('Payment not found')
 rec=recommend(p); allowed,reasons=gate(p,rec['action']); cf=counterfactuals(p,rec['score'])
 return {'payment':dict(p),'score':rec['score'],'action':rec['action'],'explanation':rec['explanation'],'policy_allowed':allowed,'policy_reasons':reasons,'counterfactuals':[{'action':a,'score':s} for a,s in cf[:4]],'playbook':REASON_PLAYBOOKS.get(p['failure_reason'] or 'UNKNOWN',REASON_PLAYBOOKS['UNKNOWN'])}

def execute(payment_id):
 p=get_context(payment_id)
 if not p: raise ValueError('Payment not found')
 rec=recommend(p); allowed,reasons=gate(p,rec['action']); now=datetime.now(timezone.utc).isoformat()
 if not allowed:
  exec_sql('INSERT INTO agent_actions(payment_id,action,reason,confidence,policy_result,api_result,success,created_at) VALUES(?,?,?,?,?,?,?,?)',(payment_id,rec['action'],rec['explanation'],rec['score'],'BLOCKED', 'Blocked by policy',0,now))
  exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(payment_id,'POLICY_BLOCK',json.dumps({'action':rec['action'],'reasons':reasons}),now))
  return {'ok':False,'status':'blocked','action':rec['action'],'reasons':reasons,'score':rec['score'],'amount':p['amount']}
 try:
  result=payment_provider.execute(rec['action'],dict(p)); success=bool(result.get('success')); conn=get_conn()
  if rec['action']=='RETRY_PAYMENT': conn.execute('UPDATE payments SET retry_count=retry_count+1,status=?,recovered_at=? WHERE id=?',('captured' if success else 'failed',now if success else None,payment_id))
  elif rec['action']=='SCHEDULE_RETRY':
   conn.execute('UPDATE payments SET retry_count=retry_count+1 WHERE id=?',(payment_id,)); conn.execute('UPDATE recovery_cases SET next_action_at=?, channel=? WHERE payment_id=?',((datetime.now(timezone.utc)+timedelta(hours=6)).isoformat(),'scheduled_retry',payment_id))
  elif rec['action']=='SEND_REMINDER': conn.execute('UPDATE payments SET reminder_count=reminder_count+1 WHERE id=?',(payment_id,))
  rc_status='recovered' if success and rec['action']=='RETRY_PAYMENT' else 'action_completed' if success else 'failed'
  rev=p['amount'] if rc_status=='recovered' else 0
  conn.execute('UPDATE recovery_cases SET actual_action=?,status=?,revenue_recovered=?,updated_at=? WHERE payment_id=?',(rec['action'],rc_status,rev,now,payment_id)); conn.commit(); conn.close()
  exec_sql('INSERT INTO agent_actions(payment_id,action,reason,confidence,policy_result,api_result,success,created_at) VALUES(?,?,?,?,?,?,?,?)',(payment_id,rec['action'],rec['explanation'],rec['score'],'PASSED',json.dumps(result),1 if success else 0,now))
  exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(payment_id,'ACTION_EXECUTED',json.dumps({'action':rec['action'],'amount':p['amount'],'result':result,'score':rec['score']}),now))
  return {'ok':success,'status':rc_status,'action':rec['action'],'score':rec['score'],'result':result,'policy_reasons':reasons,'amount':p['amount']}
 except Exception as e:
  exec_sql('INSERT INTO agent_actions(payment_id,action,reason,confidence,policy_result,api_result,success,created_at) VALUES(?,?,?,?,?,?,?,?)',(payment_id,rec['action'],rec['explanation'],rec['score'],'PASSED','Tool error: '+str(e),0,now))
  exec_sql('INSERT INTO audit_logs(payment_id,event,details,created_at) VALUES(?,?,?,?)',(payment_id,'GRACEFUL_FAILURE',json.dumps({'action':rec['action'],'error':str(e),'fallback':'ESCALATE'}),now))
  return {'ok':False,'status':'graceful_failure','action':rec['action'],'score':rec['score'],'error':str(e),'fallback':'ESCALATE','amount':p['amount']}

def run_batch(limit=100):
 rows=q("SELECT id FROM payments WHERE status='failed' ORDER BY (amount) * 1 DESC LIMIT ?",(limit,)); stats={'processed':0,'recovered':0,'revenue_recovered':0.0,'blocked':0,'failed':0,'completed':0}
 for r in rows:
  out=execute(r['id']); stats['processed']+=1
  if out['status']=='recovered': stats['recovered']+=1; stats['revenue_recovered']+=float(q('SELECT revenue_recovered FROM recovery_cases WHERE payment_id=?',(r['id'],),one=True)['revenue_recovered'])
  elif out['status']=='blocked':stats['blocked']+=1
  elif out['status']=='completed':stats['completed']+=1
  else:stats['failed']+=1
 return stats
