"""Hosted checkout adapters. No card details or provider secrets enter the browser."""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from decimal import Decimal


class PaymentError(Exception): pass


def configured():
    required={'stripe':['STRIPE_SECRET_KEY','STRIPE_WEBHOOK_SECRET'],
              'paypal':['PAYPAL_CLIENT_ID','PAYPAL_CLIENT_SECRET','PAYPAL_WEBHOOK_ID'],
              'heleket':['HELEKET_MERCHANT_ID','HELEKET_API_KEY']}
    return {p:all(os.environ.get(k) for k in keys) for p,keys in required.items()}


def call(url, method='GET', body=None, headers=None):
    request=urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request,timeout=25) as response:
            return json.loads(response.read(2*1024*1024))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise PaymentError('The payment provider could not complete this request. Retry the same payment method or contact support. No payment is assumed successful.') from error


def json_body(value):return json.dumps(value,separators=(',',':'),ensure_ascii=False).encode()
def dollars(order):return f"{order['total']/100:.2f}"
def exact(amount,order):
    try:return Decimal(str(amount)).is_finite() and Decimal(str(amount))==Decimal(order['total'])/100
    except Exception:return False


def stripe(path, data=None, key=None):
    headers={'Authorization':'Bearer '+os.environ['STRIPE_SECRET_KEY']}
    if key:headers['Idempotency-Key']=key
    body=None
    if data is not None:
        body=urllib.parse.urlencode(data).encode();headers['Content-Type']='application/x-www-form-urlencoded'
    return call('https://api.stripe.com/v1/'+path,'POST' if data is not None else 'GET',body,headers)


def paypal(path,body=None,key=None,method=None):
    base='https://api-m.paypal.com' if os.environ.get('PAYPAL_MODE')=='live' else 'https://api-m.sandbox.paypal.com'
    auth=base64.b64encode((os.environ['PAYPAL_CLIENT_ID']+':'+os.environ['PAYPAL_CLIENT_SECRET']).encode()).decode()
    token=call(base+'/v1/oauth2/token','POST',b'grant_type=client_credentials',{'Authorization':'Basic '+auth,'Content-Type':'application/x-www-form-urlencoded'})['access_token']
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Prefer':'return=representation'}
    if key:headers['PayPal-Request-Id']=key
    return call(base+path,method or ('POST' if body is not None else 'GET'),json_body(body) if body is not None else None,headers)


def heleket(path,data):
    body=json_body(data)
    signature=hashlib.md5(base64.b64encode(body)+os.environ['HELEKET_API_KEY'].encode()).hexdigest()
    response=call('https://api.heleket.com/v1/'+path,'POST',body,{'merchant':os.environ['HELEKET_MERCHANT_ID'],'sign':signature,'Content-Type':'application/json'})
    if response.get('state')!=0 or not isinstance(response.get('result'),dict):raise PaymentError('Heleket did not accept the invoice. Check the merchant settings and currency in the provider dashboard.')
    return response['result']


def create(order,provider,base,attempt):
    oid=order['id'];target=base+'/billing/'+oid
    if provider=='stripe':
        result=stripe('checkout/sessions',{'mode':'payment','payment_method_types[0]':'card',
            'client_reference_id':oid,'metadata[order_id]':oid,'payment_intent_data[metadata][order_id]':oid,
            'success_url':target,'cancel_url':target,
            'line_items[0][quantity]':1,'line_items[0][price_data][currency]':order['currency'].lower(),
            'line_items[0][price_data][unit_amount]':order['total'],
            'line_items[0][price_data][product_data][name]':'Order '+oid[:10],
            'expires_at':attempt['created']+3600},attempt['id'])
        return result['id'],result['url']
    if provider=='paypal':
        address=json.loads(order['address'])
        unit={'reference_id':oid,'custom_id':oid,'amount':{'currency_code':order['currency'],'value':dollars(order)}}
        if address:
            unit['shipping']={'name':{'full_name':address['recipient']},'address':{'address_line_1':address['street'],'admin_area_2':address['city'],'admin_area_1':address['region'],'postal_code':address['postal_code'],'country_code':address['country']}}
        result=paypal('/v2/checkout/orders',{'intent':'CAPTURE','purchase_units':[unit],
            'payment_source':{'paypal':{'experience_context':{'return_url':base+'/payments/paypal/return?order='+oid,'cancel_url':target,'user_action':'PAY_NOW','shipping_preference':'SET_PROVIDED_ADDRESS' if address else 'NO_SHIPPING'}}}},attempt['id'])
        url=next((x['href'] for x in result.get('links',[]) if x['rel'] in ['payer-action','approve']),None)
        if not url:raise PaymentError('PayPal did not return an approval link.')
        return result['id'],url
    if provider=='heleket':
        result=heleket('payment',{'amount':dollars(order),'currency':order['currency'],'order_id':oid,
            'url_return':target,'url_success':target,'url_callback':base+'/webhooks/heleket',
            'lifetime':3600,'accuracy_payment_percent':0,'is_payment_multiple':True})
        return result['uuid'],result['url']
    raise PaymentError('Unknown payment provider.')


def inspect(order,attempt,capture=False):
    provider=attempt['provider'];external=attempt['external_id']
    if not external:raise PaymentError('The checkout link has not been created yet. Retry your selected provider.')
    if provider=='stripe':
        r=stripe('checkout/sessions/'+urllib.parse.quote(external,safe=''))
        if r.get('client_reference_id')!=order['id'] or r.get('amount_total')!=order['total'] or r.get('currency','').upper()!=order['currency']:raise PaymentError('Payment details do not match this order.')
        if r.get('payment_status')=='paid':
            pi=r.get('payment_intent')
            if pi:
                intent=stripe('payment_intents/'+urllib.parse.quote(pi,safe=''))
                charge=intent.get('latest_charge')
                if charge:
                    charge=stripe('charges/'+urllib.parse.quote(charge,safe=''))
                    if charge.get('refunded') or charge.get('amount_refunded',0)>0 or charge.get('disputed'):return 'review',str(pi)
            return 'paid',str(pi or external)
        return ('expired' if r.get('status')=='expired' else 'pending'),external
    if provider=='paypal':
        r=paypal('/v2/checkout/orders/'+urllib.parse.quote(external,safe=''))
        if r.get('status')=='APPROVED' and capture:
            approved=r.get('purchase_units',[])
            if len(approved)!=1 or approved[0].get('custom_id')!=order['id'] or approved[0].get('amount',{}).get('currency_code')!=order['currency'] or not exact(approved[0].get('amount',{}).get('value'),order):
                raise PaymentError('PayPal approval does not match this purchase.')
            paypal('/v2/checkout/orders/'+urllib.parse.quote(external,safe='')+'/capture',{},attempt['id']+'-capture')
            # Capture responses can omit purchase-unit amount/custom_id. Retrieve the
            # canonical order again instead of assuming the capture response shape.
            r=paypal('/v2/checkout/orders/'+urllib.parse.quote(external,safe=''))
        units=r.get('purchase_units',[])
        if len(units)!=1 or units[0].get('custom_id')!=order['id']:raise PaymentError('PayPal order reference mismatch.')
        unit=units[0]
        if unit.get('amount',{}).get('currency_code')!=order['currency'] or not exact(unit.get('amount',{}).get('value'),order):raise PaymentError('PayPal amount or currency mismatch.')
        captures=unit.get('payments',{}).get('captures',[])
        if any(c.get('status') in ['REFUNDED','PARTIALLY_REFUNDED','DECLINED'] for c in captures):return 'review',external
        if r.get('status')=='COMPLETED' and len(captures)==1:
            c=captures[0]
            if c.get('status')=='COMPLETED' and c.get('amount',{}).get('currency_code')==order['currency'] and exact(c.get('amount',{}).get('value'),order):return 'paid',c['id']
        return ('expired' if r.get('status')=='VOIDED' else 'pending'),external
    r=heleket('payment/info',{'uuid':external})
    if r.get('order_id')!=order['id'] or str(r.get('currency','')).upper()!=order['currency'] or not exact(r.get('amount'),order):raise PaymentError('Heleket amount or reference mismatch.')
    status=r.get('payment_status',r.get('status'))
    if status in ['paid','paid_over']:return 'paid',external
    if status in ['refund_process','refund_paid','refund_fail']:return 'review',external
    return ('expired' if status in ['cancel','fail','system_fail','wrong_amount'] and r.get('is_final') else 'pending'),external


def verify_stripe(raw,header):
    secret=os.environ.get('STRIPE_WEBHOOK_SECRET','')
    parts={}
    for piece in header.split(','):
        if '=' in piece:
            k,v=piece.split('=',1);parts.setdefault(k,[]).append(v)
    try:timestamp=int(parts['t'][0])
    except (KeyError,ValueError,IndexError):return False
    if not secret or abs(time.time()-timestamp)>300:return False
    expected=hmac.new(secret.encode(),str(timestamp).encode()+b'.'+raw,hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected,v) for v in parts.get('v1',[]))


def verify_heleket(event):
    key=os.environ.get('HELEKET_API_KEY','');payload=dict(event);supplied=str(payload.pop('sign',''))
    if not key or not supplied:return False
    encoded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('/','\\/').encode()
    expected=hashlib.md5(base64.b64encode(encoded)+key.encode()).hexdigest()
    return hmac.compare_digest(expected,supplied)


def verify_paypal(event,headers):
    if not os.environ.get('PAYPAL_WEBHOOK_ID'):return False
    payload={'auth_algo':headers.get('PAYPAL-AUTH-ALGO'),'cert_url':headers.get('PAYPAL-CERT-URL'),
             'transmission_id':headers.get('PAYPAL-TRANSMISSION-ID'),'transmission_sig':headers.get('PAYPAL-TRANSMISSION-SIG'),
             'transmission_time':headers.get('PAYPAL-TRANSMISSION-TIME'),'webhook_id':os.environ['PAYPAL_WEBHOOK_ID'],'webhook_event':event}
    # Certificate URLs are forwarded to PayPal's verification API; this server never fetches them.
    return paypal('/v1/notifications/verify-webhook-signature',payload).get('verification_status')=='SUCCESS'
