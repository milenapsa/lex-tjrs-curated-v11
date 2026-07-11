
from __future__ import annotations
import json, os, re, time, urllib.request, urllib.parse, uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv("PORT","8080"))
UPSTREAM=os.getenv("LEX_UPSTREAM","http://homosapiens-lex-tjsc-curated-v10:8080")
VERSION="0.11.0-tjrs-curated"
TTL=1800
UA="Lex-HomoSapiens/0.11"
TJRS_URL="https://www.tjrs.jus.br/novo/jurisprudencia-e-legislacao/jurisprudencia/sumulas/turmas-recursais-da-fazenda-publica/"
TJRS_INDEX="https://www.tjrs.jus.br/novo/jurisprudencia-e-legislacao/jurisprudencia/sumulas/"
_cache={}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.buf=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style","nav","header","footer"}: self.skip+=1
    def handle_endtag(self,tag):
        if tag in {"script","style","nav","header","footer"} and self.skip: self.skip-=1
        if tag in {"p","li","h1","h2","h3","h4","div","br","td"} and not self.skip: self.buf.append("\n")
    def handle_data(self,data):
        if not self.skip: self.buf.append(data)

def fetch_text():
    hit=_cache.get("tjrs")
    if hit and time.time()-hit[0] < TTL: return hit[1]
    req=urllib.request.Request(TJRS_URL,headers={"User-Agent":UA,"Accept":"text/html"})
    with urllib.request.urlopen(req,timeout=20) as r:
        raw=r.read(2_000_000).decode("utf-8","replace")
    p=TextParser(); p.feed(raw)
    text=re.sub(r"[ \t]+"," ","".join(p.buf))
    text=re.sub(r"\n{2,}","\n",text)
    _cache["tjrs"]=(time.time(),text)
    return text

STOP={"de","da","do","das","dos","e","a","o","em","para","por","com","um","uma","no","na","nos","nas","lei","art"}
def tokens(q):
    return [x for x in re.findall(r"[a-z0-9áéíóúâêôãõç]+",q.lower()) if len(x)>2 and x not in STOP]

def tjrs_search(query,limit):
    text=fetch_text()
    # capture quoted statement followed by Súmula N and optional process data
    pat=re.compile(r'[“"](?P<body>[^”"]{35,1400})[”"]\s*Súmula\s+(?P<num>\d+)(?P<meta>.{0,420}?)(?=(?:[“"]|$))',re.I|re.S)
    toks=tokens(query); rows=[]
    for m in pat.finditer(text):
        body=re.sub(r"\s+"," ",m.group("body")).strip()
        meta=re.sub(r"\s+"," ",m.group("meta")).strip()
        low=(body+" "+meta).lower()
        score=sum(1 for t in toks if t in low)
        if score and (len(toks)<=1 or score>=min(2,len(toks))):
            cnj=re.search(r'\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b',meta)
            rows.append((score,{
                "id":f"tjrs-trfp-sumula:{m.group('num')}",
                "title":f"TJRS — Súmula {m.group('num')} das Turmas Recursais da Fazenda Pública",
                "summary":body,
                "type":"sumula_tjrs_turma_recursal_fazenda",
                "date":"",
                "organization":"Tribunal de Justiça do Estado do Rio Grande do Sul",
                "source":"tjrs_sumulas_tr_fazenda",
                "source_label":"TJRS — Súmulas das Turmas Recursais da Fazenda Pública",
                "source_url":TJRS_URL,
                "official_url":TJRS_URL,
                "processo_cnj":cnj.group(0) if cnj else None,
                "metadata":meta[:420],
                "is_official":True,
                "is_synthetic":False,
                "retrieved_at":now(),
                "match_score":score
            }))
    rows.sort(key=lambda x:(-x[0],x[1]["title"]))
    return [r for _,r in rows[:limit]],{
        "source":"tjrs_sumulas_tr_fazenda","status":"ok","count":min(len(rows),limit),
        "request_url":TJRS_URL,"cache_ttl_seconds":TTL
    }

def fetch_json(url,method="GET",payload=None):
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    headers={"User-Agent":UA,"Accept":"application/json"}
    if body is not None: headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode())

SOURCES=[
 {"id":"tjrs_sumulas_tr_fazenda","name":"TJRS — Súmulas das Turmas Recursais da Fazenda Pública","status":"online","coverage":["sumulas","turmas_recursais","fazenda_publica"],"official":True,"requires_secret":False,"url":TJRS_URL},
 {"id":"tjrs_portal_jurisprudencia","name":"TJRS — Pesquisa de Jurisprudência","status":"manual_official_portal","coverage":["acordaos","decisoes","ementas"],"official":True,"requires_secret":False,"url":"https://www.tjrs.jus.br/buscas/jurisprudencia/?aba=jurisprudencia","automation_note":"Mecanismo Solr não automatizado; robots.txt desautoriza /busca."},
 {"id":"tjrs_sumulas_index","name":"TJRS — Índice oficial de Súmulas","status":"online_reference","coverage":["sumulas","suplementos"],"official":True,"requires_secret":False,"url":TJRS_INDEX}
]

def interleave(items,limit):
    groups=defaultdict(deque); order=[]
    for x in items:
        s=x.get("source","unknown")
        if s not in groups: order.append(s)
        groups[s].append(x)
    out=[]
    while len(out)<limit and any(groups[s] for s in order):
        for s in order:
            if groups[s] and len(out)<limit: out.append(groups[s].popleft())
    return out

def run_search(path,payload):
    started=time.monotonic()
    q=str(payload.get("query") or payload.get("q") or "").strip()
    limit=max(1,min(int(payload.get("limit",10)),20))
    base=fetch_json(UPSTREAM+("/v1/search" if path=="/v1/search" else path),"POST",payload)
    results=list(base.get("results") or []); evidence=list(base.get("evidence") or [])
    try:
        found,proof=tjrs_search(q,limit); results.extend(found); evidence.append(proof)
    except Exception as exc:
        evidence.append({"source":"tjrs_sumulas_tr_fazenda","status":"error","error_type":exc.__class__.__name__,"message":str(exc)[:200]})
    seen=set(); dedup=[]
    for x in results:
        k=(x.get("source"),x.get("id"),x.get("title"))
        if k in seen: continue
        seen.add(k); dedup.append(x)
    final=interleave(dedup,limit)
    return {
      "status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),
      "trace_id":str(uuid.uuid4()),"query":q,"scope":base.get("scope","all"),
      "result_count":len(final),"results":final,"evidence":evidence,
      "sources_used":sorted({x.get("source") for x in final if x.get("source")}),
      "integrity":{"official":sum(1 for x in final if x.get("is_official")),
                   "synthetic":sum(1 for x in final if x.get("is_synthetic")),
                   "source_urls_present":sum(1 for x in final if x.get("source_url"))},
      "warnings":list(base.get("warnings") or []),
      "human_review_required":True,"no_invention_policy":True,
      "duration_ms":int((time.monotonic()-started)*1000)
    }

class H(BaseHTTPRequestHandler):
    def sendj(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(data)
    def body(self):
        n=int(self.headers.get("Content-Length","0") or 0)
        if n>64000: raise ValueError("payload_too_large")
        return json.loads((self.rfile.read(n) if n else b"{}").decode())
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        online=["camara_proposicoes","senado_processos","senado_legislacao","tse_ckan","tjsc_sumulas","tjsc_enunciados","tjrs_sumulas_tr_fazenda"]
        if p in {"/health","/v1/health"}:
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"real_sources_online":online,"human_review_required":True,"no_invention_policy":True})
        if p in {"/ready","/v1/readiness"}:
            return self.sendj(200,{"status":"ready","version":VERSION,"online_sources":online,"generated_at":now()})
        if p in {"/v1/sources","/v1/sources/registry"}:
            base=fetch_json(UPSTREAM+"/v1/sources")
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"sources":list(base.get("sources") or [])+SOURCES,"human_review_required":True,"no_invention_policy":True})
        self.sendj(404,{"error":"not_found"})
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if p not in {"/v1/search","/v1/search/global","/v1/search/legislacao","/v1/search/datasets"}:
            return self.sendj(404,{"error":"not_found"})
        try:
            payload=self.body()
            if not str(payload.get("query") or payload.get("q") or "").strip():
                return self.sendj(422,{"error":"query_required"})
            self.sendj(200,run_search(p,payload))
        except Exception as exc:
            self.sendj(500,{"error":"tjrs_curated_connector_error","detail":exc.__class__.__name__})
    def log_message(self,*args): pass

ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
