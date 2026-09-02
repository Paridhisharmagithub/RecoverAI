from app.db import q
ACTIONS=['RETRY_PAYMENT','SCHEDULE_RETRY','SEND_REMINDER','GENERATE_PAYMENT_LINK','ESCALATE','STOP']

def settings(): return dict(q('SELECT * FROM settings WHERE id=1',one=True))

def gate(payment, action):
    s=settings(); reasons=[]; allowed=True
    if action not in ACTIONS: return False,['Unknown action is never allowed.']
    if action in ['RETRY_PAYMENT','SCHEDULE_RETRY','GENERATE_PAYMENT_LINK','SEND_REMINDER'] and not s['auto_recovery_enabled']:
        allowed=False; reasons.append('Automatic recovery is disabled by merchant.')
    if payment['fraud_flag'] and action not in ['ESCALATE','STOP']:
        allowed=False; reasons.append('Fraud flag requires human review.')
    if payment['amount']>s['high_value_threshold'] and action not in ['ESCALATE','STOP']:
        allowed=False; reasons.append(f"Amount exceeds high-value threshold of ₹{s['high_value_threshold']:,.0f}.")
    if action in ['RETRY_PAYMENT','SCHEDULE_RETRY'] and payment['retry_count']>=s['max_retry_count']:
        allowed=False; reasons.append('Retry limit reached.')
    if action=='SEND_REMINDER' and payment['reminder_count']>=s['max_reminders']:
        allowed=False; reasons.append('Reminder limit reached.')
    if action in ['RETRY_PAYMENT','SCHEDULE_RETRY'] and payment['amount']>s['max_auto_amount']:
        allowed=False; reasons.append('Amount exceeds automatic recovery limit.')
    if not reasons: reasons.append('All merchant policy checks passed.')
    return allowed,reasons
