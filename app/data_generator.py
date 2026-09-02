from datetime import datetime, timedelta, timezone
import random, math
from app.db import get_conn

SEED=42; N_CUSTOMERS=800; N_PAYMENTS=5000
FAILURE_REASONS=['BANK_TIMEOUT','INSUFFICIENT_FUNDS','CARD_EXPIRED','AUTHENTICATION_FAILED','UPI_TIMEOUT','NETWORK_ERROR','UNKNOWN']
METHODS=['upi','card','netbanking','wallet']
NAMES=['Aarav','Ananya','Arjun','Diya','Ishaan','Meera','Kabir','Aditi','Rahul','Riya','Vivaan','Nisha','Advik','Saanvi','Karan','Myra','Vihaan','Ira']


def sigmoid(x): return 1/(1+math.exp(-x))


def choose_best(reason, success_rate, fraud, amount, retries, subscription):
    if fraud or amount>50000: return 'ESCALATE'
    if reason in ('BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR'): return 'RETRY_PAYMENT'
    if reason=='INSUFFICIENT_FUNDS': return 'SCHEDULE_RETRY'
    if reason in ('CARD_EXPIRED','AUTHENTICATION_FAILED'): return 'GENERATE_PAYMENT_LINK'
    if subscription=='active' and success_rate>.7: return 'SEND_REMINDER'
    return 'ESCALATE'


def seed_if_empty(force=False):
    conn=get_conn()
    if force:
        conn.executescript('DELETE FROM audit_logs; DELETE FROM agent_actions; DELETE FROM recovery_cases; DELETE FROM experiments; DELETE FROM recovery_campaigns; DELETE FROM payments; DELETE FROM customers;')
    if conn.execute('SELECT COUNT(*) c FROM payments').fetchone()['c']:
        conn.close(); return False
    rng=random.Random(SEED); now=datetime.now(timezone.utc)
    customers=[]
    for i in range(1,N_CUSTOMERS+1):
        total=rng.randint(6,35); success=max(1,min(total,rng.randint(int(total*.45),total)))
        name=f'{rng.choice(NAMES)} {chr(65+rng.randint(0,25))}'
        ltv=round(rng.uniform(2500,180000),2)
        segment='vip' if ltv>60000 else 'loyal' if success/total>.78 else 'growth'
        customers.append((f'cus_{i:04d}',name,f'user{i}@example.com',f'+91{9000000000+i}',total,success,ltv,segment))
    conn.executemany('INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)',customers)
    payments=[]
    for i in range(1,N_PAYMENTS+1):
        cid=rng.choice(customers)[0]; cust=next(c for c in customers if c[0]==cid)
        success_rate=cust[5]/cust[4]
        amount=round(rng.choice([499,799,1299,1799,2499,3499,4999,7999,9999,14999,24999,49999])*rng.uniform(.75,1.2),2)
        method=rng.choice(METHODS); created=now-timedelta(hours=rng.randint(1,24*45)); failed=rng.random()<.48
        status='failed' if failed else 'captured'; reason=rng.choice(FAILURE_REASONS) if failed else None
        retries=rng.randint(0,2) if failed else 0; reminders=rng.randint(0,1) if failed else 0
        fraud=1 if (failed and rng.random()<.035) else 0; subscription=rng.choices(['active','inactive','trial'],weights=[.62,.28,.10])[0]
        # Curated demo cases: one guaranteed recovery and one graceful provider failure.
        if i==10:
            failed=True; status='failed'; reason='BANK_TIMEOUT'; retries=0; fraud=0; amount=2499.0; subscription='active'; baseline_eligible=1
        if i==7:
            failed=True; status='failed'; reason='BANK_TIMEOUT'; retries=0; fraud=0; amount=3499.0; subscription='active'; baseline_eligible=1
        baseline_eligible=int(failed and not fraud and retries<2 and amount<=50000)
        best=choose_best(reason,success_rate,fraud,amount,retries,subscription) if failed else 'STOP'
        # Hidden ground truth: probabilistic and deliberately not identical to the model rules.
        reason_bonus={'BANK_TIMEOUT':1.1,'UPI_TIMEOUT':1.0,'NETWORK_ERROR':.8,'INSUFFICIENT_FUNDS':.25,'CARD_EXPIRED':-.5,'AUTHENTICATION_FAILED':-.25,'UNKNOWN':-.8}.get(reason or 'UNKNOWN',-.8)
        z=-.15 + 1.45*(success_rate-.65) + reason_bonus + .35*(subscription=='active') - .32*(retries) - 1.25*fraud - .000008*max(0,amount-10000)
        hidden_prob=max(.03,min(.97,sigmoid(z)))
        if i==10:
            hidden_prob=0.94
        if i==7:
            hidden_prob=0.80
        recoverable=int(rng.random()<hidden_prob and best!='ESCALATE')
        if i in (7,10): recoverable=1
        payments.append((f'pay_{i:06d}',cid,amount,method,status,reason,retries,reminders,fraud,subscription,created.isoformat(),None,recoverable,best,hidden_prob,baseline_eligible))
    conn.executemany('INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',payments)
    conn.commit(); conn.close(); return True
