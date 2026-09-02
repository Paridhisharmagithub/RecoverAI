import requests
from app.config import USE_RAZORPAY, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET

class LocalSimulator:
    def execute(self, action, payment):
        if action=='RETRY_PAYMENT':
            if payment['id'].endswith('007'): raise RuntimeError('Temporary provider timeout — failure injected for demo')
            if payment['retry_count']>=2: return {'success':False,'message':'PAYMENT_RETRY_LIMIT_EXCEEDED','provider':'local-simulator'}
            success=bool(payment['recoverable_label']) and payment['failure_reason'] in ['BANK_TIMEOUT','UPI_TIMEOUT','NETWORK_ERROR'] and not payment['fraud_flag']
            return {'success':success,'message':'Payment recovered' if success else 'Retry did not recover payment','provider':'local-simulator'}
        if action=='SCHEDULE_RETRY': return {'success':True,'message':'Retry scheduled for +6h','provider':'local-simulator'}
        if action=='SEND_REMINDER': return {'success':True,'message':'Reminder queued','provider':'local-simulator','channel':'email'}
        if action=='GENERATE_PAYMENT_LINK': return {'success':True,'message':'Recovery payment link created','provider':'local-simulator','short_url':'https://example.test/recover/'+payment['id']}
        if action=='ESCALATE': return {'success':True,'message':'Escalated to merchant review','provider':'local-simulator'}
        return {'success':True,'message':'No action required','provider':'local-simulator'}

class RazorpayProvider:
    base='https://api.razorpay.com/v1'
    def execute(self, action, payment):
        if action in ['GENERATE_PAYMENT_LINK','SEND_REMINDER']:
            payload={'amount':int(round(payment['amount']*100)),'currency':'INR','accept_partial':False,'reference_id':payment['id'][:40],'description':f'Recovery for {payment["id"]}','customer':{'name':payment['customer_name'],'contact':payment['customer_phone'],'email':payment['customer_email']},'reminder_enable': action=='SEND_REMINDER'}
            r=requests.post(self.base+'/payment_links',json=payload,auth=(RAZORPAY_KEY_ID,RAZORPAY_KEY_SECRET),timeout=15); r.raise_for_status(); data=r.json()
            return {'success':True,'message':'Razorpay Payment Link created','provider':'razorpay','short_url':data.get('short_url'),'id':data.get('id')}
        return {'success':False,'message':f'{action} is not directly exposed as a generic Razorpay retry endpoint; safe fallback is payment-link recovery','provider':'razorpay-adapter'}

payment_provider=RazorpayProvider() if USE_RAZORPAY and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else LocalSimulator()
