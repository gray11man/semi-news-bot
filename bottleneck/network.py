"""Live retrieval. Feed snippets discover ideas; only fetched bodies verify them."""
import concurrent.futures
import datetime as dt
import email.utils
import html
import ipaddress
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import UTC, canonical, digest, now, primary

class FetchError(RuntimeError): pass
class ApiError(RuntimeError): pass
class BudgetError(ApiError): pass
class RequestTooLarge(BudgetError): pass


def public_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise FetchError('Invalid public URL')
    if p.port not in (None,80,443): raise FetchError('Unsupported port')
    try:
        addresses=socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme=='https' else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise FetchError('Non-public address')
    except (socket.gaierror,ValueError):
        raise FetchError('Address lookup failed') from None
    return url

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get(url, limit=2500000):
    public_url(url)
    request=urllib.request.Request(url, headers={'User-Agent':'IndustryShiftResearch/1.0'})
    try:
        with urllib.request.build_opener(Redirect()).open(request, timeout=18) as r:
            data=r.read(limit+1)
            if len(data)>limit: raise FetchError('Response exceeds limit')
            return data, r.url, r.headers.get_content_type()
    except (OSError,urllib.error.URLError):
        raise FetchError('Source request failed') from None


def rss_url(query, lang='en', days=3):
    locale={'ko':('ko','KR','KR:ko'), 'en':('en-US','US','US:en'),
            'ja':('ja','JP','JP:ja'), 'zh':('zh-TW','TW','TW:zh-Hant')}[lang]
    return 'https://news.google.com/rss/search?' + urllib.parse.urlencode(
        dict(q=f'{query} when:{days}d', hl=locale[0], gl=locale[1], ceid=locale[2]))


def read_feed(spec, max_entries=60, days=3):
    data, _, _=get(spec['url'])
    try: root=ET.fromstring(data)
    except ET.ParseError: raise FetchError('RSS parsing failed') from None
    if root.tag not in ('rss','{http://www.w3.org/2005/Atom}feed'):
        raise FetchError('Not a feed')
    out=[]; current=dt.datetime.now(UTC)
    nodes=root.findall('./channel/item') or root.findall('{http://www.w3.org/2005/Atom}entry')
    for e in nodes[:max_entries]:
        atom='{http://www.w3.org/2005/Atom}'
        title=e.findtext('title') or e.findtext(atom+'title') or ''
        link=e.findtext('link')
        if not link:
            node=e.find(atom+'link'); link=node.get('href','') if node is not None else ''
        date=e.findtext('pubDate') or e.findtext(atom+'published') or e.findtext(atom+'updated')
        try:
            try: published=email.utils.parsedate_to_datetime(date)
            except (TypeError,ValueError): published=dt.datetime.fromisoformat(date.replace('Z','+00:00'))
            if published.tzinfo is None: published=published.replace(tzinfo=UTC)
        except (ValueError,TypeError,AttributeError): continue
        if not dt.timedelta(hours=-1) <= current-published <= dt.timedelta(days=days): continue
        if not title or urllib.parse.urlsplit(link).scheme not in ('http','https'): continue
        summary=e.findtext('description') or e.findtext(atom+'summary') or ''
        import re
        summary=html.unescape(re.sub('<[^>]+>',' ',summary))[:1000]
        out.append(dict(id=digest(canonical(link)),title=html.unescape(title)[:400],url=link,
                        published=published.isoformat(),summary=summary,sector=spec['sector'],
                        publisher=e.findtext('source') or ''))
    return out


def collect(specs, days=3):
    items={}; errors=[]; healthy=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pending={pool.submit(read_feed,s,60,days):s for s in specs}
        for future in concurrent.futures.as_completed(pending):
            spec=pending[future]
            try:
                for a in future.result(): items.setdefault(a['id'],a)
                healthy+=1
            except Exception:
                errors.append(spec['sector'])
    return list(items.values()), dict(total=len(specs),healthy=healthy,failed=errors)


def original_url(url):
    if urllib.parse.urlsplit(url).hostname!='news.google.com': return url
    # Decoder runs in a separate process: an upstream hang cannot stall the whole bot.
    script=('import json,sys; from googlenewsdecoder import gnewsdecoder; '
            'print(json.dumps(gnewsdecoder(sys.argv[1],interval=1)))')
    try:
        r=subprocess.run([sys.executable,'-c',script,url],capture_output=True,text=True,timeout=35)
        decoded=json.loads(r.stdout)
        if decoded.get('status'): return public_url(decoded['decoded_url'])
    except (subprocess.TimeoutExpired,ValueError,KeyError,FetchError): pass
    raise FetchError('Google News original URL unresolved')


def fetch_body(article, primary_domains):
    url=original_url(article['url'])
    data, final, mime=get(url)
    if mime not in ('text/html','application/xhtml+xml','text/plain'): raise FetchError('Unsupported source format')
    import trafilatura
    text=trafilatura.extract(data,include_comments=False,include_tables=True,favor_precision=True)
    if not text or len(text)<300: raise FetchError('Article body unavailable')
    return dict(id=article['id'],title=article['title'],url=final,feed_date=article['published'],
                fetched=now(),text=text[:16000],status='body',primary=primary(final,primary_domains))


class Gemini:
    def __init__(self,key,model,max_calls=10):
        self.key=key; self.model=model; self.max_calls=max_calls; self.calls=0; self.tokens=0
        self.budget=None; self.input_limit=36000; self.output_limit=8192; self.count_calls=0; self.usage=[]
        import re
        if not re.fullmatch(r'[A-Za-z0-9._-]+',model): raise ApiError('Invalid model name')

    def count(self,payload):
        self.count_calls+=1
        # countTokens accepts either `contents` or a GenerateContentRequest.
        # Send only the input-bearing fields: generationConfig is not needed
        # for counting and some model versions reject output-only options here.
        count_request={
            'model':'models/'+self.model,
            'contents':payload['contents'],
            'systemInstruction':payload.get('systemInstruction'),
        }
        request=urllib.request.Request(
            f'https://generativelanguage.googleapis.com/v1beta/models/{self.model}:countTokens',
            data=json.dumps({'generateContentRequest':count_request},ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json','x-goog-api-key':self.key})
        try:
            with urllib.request.urlopen(request,timeout=40) as r: result=json.load(r)
            value=result['totalTokens']
            if not isinstance(value,int) or value<0:raise ValueError()
            return value
        except urllib.error.HTTPError as e:
            # Keep provider details out of logs (they can contain request data),
            # but expose the status so a bad key/model is distinguishable from
            # a transient network failure.
            detail=self._http_detail(e)
            suffix=f': {detail}' if detail else ''
            raise ApiError(f'Token count HTTP {e.code}{suffix}; check access/quota/model') from None
        except (OSError,ValueError,KeyError):
            raise ApiError('Token count failed; generation blocked to protect budget') from None

    def json(self,system,data,schema):
        import jsonschema
        output_limit=4096 if 'checks' in schema.get('properties',{}) else self.output_limit
        # Gemini accepts a smaller JSON-Schema subset for structured output.
        # Keep the full schema for local validation, but omit constraints that
        # the API does not support (for example maxLength).
        api_schema=self._api_schema(schema)
        if self.model.startswith('gemini-3'):
            config={'maxOutputTokens':output_limit,
                    'responseFormat':{'text':{'mimeType':'application/json','schema':api_schema}}}
        else:
            config={'maxOutputTokens':output_limit,
                    'responseMimeType':'application/json',
                    # The legacy GenerateContent Schema uses enum values such
                    # as OBJECT/STRING in raw REST JSON, unlike JSON Schema.
                    'responseSchema':self._legacy_schema(api_schema)}
        payload=dict(systemInstruction={'parts':[{'text':system}]},
                     contents=[{'role':'user','parts':[{'text':json.dumps(data,ensure_ascii=False)}]}],
                     generationConfig=config)
        input_tokens=self.count(payload)
        if input_tokens>self.input_limit:raise RequestTooLarge('Per-request input token limit reached; reduce batch/excerpt size')
        # A few model/version combinations reject an otherwise valid
        # responseSchema with HTTP 400.  Keep the strict schema as the first
        # attempt, then retry once in JSON mode (the response is still checked
        # against the full local jsonschema below).  Reuse the same budget
        # reservation so a rejected request is never double-counted.
        fallback_used=False
        reuse_receipt=None
        first_400_detail=''
        for attempt in range(3):
            if self.calls>=self.max_calls: raise BudgetError('Gemini call budget exhausted; backlog retained')
            if reuse_receipt is not None:
                receipt,reuse_receipt=reuse_receipt,None
            else:
                receipt=self.budget.reserve(input_tokens+output_limit+2048) if self.budget else None
            self.calls+=1
            request=urllib.request.Request(
                f'https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent',
                data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','x-goog-api-key':self.key})
            try:
                with urllib.request.urlopen(request,timeout=150) as r: result=json.load(r)
            except urllib.error.HTTPError as e:
                code=e.code
                if code in (429,500,502,503,504) and attempt<2:
                    try: delay=min(45,max(1,float(e.headers.get('Retry-After','10'))))
                    except ValueError: delay=10
                    time.sleep(delay); continue
                detail=self._http_detail(e)
                if code==400 and detail and not first_400_detail:
                    first_400_detail=detail
                detail_l=detail.lower()
                schema_error=(not detail or any(x in detail_l for x in
                    ('schema','responseformat','response format','generationconfig',
                     'generation config','additionalproperties','unknown name',
                     'invalid argument','json payload')))
                if code==400 and not fallback_used and schema_error and attempt<2:
                    fallback_used=True
                    payload=dict(payload,generationConfig={
                        'maxOutputTokens':output_limit,
                        'responseMimeType':'application/json'})
                    reuse_receipt=receipt
                    continue
                if not detail and first_400_detail:
                    detail=first_400_detail
                if not detail and code==400:
                    mode='json-fallback' if fallback_used else 'structured'
                    detail=f'provider detail unavailable (model={self.model}, mode={mode})'
                suffix=f': {detail}' if detail else ''
                raise ApiError(f'Gemini HTTP {code}{suffix}; check access/quota/model') from None
            except (OSError,ValueError):
                raise ApiError('Gemini response unavailable') from None
            usage=result.get('usageMetadata',{})
            actual=usage.get('totalTokenCount')
            if self.budget:self.budget.settle(receipt,actual)
            self.tokens+=actual if isinstance(actual,int) else input_tokens+output_limit+2048
            self.usage.append(dict(input=usage.get('promptTokenCount'),output=usage.get('candidatesTokenCount'),thinking=usage.get('thoughtsTokenCount'),total=actual))
            candidates=result.get('candidates',[])
            if not candidates or candidates[0].get('finishReason')!='STOP':
                raise ApiError('Incomplete Gemini response; not an empty result')
            raw=''.join(p.get('text','') for p in candidates[0].get('content',{}).get('parts',[]) if not p.get('thought'))
            try:
                obj=json.loads(raw); jsonschema.validate(obj,schema)
            except (ValueError,jsonschema.ValidationError):
                raise ApiError('Gemini schema validation failed') from None
            return obj
        raise ApiError('Gemini retries exhausted')

    @staticmethod
    def _api_schema(value):
        """Remove JSON-Schema keywords unsupported by Gemini structured output."""
        if isinstance(value,dict):
            unsupported={'maxLength','minLength','pattern','formatMinimum','formatMaximum',
                         'exclusiveMinimum','exclusiveMaximum','multipleOf','default','examples',
                         # `additionalProperties` belongs to full JSON Schema,
                         # but is not a field of the legacy Gemini Schema used
                         # by responseSchema (the 2.x models).
                         'additionalProperties'}
            return {k:Gemini._api_schema(v) for k,v in value.items() if k not in unsupported}
        if isinstance(value,list):
            return [Gemini._api_schema(v) for v in value]
        return value

    @staticmethod
    def _legacy_schema(value,key=None):
        """Convert JSON-Schema type names to the REST Schema enum spelling."""
        if key=='type' and isinstance(value,str):
            return value.upper()
        if isinstance(value,dict):
            return {k:Gemini._legacy_schema(v,k) for k,v in value.items()}
        if isinstance(value,list):
            return [Gemini._legacy_schema(v) for v in value]
        return value

    @staticmethod
    def _http_detail(error):
        """Extract only Google's short error message; never log request data."""
        try:
            body=error.read(4096).decode('utf-8','replace')
            result=json.loads(body)
            detail=result.get('error',{}).get('message','')
            if isinstance(detail,str):
                import re
                return re.sub(r'\s+',' ',detail).strip()[:500]
        except (OSError,ValueError,AttributeError,KeyError,TypeError):
            pass
        return ''


def telegram(text,token,chat):
    request=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',
        data=json.dumps(dict(chat_id=chat,text=text,link_preview_options={'is_disabled':True})).encode(),
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=30) as r: data=json.load(r)
    except urllib.error.HTTPError as e:
        if 400<=e.code<500: return 'pending', None
        return 'uncertain', None
    except (OSError,ValueError): return 'uncertain', None
    if data.get('ok') and data.get('result',{}).get('message_id'):
        return 'sent',data['result']['message_id']
    return 'pending',None
