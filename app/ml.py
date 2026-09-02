from pathlib import Path
import json, joblib, numpy as np, datetime as dt
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score, confusion_matrix, average_precision_score
from sklearn.model_selection import train_test_split
from app.db import q
MODEL_PATH=Path('models/recovery_model.joblib'); METRICS_PATH=Path('models/recovery_metrics.json')
METHOD_MAP={'upi':0,'card':1,'netbanking':2,'wallet':3}; REASON_MAP={'BANK_TIMEOUT':0,'INSUFFICIENT_FUNDS':1,'CARD_EXPIRED':2,'AUTHENTICATION_FAILED':3,'UPI_TIMEOUT':4,'NETWORK_ERROR':5,'UNKNOWN':6}
FEATURES=['amount','method_id','reason_id','retry_count','success_rate','total_txn','fraud_flag','age_hours','subscription_active']
_MODEL=None; _METRICS=None

def feature_vector(r):
    total=float(r['total_txn'] if 'total_txn' in r.keys() else r['total_transactions']); succ=float(r['successful_txn'] if 'successful_txn' in r.keys() else r['successful_transactions'])
    created=dt.datetime.fromisoformat(r['created_at']); created=created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc)
    age=max(0,(dt.datetime.now(dt.timezone.utc)-created).total_seconds()/3600)
    return [float(r['amount']),METHOD_MAP.get(r['method'],0),REASON_MAP.get(r['failure_reason'] or 'UNKNOWN',6),float(r['retry_count']),succ/max(1,total),total,float(r['fraud_flag']),age,float(r['subscription_status']=='active')]

def build_dataset():
    return q('''SELECT p.*, c.total_transactions total_txn, c.successful_transactions successful_txn FROM payments p JOIN customers c ON c.id=p.customer_id WHERE p.status='failed' ORDER BY p.created_at''')

def train():
    global _MODEL,_METRICS
    rows=build_dataset()
    X_all=np.array([feature_vector(r) for r in rows])
    y_all=np.array([int(r['recoverable_label']) for r in rows])
    if len(np.unique(y_all)) < 2:
        raise RuntimeError('Training data contains only one recoverability class.')
    X,Xt,y,yt=train_test_split(X_all,y_all,test_size=.2,random_state=42,stratify=y_all)
    model=RandomForestClassifier(n_estimators=240,max_depth=10,min_samples_leaf=4,random_state=42,class_weight='balanced',n_jobs=-1)
    model.fit(X,y)
    classes=list(model.classes_)
    if 1 in classes:
        prob=model.predict_proba(Xt)[:,classes.index(1)]
    else:
        prob=np.zeros(len(Xt))
    pred=(prob>=.5).astype(int)
    tn,fp,fn,tp=confusion_matrix(yt,pred,labels=[0,1]).ravel()
    metrics={'precision':round(float(precision_score(yt,pred,zero_division=0)),3),'recall':round(float(recall_score(yt,pred,zero_division=0)),3),'f1':round(float(f1_score(yt,pred,zero_division=0)),3),'roc_auc':round(float(roc_auc_score(yt,prob)),3),'pr_auc':round(float(average_precision_score(yt,prob)),3),'false_positive_cost':round(float(fp*18.0),2),'false_positives':int(fp),'false_negatives':int(fn),'test_size':len(yt),'train_size':len(y)}
    MODEL_PATH.parent.mkdir(exist_ok=True); joblib.dump(model,MODEL_PATH); METRICS_PATH.write_text(json.dumps(metrics,indent=2),encoding='utf-8'); _MODEL,_METRICS=model,metrics; return model,metrics

def load_or_train():
    global _MODEL,_METRICS
    if _MODEL is not None:return _MODEL,_METRICS
    if MODEL_PATH.exists():
        try:
            _MODEL=joblib.load(MODEL_PATH); _METRICS=json.loads(METRICS_PATH.read_text(encoding='utf-8')) if METRICS_PATH.exists() else None
            if _METRICS:return _MODEL,_METRICS
        except Exception: pass
    return train()

def predict_many(rows):
    model,_=load_or_train(); X=np.array([feature_vector(r) for r in rows]); probs=model.predict_proba(X)[:,1]
    return [max(.01,min(.99,float(x))) for x in probs]

def predict(row): return predict_many([row])[0]
