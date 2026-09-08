import base64, json, queue, socket, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
APP_DIR=Path(__file__).resolve().parent

def fa(s): return s.encode('ascii').decode('unicode_escape')
T={'title':fa('ZMOBGPT — کانفیگ تستر'),'sub':fa('لینک‌های Subscription (هر خط یک لینک):'),'start':fa('دریافت و شروع تست'),'stop':fa('توقف'),'ready':fa('آماده'),'name':fa('نام'),'protocol':fa('پروتکل'),'server':fa('سرور'),'active':fa('فعال'),'status':fa('وضعیت'),'yes':fa('بله'),'no':fa('خیر'),'footer':fa('ابتدا فعال بودن بررسی می‌شود، سپس Upload تست می‌شود و بهترین نتیجه همزمان نمایش داده می‌شود.'),'empty':fa('حداقل یک لینک Subscription وارد کنید.')}
@dataclass
class Config: index:int; raw:str; name:str; scheme:str; host:str=''; port:int=0
@dataclass
class Result: cfg:Config; active:bool=False; latency:float=0; upload:float=0; status:str=''
def decode_sub(text):
    try:
        raw=base64.b64decode(text.strip()+'='*(-len(text.strip())%4),validate=False).decode('utf-8','ignore')
        if '://' in raw:return raw
    except Exception:pass
    return text
def parse_configs(text):
    out=[]
    for i,line in enumerate(decode_sub(text).replace('\r','').split('\n'),1):
        line=line.strip()
        if not line.startswith(('vless://','vmess://','trojan://','ss://')):continue
        try:
            u=urllib.parse.urlsplit(line);q=urllib.parse.parse_qs(u.query);name=urllib.parse.unquote(q.get('remarks',[''])[0] or u.fragment or f'Config {i}')
            out.append(Config(i,line,name,u.scheme.lower(),u.hostname or '',u.port or 0))
        except Exception:pass
    return out
def tcp_check(c,timeout=4):
    t=time.perf_counter()
    try:
        with socket.create_connection((c.host,c.port),timeout=timeout):pass
        return True,(time.perf_counter()-t)*1000,'TCP OK'
    except Exception as e:return False,0,str(e)[:60]
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
    def cancel(self):self.stop=True;self.summary.config(text=fa('در حال توقف...'))
    def start(self):
        urls=[x.strip() for x in self.urls.get('1.0','end').splitlines() if x.strip()]
        if not urls:return messagebox.showwarning('ZMOBGPT',T['empty'])
        self.start_btn.config(state='disabled');self.stop=False;threading.Thread(target=self.worker,args=(urls,),daemon=True).start()
    def worker(self,urls):
        cfgs=[]
        for url in urls:
            try:
                with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ZMOBGPT/1.0'}),timeout=15) as r:cfgs+=parse_configs(r.read().decode('utf-8','ignore'))
            except Exception as e:self.events.put(('summary',fa('خطا در دریافت: ')+str(e)[:60]))
        for i,c in enumerate(cfgs,1):c.index=i
        self.results=[Result(c) for c in cfgs];self.events.put(('reset',self.results));self.progress['maximum']=max(1,len(cfgs))
        for i,r in enumerate(self.results,1):
            if self.stop:break
            r.active,r.latency,r.status=tcp_check(r.cfg);self.events.put(('row',r));self.events.put(('progress',i))
        active=[r for r in self.results if r.active];self.events.put(('summary',fa(f'{len(active)} فعال از {len(self.results)} کانفیگ — آماده تست Upload')));self.events.put(('done',None))
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
if __name__=='__main__':App().mainloop()
