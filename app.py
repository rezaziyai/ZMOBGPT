import base64, json, queue, socket, ssl, subprocess, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
XRAY = APP_DIR / 'xray.exe'

@dataclass
class Config:
    index:int; raw:str; name:str; scheme:str; host:str=''; port:int=0
@dataclass
class Result:
    cfg:Config; active:bool=False; latency:float=0; upload:float=0; status:str=''

def decode_sub(text):
    text=text.strip()
    try:
        raw=base64.b64decode(text+'='*(-len(text)%4),validate=False).decode('utf-8','ignore')
        if '://' in raw: return raw
    except Exception: pass
    return text

def parse_configs(text):
    out=[]
    for i,line in enumerate(decode_sub(text).replace('\r','').split('\n'),1):
        line=line.strip()
        if not line.startswith(('vless://','vmess://','trojan://','ss://')): continue
        try:
            u=urllib.parse.urlsplit(line); q=urllib.parse.parse_qs(u.query)
            name=urllib.parse.unquote(q.get('remarks',[''])[0] or u.fragment or f'Config {i}')
            out.append(Config(i,line,name,u.scheme.lower(),u.hostname or '',u.port or 0))
        except Exception: pass
    return out

def tcp_check(c,timeout=4):
    t=time.perf_counter()
    try:
        with socket.create_connection((c.host,c.port),timeout=timeout): pass
        return True,(time.perf_counter()-t)*1000,'TCP OK'
    except Exception as e: return False,0,str(e)[:70]

def xray_config(c,port):
    u=urllib.parse.urlsplit(c.raw); q=urllib.parse.parse_qs(u.query)
    if c.scheme=='vless':
        stream={'network':q.get('type',['tcp'])[0]}; sec=q.get('security',['none'])[0]
        if sec!='none': stream['security']=sec
        if sec=='tls': stream['tlsSettings']={'serverName':q.get('sni',[u.hostname])[0]}
        if sec=='reality': stream['realitySettings']={'serverName':q.get('sni',[u.hostname])[0],'fingerprint':q.get('fp',['chrome'])[0],'publicKey':q.get('pbk',[''])[0],'shortId':q.get('sid',[''])[0]}
        if stream['network']=='ws': stream['wsSettings']={'path':q.get('path',['/'])[0],'headers':{'Host':q.get('host',[u.hostname])[0]}}
        if stream['network']=='grpc': stream['grpcSettings']={'serviceName':q.get('serviceName',[''])[0]}
        ob={'protocol':'vless','settings':{'vnext':[{'address':u.hostname,'port':u.port or 443,'users':[{'id':u.username,'encryption':q.get('encryption',['none'])[0],'flow':q.get('flow',[''])[0]}]}]},'streamSettings':stream}
    elif c.scheme=='trojan':
        stream={'network':q.get('type',['tcp'])[0],'security':'tls','tlsSettings':{'serverName':q.get('sni',[u.hostname])[0]}}
        ob={'protocol':'trojan','settings':{'servers':[{'address':u.hostname,'port':u.port or 443,'password':urllib.parse.unquote(u.username or '')}]},'streamSettings':stream}
    elif c.scheme=='vmess':
        try: d=json.loads(base64.b64decode((u.netloc+u.path)+'='*(-len(u.netloc+u.path)%4)).decode())
        except Exception: raise ValueError('bad vmess')
        stream={'network':d.get('net','tcp')}
        if d.get('tls')=='tls': stream.update({'security':'tls','tlsSettings':{'serverName':d.get('sni') or d.get('host') or d['add']}})
        if stream['network']=='ws': stream['wsSettings']={'path':d.get('path','/'),'headers':{'Host':d.get('host',d['add'])}}
        ob={'protocol':'vmess','settings':{'vnext':[{'address':d['add'],'port':int(d.get('port',443)),'users':[{'id':d['id'],'alterId':int(d.get('aid',0)),'security':d.get('scy','auto')}]}]},'streamSettings':stream}
    else: raise ValueError('unsupported')
    return {'log':{'loglevel':'warning'},'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'udp':True}}],'outbounds':[ob]}

def upload_test(c):
    if not XRAY.exists(): return 0,'Xray core missing'
    port=20000+(c.index%1000); conf=APP_DIR/f'.test_{c.index}.json'; p=None
    try:
        conf.write_text(json.dumps(xray_config(c,port),ensure_ascii=False),encoding='utf-8')
        p=subprocess.Popen([str(XRAY),'run','-c',str(conf)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2): break
            except OSError: time.sleep(.1)
        # urllib needs PySocks for socks5. If unavailable, report it instead of pretending.
        try: import socks
        except ImportError: return 0,'Install PySocks for upload test'
        s=socks.socksocket(); s.set_proxy(socks.SOCKS5,'127.0.0.1',port); s.settimeout(6)
        host='speed.cloudflare.com'; s.connect((host,443)); ss=socket.create_connection(('127.0.0.1',1),timeout=.01) if False else None
        payload=b'Z'*262144; req=(f'POST /__up HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode()+payload
        t=time.perf_counter(); s.sendall(req); s.recv(256); elapsed=max(time.perf_counter()-t,.001)
        return len(payload)*8/elapsed/1e6,'Upload OK'
    except Exception as e: return 0,str(e)[:70]
    finally:
        if p: p.terminate()
        try: conf.unlink()
        except OSError: pass

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('ZMOBGPT â€” Config Tester'); self.geometry('1180x720'); self.events=queue.Queue(); self.results=[]; self.stop=False; self.ui(); self.after(100,self.drain)
    def ui(self):
        top=ttk.Frame(self,padding=10); top.pack(fill='x'); ttk.Label(top,text='Ù„ÛŒÙ†Ú©â€ŒÙ‡Ø§ÛŒ Subscription (Ù‡Ø± Ø®Ø· ÛŒÚ© Ù„ÛŒÙ†Ú©):').pack(anchor='w')
        self.urls=tk.Text(top,height=4); self.urls.pack(fill='x',pady=5); bar=ttk.Frame(top); bar.pack(fill='x')
        self.start_btn=ttk.Button(bar,text='Ø¯Ø±ÛŒØ§ÙØª Ùˆ Ø´Ø±ÙˆØ¹ ØªØ³Øª',command=self.start); self.start_btn.pack(side='left'); ttk.Button(bar,text='ØªÙˆÙ‚Ù',command=self.cancel).pack(side='left',padx=6)
        self.progress=ttk.Progressbar(bar,mode='determinate'); self.progress.pack(side='left',fill='x',expand=True,padx=10); self.summary=ttk.Label(bar,text='Ø¢Ù…Ø§Ø¯Ù‡'); self.summary.pack(side='right')
        cols=('#','name','protocol','host','active','latency','upload','status'); self.tree=ttk.Treeview(self,columns=cols,show='headings'); heads={'#':'#','name':'Ù†Ø§Ù…','protocol':'Ù¾Ø±ÙˆØªÚ©Ù„','host':'Ø³Ø±ÙˆØ±','active':'ÙØ¹Ø§Ù„','latency':'Latency ms','upload':'Upload Mbps','status':'ÙˆØ¶Ø¹ÛŒØª'}
        widths={'#':45,'name':230,'protocol':80,'host':210,'active':70,'latency':90,'upload':110,'status':260}
        for c in cols: self.tree.heading(c,text=heads[c]); self.tree.column(c,width=widths[c],anchor='center')
        self.tree.pack(fill='both',expand=True,padx=10,pady=10); ttk.Label(self,text='ÙØ¹Ø§Ù„â€ŒÙ‡Ø§ Ø§Ø¨ØªØ¯Ø§ Ø¨Ø±Ø±Ø³ÛŒ Ù…ÛŒâ€ŒØ´ÙˆÙ†Ø¯Ø› Ø³Ù¾Ø³ Upload ØªØ³Øª Ù…ÛŒâ€ŒØ´ÙˆØ¯ Ùˆ Ø¬Ø¯ÙˆÙ„ Ø¯Ø± Ù„Ø­Ø¸Ù‡ Ø¨Ø± Ø§Ø³Ø§Ø³ Ù†ØªÛŒØ¬Ù‡ Ø¨Ù‡â€ŒØ±ÙˆØ²Ø±Ø³Ø§Ù†ÛŒ Ù…ÛŒâ€ŒØ´ÙˆØ¯.',padding=10).pack(fill='x')
    def cancel(self): self.stop=True; self.summary.config(text='Ø¯Ø± Ø­Ø§Ù„ ØªÙˆÙ‚Ù...')
    def start(self):
        urls=[x.strip() for x in self.urls.get('1.0','end').splitlines() if x.strip()]
        if not urls: return messagebox.showwarning('ZMOBGPT','Ø­Ø¯Ø§Ù‚Ù„ ÛŒÚ© Ù„ÛŒÙ†Ú© Subscription ÙˆØ§Ø±Ø¯ Ú©Ù†ÛŒØ¯.')
        self.start_btn.config(state='disabled'); self.stop=False; threading.Thread(target=self.worker,args=(urls,),daemon=True).start()
    def worker(self,urls):
        cfgs=[]
        for url in urls:
            try:
                with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ZMOBGPT/1.0'}),timeout=15) as r: cfgs += parse_configs(r.read().decode('utf-8','ignore'))
            except Exception as e: self.events.put(('summary',f'Ø®Ø·Ø§ Ø¯Ø± Ø¯Ø±ÛŒØ§ÙØª: {e}'))
        for i,c in enumerate(cfgs,1): c.index=i
        self.results=[Result(c) for c in cfgs]; self.events.put(('reset',self.results)); self.events.put(('summary',f'{len(cfgs)} Ú©Ø§Ù†ÙÛŒÚ¯ Ø¯Ø±ÛŒØ§ÙØª Ø´Ø¯Ø› ØªØ³Øª ÙØ¹Ø§Ù„ Ø¨ÙˆØ¯Ù†...')); self.progress['maximum']=max(1,len(cfgs))
        for i,r in enumerate(self.results,1):
            if self.stop: break
            r.active,r.latency,r.status=tcp_check(r.cfg); self.events.put(('row',r)); self.events.put(('progress',i))
        active=[r for r in self.results if r.active]; self.events.put(('summary',f'{len(active)} ÙØ¹Ø§Ù„ Ø§Ø² {len(self.results)}Ø› Ø´Ø±ÙˆØ¹ ØªØ³Øª Upload...')); self.progress['maximum']=max(1,len(active))
        for i,r in enumerate(active,1):
            if self.stop: break
            r.upload,r.status=upload_test(r.cfg); self.events.put(('row',r)); self.events.put(('progress',i)); self.events.put(('summary',f'Ø¨Ù‡ØªØ±ÛŒÙ† ÙØ¹Ù„ÛŒ: {self.best()}'))
        self.events.put(('summary',f'ØªÙ…Ø§Ù… Ø´Ø¯ â€” {len(active)} ÙØ¹Ø§Ù„ | {self.best()}')); self.events.put(('done',None))
    def best(self):
        a=[r for r in self.results if r.active and r.upload>0]
        if not a: return 'Ù‡Ù†ÙˆØ² Ù†ØªÛŒØ¬Ù‡â€ŒØ§ÛŒ Ù†ÛŒØ³Øª'
        b=max(a,key=lambda r:r.upload); return f'#{b.cfg.index} {b.upload:.2f} Mbps'
    def drain(self):
        try:
            while True:
                typ,d=self.events.get_nowait()
                if typ=='reset':
                    self.tree.delete(*self.tree.get_children()); [self.insert(r) for r in d]
                elif typ=='row': self.update_row(d)
                elif typ=='progress': self.progress['value']=d
                elif typ=='summary': self.summary.config(text=d)
                elif typ=='done': self.start_btn.config(state='normal'); self.sort_best()
        except queue.Empty: pass
        self.after(100,self.drain)
    def vals(self,r): return (r.cfg.index,r.cfg.name,r.cfg.scheme,r.cfg.host,'Ø¨Ù„Ù‡' if r.active else 'Ø®ÛŒØ±',f'{r.latency:.0f}',f'{r.upload:.2f}',r.status)
    def insert(self,r): self.tree.insert('','end',iid=str(r.cfg.index),values=self.vals(r))
    def update_row(self,r):
        if not self.tree.exists(str(r.cfg.index)): self.insert(r)
        else: self.tree.item(str(r.cfg.index),values=self.vals(r))
    def sort_best(self):
        for i in sorted(self.tree.get_children(),key=lambda x:(self.tree.set(x,'active')!='Ø¨Ù„Ù‡',-float(self.tree.set(x,'upload') or 0),float(self.tree.set(x,'latency') or 99999))): self.tree.move(i,'','end')

if __name__=='__main__': App().mainloop()

