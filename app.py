import base64, json, queue, socket, threading, time, urllib.parse, urllib.request
import os, subprocess, tempfile, shutil, base64, json, socket, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
APP_DIR=Path(__file__).resolve().parent
XRAY=APP_DIR/'xray.exe'
UP_SIZE=1000000

def fa(s): return s
T={'title':fa('ZMOBGPT \u2014 \u06a9\u0627\u0646\u0641\u06cc\u06af \u062a\u0633\u062a\u0631'),'sub':fa('\u0644\u06cc\u0646\u06a9\u200c\u0647\u0627\u06cc Subscription (\u0647\u0631 \u062e\u0637 \u06cc\u06a9 \u0644\u06cc\u0646\u06a9):'),'start':fa('\u062f\u0631\u06cc\u0627\u0641\u062a \u0648 \u0634\u0631\u0648\u0639 \u062a\u0633\u062a'),'stop':fa('\u062a\u0648\u0642\u0641'),'ready':fa('\u0622\u0645\u0627\u062f\u0647'),'name':fa('\u0646\u0627\u0645'),'protocol':fa('\u067e\u0631\u0648\u062a\u06a9\u0644'),'server':fa('\u0633\u0631\u0648\u0631'),'active':fa('\u0641\u0639\u0627\u0644'),'status':fa('\u0648\u0636\u0639\u06cc\u062a'),'yes':fa('\u0628\u0644\u0647'),'no':fa('\u062e\u06cc\u0631'),'footer':fa('\u0627\u0628\u062a\u062f\u0627 \u0641\u0639\u0627\u0644 \u0628\u0648\u062f\u0646 \u0628\u0631\u0631\u0633\u06cc \u0645\u06cc\u200c\u0634\u0648\u062f\u060c \u0633\u067e\u0633 Upload \u0648\u0627\u0642\u0639\u06cc \u0627\u0632 \u0645\u0633\u06cc\u0631 Xray \u0627\u0646\u062c\u0627\u0645 \u0645\u06cc\u200c\u0634\u0648\u062f.'),'empty':fa('\u062d\u062f\u0627\u0642\u0644 \u06cc\u06a9 \u0644\u06cc\u0646\u06a9 Subscription \u0648\u0627\u0631\u062f \u06a9\u0646\u06cc\u062f.')}
@dataclass
class Config: index:int; raw:str; name:str; scheme:str; host:str=''; port:int=0
@dataclass
class Result: cfg:Config; active:bool=False; latency:float=0; upload:float=0; status:str=''
def decode_sub(text):
    try:
        raw=base64.b64decode(text.strip()+'='*(-len(text.strip())%4),validate=False).decode('utf-8','ignore')
        if '://' in raw:return raw
    except Exception: pass
    return text

def parse_configs(text):
    out=[]
    for i,line in enumerate(decode_sub(text).replace('\r','').splitlines(),1):
        line=line.strip()
        if not line.startswith(('vless://','vmess://','trojan://','ss://')): continue
        try:
            u=urllib.parse.urlsplit(line); q=urllib.parse.parse_qs(u.query)
            name=urllib.parse.unquote(q.get('remarks',[''])[0] or u.fragment or f'Config {i}')
            out.append(Config(i,line,name,u.scheme.lower(),u.hostname or '',u.port or 0))
        except Exception: pass
    return out

def qone(q,k,d=''): return urllib.parse.unquote(q.get(k,[d])[0])
def b64dec(s):
    return base64.b64decode(s+'='*(-len(s)%4)).decode('utf-8','ignore')

def stream_settings(q,net='tcp',sec='none',extra=None):
    extra=extra or {}; net=qone(q,'type',net); sec=qone(q,'security',sec); st={'network':net,'security':sec}
    sni=qone(q,'sni',qone(q,'serverName','')); fp=qone(q,'fp','chrome'); alpn=qone(q,'alpn','')
    if sec=='tls':
        st['tlsSettings']={'serverName':sni,'fingerprint':fp}
        if alpn: st['tlsSettings']['alpn']=alpn.split(',')
    elif sec=='reality':
        st['realitySettings']={'serverName':sni,'fingerprint':fp,'publicKey':qone(q,'pbk',''),'shortId':qone(q,'sid','')}
    if net in ('ws','websocket'):
        h=qone(q,'host',''); w={'path':qone(q,'path','/')}
        if h:w['headers']={'Host':h}
        st['network']='ws';st['wsSettings']=w
    elif net in ('grpc','gun'):
        st['network']='grpc';st['grpcSettings']={'serviceName':qone(q,'serviceName','')}
    elif net in ('http','h2'):
        st['network']='http';st['httpSettings']={'path':qone(q,'path','/'),'host':[qone(q,'host','')] if qone(q,'host','') else []}
    if net=='xhttp':
        st['network']='xhttp';st['xhttpSettings']={'path':qone(q,'path','/')}
    return st
def outbound(c):
    raw=c.raw
    if c.scheme=='vmess':
        d=json.loads(b64dec(raw[8:].split('#')[0])); q={k:[str(v)] for k,v in d.items()}
        host=d.get('add',''); port=int(d.get('port',443)); user={'id':d.get('id',''),'alterId':int(d.get('aid',0) or 0),'security':d.get('scy','auto') or 'auto'}
        o={'protocol':'vmess','settings':{'vnext':[{'address':host,'port':port,'users':[user]}]},'streamSettings':stream_settings(q,d.get('net','tcp'),('tls' if d.get('tls') else 'none'))}
        return o
    u=urllib.parse.urlsplit(raw); q=urllib.parse.parse_qs(u.query); host=u.hostname or c.host; port=u.port or c.port
    if c.scheme=='vless':
        user={'id':urllib.parse.unquote(u.username or ''),'encryption':qone(q,'encryption','none')}
        flow=qone(q,'flow','')
        if flow:user['flow']=flow
        return {'protocol':'vless','settings':{'vnext':[{'address':host,'port':port,'users':[user]}]},'streamSettings':stream_settings(q)}
    if c.scheme=='trojan':
        return {'protocol':'trojan','settings':{'servers':[{'address':host,'port':port,'password':urllib.parse.unquote(u.username or '')}]},'streamSettings':stream_settings(q, 'tcp', 'tls')}
    if c.scheme=='ss':
        user=u.username or ''; pwd=u.password or ''; method=''
        try:
            dec=b64dec(user); method,pwd=dec.split(':',1)
        except Exception:
            if ':' in user: method,pwd=user.split(':',1)
            else:
                try:
                    dec=b64dec(raw[5:].split('@')[0]);method,pwd=dec.split(':',1)
                except Exception: pass
        return {'protocol':'shadowsocks','settings':{'servers':[{'address':host,'port':port,'method':method,'password':pwd}]}}
    raise ValueError('unsupported protocol')

def xray_config(c,port):
    return {'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'auth':'noauth','udp':False}}],'outbounds':[outbound(c)]}

def wait_port(port,timeout=8):
    end=time.time()+timeout
    while time.time()<end:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.3): return True
        except Exception: time.sleep(.15)
    return False

def upload_test(c,index):
    if not XRAY.exists(): return 0,'Xray پیدا نشد'
    port=19000+(index%500)
    cfg=APP_DIR/f'.xray_test_{index}.json'; data=APP_DIR/f'.upload_{index}.bin'; proc=None
    try:
        cfg.write_text(json.dumps(xray_config(c,port),ensure_ascii=False),encoding='utf-8')
        with open(data,'wb') as f:f.write(os.urandom(UP_SIZE))
        proc=subprocess.Popen([str(XRAY),'run','-c',str(cfg)],cwd=str(APP_DIR),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if not wait_port(port): return 0,'Xray/proxy start failed'
        cmd=['curl.exe','--socks5-hostname',f'127.0.0.1:{port}','--connect-timeout','6','--max-time','15','-sS','-o','NUL','-X','POST','--data-binary',f'@{data}','-w','%{http_code} %{time_total} %{size_upload}','https://speed.cloudflare.com/__up']
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
        parts=r.stdout.strip().split(); code=parts[0] if parts else '000'; secs=float(parts[1]) if len(parts)>1 else 0
        if code.startswith('2') and secs>0:
            return (UP_SIZE*8/secs/1000000),f'Upload {code}'
        return 0,f'Upload failed ({code})'
    except Exception as e: return 0,str(e)[:45]
    finally:
        if proc:
            try: proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
        for p in (cfg,data):
            try:p.unlink(missing_ok=True)
            except Exception:pass
def tcp_check(c,timeout=4):
    t=time.perf_counter()
    try:
        with socket.create_connection((c.host,c.port),timeout=timeout): pass
        return True,(time.perf_counter()-t)*1000,'TCP OK'
    except Exception as e: return False,0,str(e)[:60]

def real_latency(c,index):
    if not XRAY.exists(): return False,0,'Xray Ù¾ÛŒØ¯Ø§ Ù†Ø´Ø¯'
    port=18500+(index%500); cfg=APP_DIR/f'.xray_ping_{index}.json'; proc=None
    try:
        cfg.write_text(json.dumps(xray_config(c,port),ensure_ascii=False),encoding='utf-8')
        proc=subprocess.Popen([str(XRAY),'run','-c',str(cfg)],cwd=str(APP_DIR),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if not wait_port(port,8): return False,0,'Xray/proxy start failed'
        vals=[]
        for _ in range(3):
            t=time.perf_counter()
            r=subprocess.run(['curl.exe','--socks5-hostname',f'127.0.0.1:{port}','--connect-timeout','5','--max-time','8','-sS','-o','NUL','-w','%{http_code} %{time_connect} %{time_appconnect} %{time_starttransfer}','https://www.cloudflare.com/cdn-cgi/trace'],capture_output=True,text=True,timeout=10)
            parts=r.stdout.strip().split()
            if parts and parts[0].startswith('2'):
                vals.append((time.perf_counter()-t)*1000)
        if vals:
            vals.sort(); return True,vals[len(vals)//2],'REAL PROXY'
        return False,0,'Proxy request failed'
    except Exception as e: return False,0,str(e)[:45]
    finally:
        if proc:
            try: proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass
        try: cfg.unlink(missing_ok=True)
        except Exception: pass

class App(tk.Tk):
    def __init__(self):
        super().__init__();self.title(T['title']);self.geometry('1180x720');self.events=queue.Queue();self.results=[];self.stop=False;self.ui();self.after(100,self.drain)
    def ui(self):
        top=ttk.Frame(self,padding=10);top.pack(fill='x');ttk.Label(top,text=T['sub'],font=('Tahoma',10)).pack(anchor='w')
        self.urls=tk.Text(top,height=4,font=('Tahoma',10));self.urls.pack(fill='x',pady=5);bar=ttk.Frame(top);bar.pack(fill='x')
        self.start_btn=ttk.Button(bar,text=T['start'],command=self.start);self.start_btn.pack(side='left');ttk.Button(bar,text=T['stop'],command=self.cancel).pack(side='left',padx=6)
        self.progress=ttk.Progressbar(bar,mode='determinate');self.progress.pack(side='left',fill='x',expand=True,padx=10);self.summary=ttk.Label(bar,text=T['ready'],font=('Tahoma',9));self.summary.pack(side='right')
        cols=('#','name','protocol','host','active','latency','upload','status');self.tree=ttk.Treeview(self,columns=cols,show='headings')
        heads={'#':'#','name':T['name'],'protocol':T['protocol'],'host':T['server'],'active':T['active'],'latency':'Latency ms','upload':'Upload Mbps','status':T['status']};widths={'#':45,'name':250,'protocol':85,'host':230,'active':75,'latency':95,'upload':110,'status':250}
        for c in cols:self.tree.heading(c,text=heads[c]);self.tree.column(c,width=widths[c],anchor='center')
        self.tree.pack(fill='both',expand=True,padx=10,pady=10);ttk.Label(self,text=T['footer'],padding=10,font=('Tahoma',9)).pack(fill='x')
    def cancel(self):self.stop=True;self.summary.config(text=fa('\u062f\u0631 \u062d\u0627\u0644 \u062a\u0648\u0642\u0641...'))
    def start(self):
        urls=[x.strip() for x in self.urls.get('1.0','end').splitlines() if x.strip()]
        if not urls:return messagebox.showwarning('ZMOBGPT',T['empty'])
        self.start_btn.config(state='disabled');self.stop=False;threading.Thread(target=self.worker,args=(urls,),daemon=True).start()
    def worker(self,urls):
        cfgs=[]
        for url in urls:
            try:
                with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ZMOBGPT/1.0'}),timeout=15) as r:cfgs+=parse_configs(r.read().decode('utf-8','ignore'))
            except Exception as e:self.events.put(('summary',fa('\u062e\u0637\u0627 \u062f\u0631 \u062f\u0631\u06cc\u0627\u0641\062a: ')+str(e)[:60]))
        for i,c in enumerate(cfgs,1):c.index=i
        self.results=[Result(c) for c in cfgs];self.events.put(('reset',self.results));self.progress['maximum']=max(1,len(cfgs))
        for i,r in enumerate(self.results,1):
            if self.stop:break
            r.active,r.latency,r.status=real_latency(r.cfg,r.cfg.index);self.events.put(('row',r));self.events.put(('progress',i))
        active=[r for r in self.results if r.active]
        for i,r in enumerate(active,1):
            if self.stop:break
            r.upload,r.status=upload_test(r.cfg,r.cfg.index);self.events.put(('row',r));self.events.put(('summary',fa(f'{i} / {len(active)} Upload تست شد')))
        good=[r for r in active if r.upload>0];best=max(good,key=lambda x:x.upload) if good else None
        msg=fa(f'{len(active)} فعال از {len(self.results)} — {len(good)} تست Upload موفق')
        if best:msg+=fa(f' — بهترین: {best.cfg.name} ({best.upload:.2f} Mbps)')
        self.events.put(('summary',msg));self.events.put(('done',None))
    def vals(self,r):return(r.cfg.index,r.cfg.name,r.cfg.scheme,r.cfg.host,T['yes'] if r.active else T['no'],f'{r.latency:.0f}',f'{r.upload:.2f}',r.status)
    def insert(self,r):self.tree.insert('','end',iid=str(r.cfg.index),values=self.vals(r))
    def update_row(self,r):
        if not self.tree.exists(str(r.cfg.index)):self.insert(r)
        else:self.tree.item(str(r.cfg.index),values=self.vals(r))
    def drain(self):
        try:
            while True:
                typ,d=self.events.get_nowait()
                if typ=='reset':self.tree.delete(*self.tree.get_children());[self.insert(r) for r in d]
                elif typ=='row':self.update_row(d)
                elif typ=='progress':self.progress['value']=d
                elif typ=='summary':self.summary.config(text=d)
                elif typ=='done':self.start_btn.config(state='normal')
        except queue.Empty:pass
        self.after(100,self.drain)

if __name__=='__main__':
    App().mainloop()
